"""跨 async call chain 保存 request ID。

ContextVar 像每個 request 自己擁有的 context-local 變數；不同 coroutine 不會互相覆蓋。
後續 HTTP adapter 可從這裡讀取同一 request ID 並傳給下游服務。
"""

from __future__ import annotations

from contextvars import ContextVar

request_id_context: ContextVar[str | None] = ContextVar(
    "chat_request_id",
    default=None,
)
