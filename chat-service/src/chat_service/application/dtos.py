"""Application layer 的 framework-free commands、results 與 gateway DTO。

DTO 使用標準庫 dataclass，讓 use case 不必知道 FastAPI request 或 Pydantic schema。公開 view
刻意只放安全欄位；owner identity、machine UUID、restricted raw content 與 attestation 只在
需要的 internal DTO 中短暫存在，不能直接被 presentation 當成 employee response。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar, cast
from uuid import UUID

from chat_service.domain.entities import (
    AllowedEvidence,
    AnswerPart,
    ChatTurn,
    Citation,
    GenerationDiagnostics,
    Message,
    Principal,
    PurgePreview,
    PurgeSelector,
    RestrictedEvidence,
    TurnFailure,
)
from chat_service.domain.enums import (
    AnswerStatus,
    ConversationStatus,
    RetrievalStatus,
)
from chat_service.domain.exceptions import DomainValidationError, InvalidCursorError


def _text(value: str, field_name: str, maximum: int) -> None:
    """DTO 邊界只回報欄位名稱，不把 query 或其他敏感值放入錯誤訊息。"""

    if not isinstance(value, str) or len(value) == 0 or len(value) > maximum:
        raise DomainValidationError(f"{field_name} is outside the allowed range")
    if not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank")


def _optional_text(value: str | None, field_name: str, maximum: int) -> None:
    """驗證 nullable bounded text。"""

    if value is not None:
        _text(value, field_name, maximum)


def _positive(value: int, field_name: str) -> None:
    """驗證 strict positive integer。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainValidationError(f"{field_name} must be a positive integer")


