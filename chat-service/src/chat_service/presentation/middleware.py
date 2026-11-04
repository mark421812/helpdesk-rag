"""Chat Service 的純 ASGI request middleware。

ASGI application 是 ``async (scope, receive, send)`` callable；這裡直接包裝 receive/send，
因此可以在 FastAPI 解析 JSON 前逐段限制 body bytes，而不依賴只看 ``Content-Length`` 的
BaseHTTPMiddleware。另一個 middleware 負責 bounded request ID、ContextVar、security headers
與低 cardinality HTTP metrics；它不會把 query、body、identity 或 resource ID 寫入 log。
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import Any, cast
from uuid import uuid4

import structlog
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from chat_service.infrastructure.request_context import request_id_context

logger = structlog.get_logger(__name__)


class BoundedRequestBodyMiddleware:
    """在大小上限內 buffer/replay body，不使用 Starlette 私有屬性。"""

    def __init__(self, app: ASGIApp, *, max_bytes: int, path_prefix: str = "/") -> None:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        self._app = app
        self._max_bytes = max_bytes
        self._path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI middleware 入口；request body 超限時在 parser 前安全拒絕。"""

        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self._path_prefix):
            await self._app(scope, receive, send)
            return

        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                too_large = int(content_length) > self._max_bytes
            except (TypeError, ValueError):
                too_large = False
            if too_large:
                await self._reject(scope, receive, send)
                return

        # ASGI body 可能分成多個 http.request message，因此必須逐段累計；只保存 bytes，不
        # 保存不受數量限制的 message list，避免攻擊者用大量空 chunk 放大記憶體。
        body = bytearray()
        terminal_message: Message | None = None
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                terminal_message = message
                break
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            """把已驗證大小的 body 以單一 request message 交給內層 FastAPI。"""

            nonlocal replayed
            if terminal_message is not None and not replayed:
                replayed = True
                return terminal_message
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        """不讓過大 body 繼續進入 request parser。"""

        state = scope.get("state")
        request_id = state.get("request_id") if isinstance(state, MutableMapping) else None
        response = JSONResponse(
            status_code=413,
            content={
                "error": "request_too_large",
                "message": "Request body exceeds the configured size limit",
                "request_id": request_id,
                "details": None,
            },
        )
        await response(scope, receive, send)


class RequestContextMiddleware:
    """建立 request ID/context，並補上不依賴 body 的安全 response headers。"""

    def __init__(self, app: ASGIApp, *, metrics: Any | None = None) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """以 send wrapper 注入 request ID header，並在 finally 記錄低 cardinality metrics。"""

        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        request_id = _request_id_from_scope(scope)
        if isinstance(state, MutableMapping):
            state["request_id"] = request_id
        token = request_id_context.set(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_headers(message: Message) -> None:
            """在 response start 加入 request ID 與基本安全 headers。"""

            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                headers = list(message.get("headers", []))
                _append_header(headers, b"x-request-id", request_id.encode("ascii"))
                _append_header(headers, b"x-content-type-options", b"nosniff")
                _append_header(headers, b"x-frame-options", b"DENY")
                _append_header(headers, b"referrer-policy", b"no-referrer")
                _append_header(headers, b"cache-control", b"no-store")
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_headers)
        except Exception as exc:
            # FastAPI exception handlers normally consume expected exceptions.  Unexpected errors
            # are logged by type only; raw exception text/body is deliberately not emitted here.
            logger.exception("unhandled_http_exception", error_type=type(exc).__name__)
            if not response_started:
                response = JSONResponse(
                    status_code=500,
                    content={
                        "error": "internal_error",
                        "message": "Unexpected server error",
                        "request_id": request_id,
                        "details": None,
                    },
                )
                await response(scope, receive, send_with_headers)
            else:
                raise
        finally:
            route = scope.get("route")
            route_template = str(getattr(route, "path", "__unmatched__"))
            if self._metrics is not None:
                self._metrics.observe_http(
                    method=_metric_method(scope.get("method")),
                    route=route_template,
                    status=str(status_code),
                    elapsed_seconds=time.perf_counter() - started,
                )
            structlog.contextvars.clear_contextvars()
            request_id_context.reset(token)


def _request_id_from_scope(scope: Scope) -> str:
    """只接受長度與 printable ASCII 都合法的 request ID，否則產生 UUID。"""

    raw = _header(scope, b"x-request-id")
    if raw is None:
        return str(uuid4())
    value = raw.strip()
    if 0 < len(value) <= 128 and all(0x20 <= ord(char) <= 0x7E for char in value):
        return value
    return str(uuid4())


def _metric_method(value: object) -> str:
    """把非本服務路由使用的方法壓到固定 label，避免任意 method 造成 cardinality 爆炸。"""

    method = value if isinstance(value, str) else ""
    return method if method in {"GET", "POST", "PATCH", "OPTIONS", "HEAD"} else "OTHER"


def _header(scope: Scope, name: bytes) -> str | None:
    """從 ASGI bytes headers 讀取第一個同名 header。"""

    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None


def _append_header(headers: list[tuple[bytes, bytes]], name: bytes, value: bytes) -> None:
    """以 middleware 的 canonical value 覆蓋下游同名 header。"""

    lowered = name.lower()
    headers[:] = [
        (existing, existing_value)
        for existing, existing_value in headers
        if existing.lower() != lowered
    ]
    headers.append((name, value))
