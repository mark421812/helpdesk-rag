"""Application DTOs shared by presentation and infrastructure.

DTO 是跨 adapter 傳遞資料的明確邊界；這裡只使用標準庫，避免 FastAPI、Pydantic 或 HTTPX
反向滲入 application。GatewayFailure 只攜帶對使用者安全的分類，不保存下游原始 body。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """已通過 wire schema 驗證的 JSON 投影與 HTTP 狀態。"""

    status_code: int
    payload: dict[str, object]
    retry_after_seconds: int | None = None


class GatewayFailure(Exception):
    """可安全映射成 Web Chat error envelope 的下游失敗。"""

    def __init__(
        self,
        *,
        status_code: int,
        error: str,
        message: str,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(error)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.retry_after_seconds = retry_after_seconds