def _non_negative(value: int, field_name: str) -> None:
    """驗證可為零的計數欄位。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainValidationError(f"{field_name} must be a non-negative integer")


def _aware(value: datetime, field_name: str) -> None:
    """Cursor 與 purge DTO 不接受 naive datetime。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidCursorError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class IdentityInput:
    """Presentation 轉交給 identity port 的純資料，不依賴 FastAPI Request。"""

    bearer_token: str | None = None
    development_user_id: str | None = None
    development_group_ids: tuple[str, ...] = ()
    development_roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CreateConversationCommand:
    """建立 conversation 的 application command。"""

    title: str | None = None

    def __post_init__(self) -> None:
        _optional_text(self.title, "title", 200)


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Employee API 可看的 conversation view，不包含 owner identity。"""

    conversation_id: UUID
    title: str | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


@dataclass(frozen=True, slots=True)
class GetConversationQuery:
    """取得單一 conversation 的 owner-scoped query。"""

    principal: Principal
    conversation_id: UUID


@dataclass(frozen=True, slots=True)
class ListConversationsQuery:
    """以 keyset cursor 查詢 owner 自己的 conversations。"""

    principal: Principal
    limit: int
    cursor: ConversationCursorPayload | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 100:
            raise DomainValidationError("conversation limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class ListConversationsResult:
    """conversation pagination result；cursor 本身仍是 typed payload。"""

    items: tuple[ConversationSummary, ...]
    next_cursor: ConversationCursorPayload | None


class _Unset:
    """表示 PATCH 欄位未提供，與明確提供 null 不同。"""


UNSET = _Unset()


@dataclass(frozen=True, slots=True)
class UpdateConversationCommand:
    """更新 title/status 的 command；至少要提供一個欄位。"""

    principal: Principal
    conversation_id: UUID
    title: str | _Unset | None = UNSET
    status: ConversationStatus | None = None

    def __post_init__(self) -> None:
        if self.title is UNSET and self.status is None:
            raise DomainValidationError("conversation update requires a field")
        if self.title is not UNSET:
            _optional_text(cast(str | None, self.title), "title", 200)
        if self.status is not None and not isinstance(self.status, ConversationStatus):
            raise DomainValidationError("status is invalid")


@dataclass(frozen=True, slots=True)
class SubmitTurnCommand:
    """建立或重播 turn 的 command；identity 來自 verified Principal 而非 body。"""

    principal: Principal
    conversation_id: UUID
    idempotency_key: str
    question: str
    language: str | None
    request_id: str

    def __post_init__(self) -> None:
        _text(self.idempotency_key, "idempotency_key", 64)
        _text(self.question, "question", 20_000)
        _optional_text(self.language, "language", 20)
        _text(self.request_id, "request_id", 128)


@dataclass(frozen=True, slots=True)
class GetTurnQuery:
    """owner-scoped turn query。"""

    principal: Principal
    conversation_id: UUID
    turn_id: UUID


@dataclass(frozen=True, slots=True)
class ListMessagesQuery:
    """依 message ordinal ASC 執行 keyset pagination 的 query。"""

    principal: Principal
    conversation_id: UUID
    limit: int
    cursor: MessageCursorPayload | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > 100:
            raise DomainValidationError("message limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class TurnResult:
    """內部 use case 使用的 turn/message 組合；presentation 另做安全投影。"""

    turn: ChatTurn
    user_message: Message
    assistant_message: Message | None


@dataclass(frozen=True, slots=True)
class GetTurnResult:
    """包含 public-safe turn result 的 application response。"""

    result: TurnResult
    error: SafeError | None = None

    def __post_init__(self) -> None:
        failure = self.result.turn.failure
        if failure is None and self.error is not None:
            raise DomainValidationError("a non-failed turn cannot expose a safe error")
        if failure is not None:
            if self.error is None:
                raise DomainValidationError("a failed turn requires its replay-safe error")
            if self.error != SafeError.from_turn_failure(failure):
                raise DomainValidationError("turn failure and replay-safe error must match")


@dataclass(frozen=True, slots=True)
class ListMessagesResult:
    """message pagination result。"""

    items: tuple[Message, ...]
    next_cursor: MessageCursorPayload | None


@dataclass(frozen=True, slots=True)
class RetrievalRequest:
    """Retrieval gateway request；不允許呼叫端注入 URL、key 或 retrieval limit。"""

    query: str
    limit: int
    language: str | None
    principal: Principal
    request_id: str

    def __post_init__(self) -> None:
        _text(self.query, "processed_query", 20_000)
        if self.limit < 1 or self.limit > 20:
            raise DomainValidationError("retrieval limit must be between 1 and 20")
        _optional_text(self.language, "language", 20)
        _text(self.request_id, "request_id", 128)


@dataclass(frozen=True, slots=True)
class RetrievalGatewayResult:
    """已通過 gateway contract 的 Retrieval 結果；restricted 只保留安全投影。"""

    status: RetrievalStatus
    llm_context: tuple[AllowedEvidence, ...]
    restricted_evidence: tuple[RestrictedEvidence, ...]
    external_generation_blocked: bool
    context_attestation: str | None
    candidate_count: int
    selected_count: int
    model_id: str
    model_revision: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, RetrievalStatus):
            raise DomainValidationError("retrieval status is invalid")
        _non_negative(self.candidate_count, "candidate_count")
        _non_negative(self.selected_count, "selected_count")
        _text(self.model_id, "model_id", 512)
        _text(self.model_revision, "model_revision", 512)
        _optional_text(self.context_attestation, "context_attestation", 8192)


@dataclass(frozen=True, slots=True)
class AnswerGenerationRequest:
    """Answer gateway 的 allowlist projection，只有 llm_context 可進入 evidence。"""

    question: str
    language: str | None
    evidence: tuple[AllowedEvidence, ...]
    context_attestation: str
    request_id: str

    def __post_init__(self) -> None:
        _text(self.question, "question", 20_000)
        _optional_text(self.language, "language", 20)
        if not self.evidence or len(self.evidence) > 20:
            raise DomainValidationError("answer evidence count is outside the allowed range")
        _text(self.context_attestation, "context_attestation", 8192)
        _text(self.request_id, "request_id", 128)


@dataclass(frozen=True, slots=True)
class AnswerGenerationResult:
    """Answer gateway 回傳的正常 contract result。"""

    status: AnswerStatus
    answer_parts: tuple[AnswerPart, ...]
    citations: tuple[Citation, ...]
    diagnostics: GenerationDiagnostics

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnswerStatus):
            raise DomainValidationError("answer status is invalid")


@dataclass(frozen=True, slots=True)
class PreviewConversationPurgeCommand:
    """建立 admin purge preview 的 command。"""

    principal: Principal
    selector: PurgeSelector
    request_id: str

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id", 128)


@dataclass(frozen=True, slots=True)
class ExecuteConversationPurgeCommand:
    """以 preview ID 與必填 reason 執行 hard purge。"""

    principal: Principal
    preview_id: UUID
    reason: str
    request_id: str

    def __post_init__(self) -> None:
        _text(self.reason, "purge_reason", 500)
        _text(self.request_id, "request_id", 128)


@dataclass(frozen=True, slots=True)
class PurgePreviewResult:
    """只回 preview metadata，不回被選 conversation 內容。"""

    preview: PurgePreview
    busy_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.preview, PurgePreview):
            raise DomainValidationError("purge preview is invalid")
        _non_negative(self.busy_count, "busy_count")


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """purge 完成後的安全計數。"""

    purge_id: UUID
    conversation_count: int
    message_count: int
    purged_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationCursorPayload:
    """strict conversation keyset payload；編碼器留給後續 presentation/application phase。"""

    version: int
    last_activity_at: datetime
    conversation_id: UUID

    def __post_init__(self) -> None:
        if self.version != 1:
            raise InvalidCursorError("unsupported conversation cursor version")
        _aware(self.last_activity_at, "last_activity_at")
        if not isinstance(self.conversation_id, UUID):
            raise InvalidCursorError("conversation cursor id must be a UUID")


@dataclass(frozen=True, slots=True)
class MessageCursorPayload:
    """strict message keyset payload，ordinal 必須是正整數。"""

    version: int
    ordinal: int

    def __post_init__(self) -> None:
        if self.version != 1:
            raise InvalidCursorError("unsupported message cursor version")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal <= 0:
            raise InvalidCursorError("message cursor ordinal must be positive")


@dataclass(frozen=True, slots=True)
class TurnAndUserMessageOrdinalAllocation:
    """
    Conversation row lock 內的組合 allocation 結果。

    Repository 必須在同一 transaction 同時增加兩個 counter，但 row_version
    只增加一次；這個 DTO 讓 Phase 5 無需接觸 ORM row。
    """

    turn_ordinal: int
    user_message_ordinal: int
    row_version: int

    def __post_init__(self) -> None:
        _positive(self.turn_ordinal, "turn_ordinal")
        _positive(self.user_message_ordinal, "user_message_ordinal")
        _positive(self.row_version, "row_version")


@dataclass(frozen=True, slots=True)
class MessageOrdinalAllocation:
    """Conversation row lock 內的單一 message ordinal allocation 結果。"""

    message_ordinal: int
    row_version: int

    def __post_init__(self) -> None:
        _positive(self.message_ordinal, "message_ordinal")
        _positive(self.row_version, "row_version")


T = TypeVar("T")
C = TypeVar("C")


@dataclass(frozen=True, slots=True)
class PaginationResult(Generic[T, C]):
    """供 repository/use case 共用的 typed keyset page。"""

    items: tuple[T, ...]
    next_cursor: C | None


@dataclass(frozen=True, slots=True)
class SafeError:
    """可回給 employee API 的錯誤；不保存 downstream detail。"""

    code: str
    message: str
    retryable: bool
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        _text(self.code, "error_code", 100)
        _text(self.message, "error_message", 2_000)
        if not isinstance(self.retryable, bool):
            raise DomainValidationError("retryable must be boolean")
        if self.retry_after_seconds is not None:
            _non_negative(self.retry_after_seconds, "retry_after_seconds")

    @classmethod
    def from_turn_failure(cls, failure: TurnFailure) -> SafeError:
        """將 DB 可重播 failure 投影成 application-safe error，不補入任何 detail。"""

        if not isinstance(failure, TurnFailure):
            raise DomainValidationError("turn failure is invalid")
        return cls(
            code=failure.safe_error_code,
            message=failure.safe_error_message,
            retryable=failure.error_retryable,
            retry_after_seconds=failure.retry_after_seconds,
        )


@dataclass(frozen=True, slots=True)
class CapacityLease:
    """Bounded capacity gate 的 opaque lease handle。"""

    lease_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.lease_id, UUID):
            raise DomainValidationError("lease_id must be a UUID")
