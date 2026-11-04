"""跨 async call chain 保存 request ID。

ContextVar 類似「每個 request 各自擁有的全域變數」；不同 coroutine 不會互相覆蓋，
因此 HTTP adapter 可以自動把目前 request ID 傳給下游服務。
"""

from __future__ import annotations

from contextvars import ContextVar

# 方括號是 generic type：這個 ContextVar 只能保存 ``str`` 或 ``None``。
request_id_context: ContextVar[str | None] = ContextVar(
    "retrieval_request_id",
    default=None,
)
