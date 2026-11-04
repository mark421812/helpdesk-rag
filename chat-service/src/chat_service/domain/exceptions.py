"""Chat Service Domain 的 framework-free exception hierarchy。

Domain 只表達規則被違反的種類，不知道 HTTP status、FastAPI response 或下游 raw error。
Presentation/Application 後續再把這些安全的例外類型映射成規格要求的 API 行為。
"""


class DomainError(Exception):
    """所有 Domain/Application core 錯誤的基底。"""


class DomainValidationError(DomainError):
    """輸入值不符合長度、格式或必要欄位規則。"""


class UnauthorizedPrincipalError(DomainError):
    """Principal 缺失、格式不合法或無法由目前 identity adapter 驗證。"""


class AdminRequiredError(DomainError):
    """目前 Principal 沒有執行管理操作所需的固定角色。"""


class DomainInvariantError(DomainError):
    """已建立的 Domain object 會違反不可變的業務 invariant。"""


class DependencyError(DomainError):
    """下游 port 回報的安全、可分類錯誤。

    Application 只依賴這個 framework-free contract，不需要 import HTTPX 或任何
    Infrastructure adapter。`code` 與 `retry_after_seconds` 都是已由 adapter 清理過的
    machine-safe 欄位；絕不可把 downstream raw body 放進來。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        self.code = code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class InvalidStateTransitionError(DomainInvariantError):
    """Turn 嘗試走不存在或已離開 terminal state 的 transition。"""


class InvalidIdempotencyKeyError(DomainValidationError):
    """Idempotency-Key 不是規格要求的 UUID 表示。"""


class ArchivedConversationError(DomainInvariantError):
    """Archived conversation 不接受新的 turn。"""


class OwnershipError(DomainInvariantError):
    """Principal 不是資源 owner；比較必須同時包含 issuer 與 subject。"""


class InvalidCursorError(DomainValidationError):
    """Cursor payload 的 version、timestamp、UUID 或 ordinal 不合法。"""


class ConflictError(DomainError):
    """可安全轉換成 conflict response 的 core 錯誤基底。"""


class IdempotencyConflictError(ConflictError):
    """Idempotency unique race 或同 key 但 fingerprint 不同。"""


class PurgeError(DomainError):
    """兩階段 purge 不包含 HTTP/framework detail 的錯誤基底。"""


class PurgeSelectionTooLargeError(DomainValidationError):
    """Preview selector 命中超過 1,000 筆，不得截斷後繼續。"""


class PurgePreviewNotFoundError(PurgeError):
    """Preview 不存在或 creator identity 不符，兩者對外不區分。"""


class PurgePreviewExpiredError(PurgeError):
    """Preview 已超過 expires_at。"""


class PurgePreviewConsumedError(ConflictError, PurgeError):
    """Preview 已被成功 execute，不得重複使用。"""


class PurgePreviewStaleError(ConflictError, PurgeError):
    """Preview 後的 ID/row-version snapshot 已變更。"""


class ConversationBusyError(ConflictError, PurgeError):
    """Purge 選取範圍仍有 processing turn。"""
