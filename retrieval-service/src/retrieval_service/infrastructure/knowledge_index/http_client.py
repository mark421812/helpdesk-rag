"""Knowledge Index Service 的 async HTTP adapter。

這個 class 實作 Application 的 ``KnowledgeSearchPort``：向內回傳 Domain/Application 型別，
向外處理 JSON、HTTP status、timeout、retry、API key 與 response-size protection。Adapter
把不穩定的網路細節隔離在 Infrastructure layer。
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any, NoReturn
from uuid import UUID

import httpx

from retrieval_service.application.dtos import KnowledgeSearchCriteria, KnowledgeSearchResult
from retrieval_service.application.ports import KnowledgeSearchPort
from retrieval_service.domain.entities import CandidateChunk
from retrieval_service.domain.enums import Confidentiality, DocumentStatus
from retrieval_service.domain.exceptions import (
    DependencyContractError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    DomainValidationError,
)
from retrieval_service.infrastructure.request_context import request_id_context

_DEPENDENCY = "knowledge-index-service"
_RETRYABLE_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class KnowledgeIndexHttpClient(KnowledgeSearchPort):
    """以 httpx connection pool 呼叫 KIS，並嚴格驗證 response contract。"""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        connect_timeout_seconds: float = 3,
        read_timeout_seconds: float = 45,
        total_timeout_seconds: float = 50,
        readiness_timeout_seconds: float = 5,
        max_response_body_bytes: int = 5_000_000,
        retry_attempts: int = 1,
        retry_backoff_seconds: float = 0.2,
        retry_max_backoff_seconds: float = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """建立可重用的 async client；constructor 只設定資源，不發出網路請求。"""

        if max_response_body_bytes < 1:
            raise ValueError("max_response_body_bytes must be positive")
        if retry_attempts < 0:
            raise ValueError("retry_attempts must not be negative")
        if total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be positive")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if retry_max_backoff_seconds < retry_backoff_seconds:
            raise ValueError(
                "retry_max_backoff_seconds must not be smaller than retry_backoff_seconds"
            )
        # 只在 key 存在時加入 header，讓本機 KIS 可以選擇停用驗證。
        headers = {"accept": "application/json"}
        if api_key:
            headers["x-internal-api-key"] = api_key
        timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=connect_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._readiness_timeout_seconds = readiness_timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds
        self._max_response_body_bytes = max_response_body_bytes
        self._retry_attempts = retry_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._retry_max_backoff_seconds = retry_max_backoff_seconds

    async def search(self, criteria: KnowledgeSearchCriteria) -> KnowledgeSearchResult:
        """把 framework-free criteria 序列化成 KIS JSON contract。"""

        payload = {
            "query": criteria.query,
            "limit": criteria.limit,
            "score_threshold": criteria.score_threshold,
            "filters": {
                "user_id": criteria.principal.user_id,
                "group_ids": list(criteria.principal.group_ids),
                "statuses": [criteria.status.value],
                "confidentialities": [],
                "language": criteria.language,
            },
        }
        # ``asyncio.timeout`` 包住所有 retry，提供單次 Retrieval 的總 deadline。
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                response = await self._request_with_retry(
                    "POST",
                    "/api/v1/search",
                    json=payload,
                )
        except TimeoutError as exc:
            raise DependencyTimeoutError(
                _DEPENDENCY,
                "Knowledge Index search exceeded its total timeout",
            ) from exc
        body = self._json_object(response)
        # 外部服務回傳的 JSON 型別在 runtime 不可信，必須逐欄驗證後才能建立 Domain entity。
        raw_items = body.get("items")
        if not isinstance(raw_items, list):
            self._contract_error("search response field 'items' must be an array")
        try:
            candidates = tuple(self._candidate(item) for item in raw_items)
            return KnowledgeSearchResult(
                candidates=candidates,
                model_id=_required_string(body, "model_id"),
                model_revision=_required_string(body, "model_revision"),
            )
        except (KeyError, TypeError, ValueError, DomainValidationError) as exc:
            raise DependencyContractError(
                _DEPENDENCY,
                "Knowledge Index search response violates the retrieval contract",
            ) from exc

    async def is_ready(self) -> bool:
        """同時確認 KIS dependencies ready，且目前 service key 通過驗證。"""

        try:
            response = await self._streamed_request(
                "GET",
                "/health/ready",
                request_timeout=self._readiness_timeout_seconds,
            )
            if response.status_code != 200:
                return False
            body = self._json_object(response)
            checks = body.get("checks")
            dependency_ready = (
                isinstance(checks, dict)
                and bool(checks)
                and all(value is True for value in checks.values())
            )
            if not dependency_ready:
                return False
            auth_response = await self._streamed_request(
                "GET",
                "/api/v1/auth/check",
                request_timeout=self._readiness_timeout_seconds,
            )
            if auth_response.status_code != 200:
                return False
            auth_body = self._json_object(auth_response)
            return auth_body.get("status") == "ok"
        except (httpx.HTTPError, DependencyContractError):
            return False

    async def close(self) -> None:
        """關閉 httpx connection pool；由 FastAPI lifespan 呼叫。"""

        await self._client.aclose()

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
    ) -> httpx.Response:
        """只對 timeout、連線錯誤與明確 retryable status 做有限次重試。"""

        last_response: httpx.Response | None = None
        # ``range(n + 1)`` 包含第一次嘗試，加上設定的 retry 次數。
        for attempt in range(self._retry_attempts + 1):
            try:
                response = await self._streamed_request(
                    method,
                    path,
                    json=json,
                )
            except httpx.TimeoutException as exc:
                if attempt < self._retry_attempts:
                    await self._wait_before_retry(attempt, None)
                    continue
                raise DependencyTimeoutError(
                    _DEPENDENCY,
                    "Knowledge Index search timed out",
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self._retry_attempts:
                    await self._wait_before_retry(attempt, None)
                    continue
                raise DependencyUnavailableError(
                    _DEPENDENCY,
                    f"Knowledge Index connection failed: {type(exc).__name__}",
                ) from exc

            if response.status_code == 200:
                return response
            last_response = response
            if response.status_code not in _RETRYABLE_STATUSES:
                break
            if attempt < self._retry_attempts:
                await self._wait_before_retry(attempt, response)
                continue
            break

        assert last_response is not None
        if last_response.status_code in _RETRYABLE_STATUSES:
            raise DependencyUnavailableError(
                _DEPENDENCY,
                f"Knowledge Index returned HTTP {last_response.status_code}",
            )
        raise DependencyContractError(
            _DEPENDENCY,
            f"Knowledge Index rejected the retrieval request with HTTP {last_response.status_code}",
        )

    async def _wait_before_retry(
        self,
        attempt: int,
        response: httpx.Response | None,
    ) -> None:
        """計算 capped exponential backoff，並加少量 jitter 避免服務同時重試。"""

        exponential = min(
            self._retry_backoff_seconds * (2**attempt),
            self._retry_max_backoff_seconds,
        )
        retry_after = 0.0
        if response is not None:
            raw_retry_after = response.headers.get("retry-after")
            if raw_retry_after is not None:
                try:
                    retry_after = max(0.0, float(raw_retry_after))
                except ValueError:
                    retry_after = 0.0
        base_delay = min(
            max(exponential, retry_after),
            self._retry_max_backoff_seconds,
        )
        jitter = random.uniform(0, min(base_delay * 0.25, 0.25))
        await asyncio.sleep(min(base_delay + jitter, self._retry_max_backoff_seconds))

    async def _streamed_request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        request_timeout: float | httpx.Timeout | None = None,
    ) -> httpx.Response:
        """逐段讀取解壓後 response；超過上限就立即停止，避免記憶體膨脹。"""

        async with self._client.stream(
            method,
            path,
            json=json,
            headers=self._request_context_headers(),
            timeout=(request_timeout if request_timeout is not None else self._client.timeout),
        ) as response:
            content_length = response.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > self._max_response_body_bytes:
                        self._contract_error("Knowledge Index response body is too large")
                except ValueError:
                    pass

            # bytearray 適合反覆 append bytes；最後再轉成不可變的 bytes。
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self._max_response_body_bytes:
                    self._contract_error("Knowledge Index response body is too large")
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=bytes(body),
                request=response.request,
            )

    def _request_context_headers(self) -> dict[str, str]:
        """把目前 coroutine 的 request ID 傳給 KIS，方便跨服務追蹤。"""

        request_id = request_id_context.get()
        return {"x-request-id": str(request_id)} if request_id else {}

    def _json_object(self, response: httpx.Response) -> dict[str, Any]:
        """解析 JSON，並保證 root 是 object/dict。"""

        try:
            payload = response.json()
        except ValueError as exc:
            raise DependencyContractError(
                _DEPENDENCY,
                "Knowledge Index returned invalid JSON",
            ) from exc
        if not isinstance(payload, dict):
            self._contract_error("Knowledge Index response must be a JSON object")
        return payload

    def _candidate(self, raw_item: object) -> CandidateChunk:
        """以 fail-closed 方式把不可信 JSON item 轉成 CandidateChunk。"""

        if not isinstance(raw_item, dict):
            self._contract_error("Knowledge Index search item must be a JSON object")
        status = DocumentStatus(_required_string(raw_item, "status"))
        if status != DocumentStatus.PUBLISHED:
            self._contract_error("Knowledge Index returned a non-published document")
        return CandidateChunk(
            chunk_id=UUID(_required_string(raw_item, "chunk_id")),
            document_id=UUID(_required_string(raw_item, "document_id")),
            version_id=UUID(_required_string(raw_item, "version_id")),
            score=_required_float(raw_item, "score"),
            content=_required_string(raw_item, "content"),
            title=_required_string(raw_item, "title"),
            document_type=_required_string(raw_item, "document_type"),
            section_path=_optional_string(raw_item, "section_path"),
            page_number=_optional_integer(raw_item, "page_number"),
            confidentiality=Confidentiality(_required_string(raw_item, "confidentiality")),
            language=_required_string(raw_item, "language"),
            external_llm_allowed=_required_boolean(raw_item, "external_llm_allowed"),
            token_count=_required_integer(raw_item, "token_count"),
            chunk_index=_required_integer(raw_item, "chunk_index"),
        )

    def _contract_error(self, message: str) -> NoReturn:
        """集中建立 dependency contract exception；NoReturn 表示此函式一定 raise。"""

        raise DependencyContractError(_DEPENDENCY, message)


def _required_string(payload: dict[str, Any], field: str) -> str:
    """讀取必要非空白字串；缺欄位時 dict access 會拋出 KeyError。"""

    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be a non-blank string")
    return value


def _optional_string(payload: dict[str, Any], field: str) -> str | None:
    """讀取可以是 null 的非空白字串。"""

    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{field} must be null or a non-blank string")
    return value


def _required_boolean(payload: dict[str, Any], field: str) -> bool:
    """嚴格接受 bool；Python 中 bool 是 int subclass，所以不能只做數字檢查。"""

    value = payload[field]
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _required_integer(payload: dict[str, Any], field: str) -> int:
    """嚴格接受整數，明確排除 True/False。"""

    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return int(value)


def _optional_integer(payload: dict[str, Any], field: str) -> int | None:
    """讀取 nullable integer。"""

    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be null or an integer")
    return int(value)


def _required_float(payload: dict[str, Any], field: str) -> float:
    """接受 int/float，再拒絕 NaN 與 infinity。"""

    value = payload[field]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise TypeError(f"{field} must be finite")
    return converted
