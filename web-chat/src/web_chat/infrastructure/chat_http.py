"""Chat Service 的 HTTP adapter。

Adapter 是唯一知道 HTTPX、下游 header、timeout 和 wire schema 的元件。它以 lifespan 共享的
AsyncClient 呼叫固定 base URL；所有錯誤都轉為不含 raw body/URL/secret 的 GatewayFailure。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, TypeVar
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from web_chat.application.dtos import GatewayFailure, GatewayResult
from web_chat.application.ports import ChatGatewayPort
from web_chat.infrastructure.chat_schemas import ConversationWire, HealthWire, TurnWire
from web_chat.infrastructure.config import Settings

WireModelT = TypeVar("WireModelT", bound=BaseModel)

_JSON_CONTENT_TYPE = "application/json"
_MAX_RETRY_AFTER_SECONDS = 86_400
_ERRORS: dict[int, tuple[int, str, str]] = {
    401: (401, "chat_unauthorized", "Demo identity was rejected by Chat Service."),
    404: (404, "chat_not_found", "The requested chat resource was not found."),
    409: (409, "chat_conflict", "The chat operation conflicts with its current state."),
    413: (413, "chat_request_too_large", "The chat request is too large."),
    422: (422, "chat_invalid_request", "The chat request is invalid."),
    429: (429, "chat_capacity_exceeded", "Chat Service is busy. Please try again later."),
    502: (502, "chat_dependency_error", "A chat dependency could not complete the request."),
    503: (503, "chat_unavailable", "Chat Service is temporarily unavailable."),
    504: (504, "chat_timeout", "Chat Service did not finish in time."),
}


class ChatHttpGateway(ChatGatewayPort):
    """透過既有 Chat employee API 完成三個最小操作。"""

    def __init__(self, *, client: httpx.AsyncClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def create_conversation(self, *, title: str | None, request_id: str) -> GatewayResult:
        """建立 conversation；建立失敗不會自行重試以免產生多筆對話。"""

        return await self._request_json(
            method="POST",
            path="/api/v1/conversations",
            request_id=request_id,
            json_body={"title": title},
            expected_status=201,
            response_model=ConversationWire,
        )

    async def submit_turn(
        self,
        *,
        conversation_id: UUID,
        idempotency_key: str,
        question: str,
        language: str | None,
        request_id: str,
    ) -> GatewayResult:
        """送出 turn；POST 明確禁止 retry，重試由相同 idempotency key 的使用者操作完成。"""

        return await self._request_json(
            method="POST",
            path=f"/api/v1/conversations/{conversation_id}/turns",
            request_id=request_id,
            json_body={"question": question, "language": language},
            expected_statuses={200, 202},
            response_model=TurnWire,
            extra_headers={"Idempotency-Key": idempotency_key},
        )

    async def get_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        request_id: str,
    ) -> GatewayResult:
        """以已驗證 UUID 建立固定 status path，不追蹤 response 內的任意 URL。"""

        return await self._request_json(
            method="GET",
            path=f"/api/v1/conversations/{conversation_id}/turns/{turn_id}",
            request_id=request_id,
            json_body=None,
            expected_status=200,
            response_model=TurnWire,
        )

    async def readiness(self, *, request_id: str) -> bool:
        """短 timeout、fail-closed 地檢查 Chat readiness；不發出任何 turn。"""

        try:
            result = await self._request_json(
                method="GET",
                path="/health/ready",
                request_id=request_id,
                json_body=None,
                expected_status=200,
                response_model=HealthWire,
                timeout_seconds=self._settings.readiness_timeout_seconds,
            )
        except GatewayFailure:
            return False
        payload = result.payload
        checks = payload.get("checks")
        return (
            payload.get("status") == "ok"
            and isinstance(checks, dict)
            and all(value is True for value in checks.values())
        )

    async def _request_json(
        self,
        *,
        method: str,
        path: str,
        request_id: str,
        json_body: Mapping[str, object] | None,
        response_model: type[WireModelT],
        expected_status: int | None = None,
        expected_statuses: set[int] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> GatewayResult:
        """執行單次 request 並在 trust boundary 內完成 body/content-type/schema 驗證。"""

        expected = {expected_status} if expected_status is not None else expected_statuses
        if not expected:
            raise ValueError("at least one expected status is required")
        headers = {
            "Accept": _JSON_CONTENT_TYPE,
            "x-request-id": request_id,
            "x-dev-user-id": self._settings.demo_user_id,
            "x-dev-group-ids": self._settings.group_header,
        }
        if json_body is not None:
            headers["Content-Type"] = _JSON_CONTENT_TYPE
        if extra_headers:
            headers.update(extra_headers)
        timeout = timeout_seconds or self._settings.upstream_timeout_seconds
        url = f"{self._settings.chat_service_url}{path}"

        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=httpx.Timeout(
                        timeout,
                        connect=min(10.0, timeout),
                        read=timeout,
                        write=min(30.0, timeout),
                        pool=min(10.0, timeout),
                    ),
                ) as response:
                    raw_body = await self._read_limited_body(response)
                    if not self._is_json_content_type(response.headers.get("content-type")):
                        raise GatewayFailure(
                            status_code=502,
                            error="chat_contract_error",
                            message="Chat Service returned an invalid response.",
                        )
                    if response.status_code not in expected:
                        raise self._map_status(response.status_code, response.headers)
                    payload = self._decode_payload(raw_body)
                    parsed = response_model.model_validate(payload)
                    return GatewayResult(
                        status_code=response.status_code,
                        payload=parsed.model_dump(mode="json"),
                        retry_after_seconds=self._retry_after(response.headers),
                    )
        except GatewayFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise GatewayFailure(
                status_code=504,
                error="chat_timeout",
                message="Chat Service did not finish in time.",
            ) from None
        except (httpx.HTTPError, OSError):
            raise GatewayFailure(
                status_code=503,
                error="chat_unavailable",
                message="Chat Service is temporarily unavailable.",
            ) from None
        except (ValueError, ValidationError, json.JSONDecodeError):
            raise GatewayFailure(
                status_code=502,
                error="chat_contract_error",
                message="Chat Service returned an invalid response.",
            ) from None

    async def _read_limited_body(self, response: httpx.Response) -> bytes:
        """不信任 Content-Length，逐 chunk 限制 response body 以避免記憶體 DoS。"""

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._settings.upstream_max_response_body_bytes:
                    raise GatewayFailure(
                        status_code=502,
                        error="chat_contract_error",
                        message="Chat Service returned an invalid response.",
                    )
            except ValueError:
                raise GatewayFailure(
                    status_code=502,
                    error="chat_contract_error",
                    message="Chat Service returned an invalid response.",
                ) from None
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > self._settings.upstream_max_response_body_bytes:
                raise GatewayFailure(
                    status_code=502,
                    error="chat_contract_error",
                    message="Chat Service returned an invalid response.",
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _is_json_content_type(value: str | None) -> bool:
        """只接受 application/json（允許 charset 參數）。"""

        if not value:
            return False
        return value.split(";", 1)[0].strip().casefold() == _JSON_CONTENT_TYPE

    @staticmethod
    def _decode_payload(raw_body: bytes) -> Any:
        """解析 JSON 但不把原始內容放進 exception 或 log。"""

        if not raw_body:
            raise ValueError("empty body")
        payload = json.loads(raw_body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("response must be a JSON object")
        return payload

    @staticmethod
    def _retry_after(headers: httpx.Headers) -> int | None:
        """只接受有界整數 Retry-After；非法值被忽略，不向瀏覽器傳遞。"""

        value = headers.get("retry-after")
        if value is None:
            return None
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        if not 0 <= parsed <= _MAX_RETRY_AFTER_SECONDS:
            return None
        return parsed

    @classmethod
    def _map_status(cls, status_code: int, headers: httpx.Headers) -> GatewayFailure:
        """把下游 HTTP status 映射成固定安全訊息；完全忽略 raw error body。"""

        status, error, message = _ERRORS.get(
            status_code,
            (502, "chat_service_error", "Chat Service returned an unexpected response."),
        )
        return GatewayFailure(
            status_code=status,
            error=error,
            message=message,
            retry_after_seconds=cls._retry_after(headers) if status_code == 429 else None,
        )
