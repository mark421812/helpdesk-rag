"""在 FastAPI 解析前限制串流 request body 大小。"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import cast

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BoundedRequestBodyMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int, path_prefix: str = "/api/") -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith(self._path_prefix):
            await self._app(scope, receive, send)
            return
        content_length = _header(scope, b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    await self._reject(scope, receive, send)
                    return
            except ValueError:
                pass

        messages: list[Message] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > self._max_bytes:
                await self._reject(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        index = 0

        async def replay() -> Message:
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope.get("state")
        request_id = state.get("request_id") if isinstance(state, MutableMapping) else None
        response = JSONResponse(
            status_code=413,
            content={
                "error": "request_too_large",
                "message": f"Request body exceeds the configured {self._max_bytes}-byte limit",
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None
