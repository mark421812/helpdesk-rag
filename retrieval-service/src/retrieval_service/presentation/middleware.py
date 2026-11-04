"""直接實作 ASGI 的 bounded request-body middleware。

ASGI application 本質是 ``async callable(scope, receive, send)``。middleware 包住下一層
application，可以在 FastAPI 解析 JSON 前限制串流 body 大小，避免大量輸入耗盡記憶體。
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import cast

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BoundedRequestBodyMiddleware:
    """在大小上限內 buffer/replay body，不使用 Starlette 私有屬性。"""

    def __init__(self, app: ASGIApp, *, max_bytes: int, path_prefix: str = "/api/") -> None:
        self._app = app
        self._max_bytes = max_bytes
        self._path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI middleware 入口；``__call__`` 讓 instance 可以像函式一樣被呼叫。"""

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

        # ASGI body 可能分成多個 http.request message，因此必須逐段累計。
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
            """把已驗證大小的 messages 依原順序交給內層 FastAPI。"""

            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
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
                "message": (f"Request body exceeds the configured {self._max_bytes}-byte limit"),
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)


def _header(scope: Scope, name: bytes) -> str | None:
    """從 ASGI 的 bytes headers 讀取單一 header。"""

    headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    for raw_name, raw_value in headers:
        if raw_name.lower() == name:
            return raw_value.decode("latin-1")
    return None
