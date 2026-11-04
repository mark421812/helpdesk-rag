"""共用的 OpenAI-compatible Chat Completions HTTP adapter。

這個模組只負責不同 hosted LLM 共通的傳輸、安全邊界與 response mapping。
各供應商的薄 wrapper 必須明確傳入 endpoint 差異，避免把 NVIDIA/Groq 規則混在一起。
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx

from answer_generation_service.application.dtos import (
    ProviderGenerationRequest,
    ProviderGenerationResult,
)
from answer_generation_service.domain.exceptions import (
    ProviderContractError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class OpenAICompatibleChatProvider:
    """執行受限、非串流的 OpenAI-compatible Chat Completions 呼叫。"""

    def __init__(
        self,
        *,
        provider_name: str,
        base_url: str,
        api_key: str,
        model: str,
        token_limit_field: Literal["max_tokens", "max_completion_tokens"],
        response_format: dict[str, object] | None,
        supports_pending_responses: bool,
        retryable_statuses: frozenset[int],
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
        total_timeout_seconds: float,
        max_response_bytes: int,
        total_attempts: int,
        retry_backoff_seconds: float,
        retry_max_backoff_seconds: float,
        max_concurrency: int,
        max_queue_size: int,
        queue_wait_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._token_limit_field = token_limit_field
        self._response_format = response_format
        self._supports_pending_responses = supports_pending_responses
        self._retryable_statuses = retryable_statuses
        self._total_timeout_seconds = total_timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._total_attempts = total_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # admitted 同時涵蓋正在呼叫 Provider 與等待 semaphore 的工作。generate() 在
        # 第一次 await 前完成 check/increment，因此在單一 event loop 內不會形成無界 queue。
        self._max_admitted = max_concurrency + max_queue_size
        self._admitted = 0
        self._queue_wait_timeout_seconds = queue_wait_timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={
                "authorization": f"Bearer {api_key}",
                "accept": "application/json",
                "content-type": "application/json",
            },
            timeout=httpx.Timeout(
                connect=connect_timeout_seconds,
                read=read_timeout_seconds,
                write=read_timeout_seconds,
                pool=connect_timeout_seconds,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def generate(self, request: ProviderGenerationRequest) -> ProviderGenerationResult:
        if self._admitted >= self._max_admitted:
            raise ProviderRateLimitError(retry_after_seconds=self._queue_retry_after_seconds())
        self._admitted += 1
        try:
            return await self._generate_admitted(request)
        finally:
            self._admitted -= 1

    async def _generate_admitted(
        self,
        request: ProviderGenerationRequest,
    ) -> ProviderGenerationResult:
        payload: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": message.role, "content": message.content} for message in request.messages
            ],
            self._token_limit_field: request.max_output_tokens,
            "temperature": request.temperature,
            "stream": False,
        }
        if self._response_format is not None:
            payload["response_format"] = dict(self._response_format)

        deadline = time.monotonic() + self._total_timeout_seconds
        acquired = False
        try:
            remaining = deadline - time.monotonic()
            wait_timeout = min(self._queue_wait_timeout_seconds, remaining)
            if wait_timeout <= 0:
                raise ProviderRateLimitError(retry_after_seconds=self._queue_retry_after_seconds())
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=wait_timeout)
                acquired = True
            except TimeoutError as exc:
                raise ProviderRateLimitError(
                    retry_after_seconds=self._queue_retry_after_seconds()
                ) from exc
            response_payload = await self._invoke_with_retry(payload, deadline)
        finally:
            if acquired:
                self._semaphore.release()
        return self._map_success(response_payload)

    def _queue_retry_after_seconds(self) -> int:
        return max(1, math.ceil(self._queue_wait_timeout_seconds))

    def is_ready(self) -> bool:
        # Readiness 不發送 generation，避免探測本身消耗外部 quota。
        return bool(self._base_url and self._model)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _invoke_with_retry(
        self,
        payload: dict[str, object],
        deadline: float,
    ) -> dict[str, Any]:
        last_status: int | None = None
        for attempt in range(self._total_attempts):
            try:
                status, headers, response_payload = await self._request_json(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    deadline=deadline,
                    body=payload,
                )
            except httpx.TimeoutException as exc:
                if attempt + 1 >= self._total_attempts:
                    raise ProviderTimeoutError("Generation provider timed out") from exc
                await self._backoff(attempt, None, deadline)
                continue
            except httpx.TransportError as exc:
                if attempt + 1 >= self._total_attempts:
                    raise ProviderUnavailableError("Generation provider is unavailable") from exc
                await self._backoff(attempt, None, deadline)
                continue

            last_status = status
            if status == 200:
                return _require_object(response_payload)
            if status == 202:
                if not self._supports_pending_responses:
                    raise ProviderContractError(
                        "Generation provider returned an unsupported pending response"
                    )
                request_id = _request_id(response_payload)
                return await self._poll(request_id, deadline)
            if status == 429:
                retry_after = _retry_after_seconds(headers)
                if attempt + 1 >= self._total_attempts:
                    raise ProviderRateLimitError(retry_after_seconds=retry_after)
                await self._backoff(attempt, retry_after, deadline)
                continue
            if status in self._retryable_statuses:
                if attempt + 1 >= self._total_attempts:
                    raise ProviderUnavailableError("Generation provider is unavailable")
                await self._backoff(attempt, _retry_after_seconds(headers), deadline)
                continue
            if status in {401, 403, 404}:
                raise ProviderUnavailableError("Generation provider authorization is unavailable")
            if status in {400, 413, 422}:
                raise ProviderContractError("Generation provider rejected the configured request")
            raise ProviderUnavailableError("Generation provider returned an unsupported status")
        raise ProviderUnavailableError(f"Generation provider failed with status {last_status}")

    async def _poll(self, request_id: str, deadline: float) -> dict[str, Any]:
        polling_url = f"{self._base_url}/status/{quote(request_id, safe='')}"
        attempt = 0
        retry_after_seconds: int | None = None
        while True:
            await self._backoff(attempt, retry_after_seconds, deadline)
            retry_after_seconds = None
            attempt += 1
            try:
                status, headers, payload = await self._request_json(
                    "GET", polling_url, deadline=deadline, body=None
                )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError("Generation provider status polling timed out") from exc
            except httpx.TransportError as exc:
                raise ProviderUnavailableError("Generation provider status polling failed") from exc
            if status == 200:
                return _require_object(payload)
            if status == 202:
                continue
            if status == 429 or status in self._retryable_statuses:
                # 下一輪只 backoff 一次，避免同一個 response 被重複計時。
                retry_after_seconds = _retry_after_seconds(headers)
                continue
            raise ProviderUnavailableError("Generation provider status polling failed")

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        deadline: float,
        body: dict[str, object] | None,
    ) -> tuple[int, httpx.Headers, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderTimeoutError("Generation provider total deadline exceeded")
        try:
            async with asyncio.timeout(remaining):
                async with self._client.stream(method, url, json=body) as response:
                    declared_length = response.headers.get("content-length")
                    if declared_length is not None:
                        try:
                            if int(declared_length) > self._max_response_bytes:
                                raise ProviderContractError(
                                    "Generation provider response exceeded the size limit"
                                )
                        except ValueError as exc:
                            raise ProviderContractError(
                                "Generation provider returned an invalid content length"
                            ) from exc

                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise ProviderContractError(
                                "Generation provider response exceeded the size limit"
                            )
                        chunks.append(chunk)
                    raw_body = b"".join(chunks)
                    successful_statuses = {200, 202} if self._supports_pending_responses else {200}
                    if response.status_code not in successful_statuses:
                        # Error body 可能回顯 Prompt，故不解析、不記錄也不往上傳。
                        return response.status_code, response.headers, None
                    content_type = response.headers.get("content-type", "").casefold()
                    if "application/json" not in content_type:
                        raise ProviderContractError(
                            "Generation provider returned an unexpected content type"
                        )
                    try:
                        parsed: Any = json.loads(raw_body)
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ProviderContractError(
                            "Generation provider returned invalid JSON"
                        ) from exc
                    return response.status_code, response.headers, parsed
        except TimeoutError as exc:
            raise ProviderTimeoutError("Generation provider total deadline exceeded") from exc

    async def _backoff(
        self,
        attempt: int,
        retry_after_seconds: int | None,
        deadline: float,
    ) -> None:
        delay = (
            float(retry_after_seconds)
            if retry_after_seconds is not None
            else min(
                self._retry_max_backoff_seconds,
                self._retry_backoff_seconds * (2**attempt),
            )
        )
        remaining = deadline - time.monotonic()
        if remaining <= delay:
            raise ProviderTimeoutError("Generation provider total deadline exceeded")
        await asyncio.sleep(delay)

    def _map_success(self, payload: dict[str, Any]) -> ProviderGenerationResult:
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderContractError("Generation provider returned invalid choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderContractError("Generation provider returned invalid choice")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        finish_reason = choice.get("finish_reason")
        if not isinstance(content, str) or not isinstance(finish_reason, str):
            raise ProviderContractError("Generation provider returned invalid message content")
        if not content.strip() or not finish_reason.strip():
            raise ProviderContractError("Generation provider returned blank message content")
        usage = payload.get("usage")
        return ProviderGenerationResult(
            content=content,
            provider=self._provider_name,
            model=self._model,
            finish_reason=finish_reason,
            prompt_tokens=_optional_non_negative_int(usage, "prompt_tokens"),
            completion_tokens=_optional_non_negative_int(usage, "completion_tokens"),
        )


def _require_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderContractError("Generation provider returned an invalid object")
    return value


def _request_id(value: Any) -> str:
    payload = _require_object(value)
    request_id = payload.get("requestId", payload.get("request_id"))
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
        raise ProviderContractError("Generation provider returned an invalid pending request ID")
    return request_id


def _optional_non_negative_int(value: Any, field_name: str) -> int | None:
    if not isinstance(value, dict) or field_name not in value:
        return None
    field_value = value[field_name]
    if type(field_value) is not int or field_value < 0:
        raise ProviderContractError("Generation provider returned invalid token usage")
    return field_value


def _retry_after_seconds(headers: httpx.Headers) -> int | None:
    raw_value = headers.get("retry-after")
    if raw_value is None:
        return None
    try:
        numeric_value = float(raw_value)
        if not math.isfinite(numeric_value):
            return None
        return max(0, math.ceil(numeric_value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
            return max(0, math.ceil(retry_at.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            return None
