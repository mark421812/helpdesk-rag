"""Web Chat 的純 ASGI 安全 middleware。

ASGI middleware 在 request body 被 FastAPI 讀取前計算實際 byte 數，不能只信 Content-Length；同時
限制外站 Origin、加入安全 response headers 與 server-generated request ID。這些 middleware 不記錄
body，也不把 user question 或 downstream details 放進例外。
"""

from __future__ import annotations

import json
from collections.abc import MutableMapping
from typing import Any
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _header(scope: Scope, name: bytes) -> str | None:
    """從 ASGI header list 取單一值，避免把整個 header map 寫入 log。"""

    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return bytes(value).decode("latin-1")
    return None


def _json_error(
    status_code: int,
    error: str,
    message: str,
    *,
    request_id: str | None = None,
) -> list[Message]:
    """建立固定 error body；不含 request input 或 exception 內容。"""

    body = json.dumps(
        {"error": error, "message": message, "request_id": request_id, "details": None},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (
            b"content-security-policy",
            b"default-src 'self'; script-src 'self'; style-src 'self'; "
            b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        ),
        (b"cache-control", b"no-store"),
    ]
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode("ascii")))
    return [
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        },
        {"type": "http.response.body", "body": body},
    ]


class RequestSecurityMiddleware:
    """限制 request body/origin 並補上固定 security headers。"""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int, allowed_origins: set[str]) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_state: MutableMapping[str, Any] = scope.setdefault("state", {})
        request_state["request_id"] = str(uuid4())
        origin = _header(scope, b"origin")
        if origin and origin not in self.allowed_origins:
            for message in _json_error(
                403,
                "origin_not_allowed",
                "This origin is not allowed.",
                request_id=request_state["request_id"],
            ):
                await send(message)
            return

        method = scope.get("method", "").upper()
        content_type = _header(scope, b"content-type")
        if scope.get("path", "").startswith("/api/") and method in {"POST", "PATCH", "PUT"}:
            if (
                not content_type
                or content_type.split(";", 1)[0].strip().casefold() != "application/json"
            ):
                for message in _json_error(
                    415,
                    "unsupported_media_type",
                    "JSON request body is required.",
                    request_id=request_state["request_id"],
                ):
                    await send(message)
                return

        bytes_seen = 0
        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = self.max_body_bytes + 1
            if declared_length > self.max_body_bytes:
                for message in _json_error(
                    413,
                    "request_too_large",
                    "The request body is too large.",
                    request_id=request_state["request_id"],
                ):
                    await send(message)
                return

        async def limited_receive() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                bytes_seen += len(chunk)
                if bytes_seen > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _ in headers}
                security_headers = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
                    ),
                    (b"cache-control", b"no-store"),
                ]
                headers.extend(item for item in security_headers if item[0] not in existing)
                if b"x-request-id" not in existing:
                    headers.append((b"x-request-id", request_state["request_id"].encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, limited_receive, secured_send)
        except _BodyTooLarge:
            for message in _json_error(
                413,
                "request_too_large",
                "The request body is too large.",
                request_id=request_state["request_id"],
            ):
                await send(message)


class _BodyTooLarge(Exception):
    """Internal control-flow marker; raw request body never leaves middleware."""
