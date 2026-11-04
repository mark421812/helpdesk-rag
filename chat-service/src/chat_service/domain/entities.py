"""Chat Service 的 framework-free entities 與 value objects。

這些 dataclass 只保存可被 Domain 驗證的業務資料；它們不認識 Pydantic、ORM、HTTP 或
下游服務。`slots=True` 可避免 value object 被意外加入未規範欄位，`frozen=True` 則讓
跨服務 evidence/citation 在送入後續 use case 前不被原地修改。時間一律由外部 ClockPort
提供，entity 本身不呼叫系統時鐘。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Final
from uuid import UUID

from chat_service.domain.enums import (
    AnswerStatus,
    ChatOutcome,
    Confidentiality,
    ConversationStatus,
    InternalTurnState,
    MessageRole,
    PublicTurnState,
    RetrievalStatus,
    project_public_turn_state,
)
from chat_service.domain.exceptions import (
    ArchivedConversationError,
    DomainValidationError,
    InvalidStateTransitionError,
    OwnershipError,
)

MAX_TITLE_LENGTH: Final = 200
MAX_QUESTION_LENGTH: Final = 20_000
MAX_LANGUAGE_LENGTH: Final = 20
MAX_CITATION_TITLE_LENGTH: Final = 1_000
MAX_SECTION_PATH_LENGTH: Final = 2_000
MAX_REQUEST_ID_LENGTH: Final = 128
MAX_PURGE_REASON_LENGTH: Final = 500
MAX_SAFE_ERROR_MESSAGE_LENGTH: Final = 2_000
MAX_RETRY_AFTER_SECONDS: Final[int] = 86_400

PROCESSING_INTERRUPTED_ERROR_CODE: Final = "chat_processing_interrupted"
PROCESSING_INTERRUPTED_ERROR_MESSAGE: Final = (
    "先前的處理程序已中斷，無法確認是否完成；請使用新的請求重新送出。"  # noqa: RUF001
)


def _positive_integer(value: int, field_name: str) -> None:
    """驗證正整數；bool 雖然是 int subclass，但不能當 ordinal 使用。"""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainValidationError(f"{field_name} must be a positive integer")


def _non_negative_integer(value: int, field_name: str) -> None:
    """驗證可為零但不可為負的計數欄位。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainValidationError(f"{field_name} must be a non-negative integer")


def _uuid(value: UUID, field_name: str) -> None:
    """確保 core 不把尚未解析的字串誤當成 UUID。"""

    if not isinstance(value, UUID):
        raise DomainValidationError(f"{field_name} must be a UUID")


def _text(value: str, field_name: str, *, maximum: int, minimum: int = 1) -> None:
    """驗證 bounded text，但不把輸入值放入錯誤訊息。"""

    if not isinstance(value, str):
        raise DomainValidationError(f"{field_name} must be text")
    if len(value) < minimum or len(value) > maximum:
        raise DomainValidationError(f"{field_name} length is outside the allowed range")
    if not value.strip():
        raise DomainValidationError(f"{field_name} must not be blank")


def _optional_text(value: str | None, field_name: str, *, maximum: int) -> None:
    """驗證 nullable bounded text。"""

    if value is not None:
        _text(value, field_name, maximum=maximum)


def _aware_datetime(value: datetime, field_name: str) -> None:
    """只接受帶 timezone 的 datetime，避免不同資料庫 session 時區混算。"""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field_name} must be timezone-aware")


def _optional_aware_datetime(value: datetime | None, field_name: str) -> None:
    """驗證 nullable timezone-aware datetime。"""

    if value is not None:
        _aware_datetime(value, field_name)


def _unique_texts(values: tuple[str, ...], field_name: str, *, maximum: int) -> None:
    """驗證 header-derived tuple 沒有空白或重複識別值。"""

    for value in values:
        _text(value, field_name, maximum=maximum)
    if len(set(values)) != len(values):
        raise DomainValidationError(f"{field_name} must not contain duplicates")


@dataclass(frozen=True, slots=True)
class Principal:
    """由 identity adapter 驗證後傳入 core 的 owner identity。"""

    issuer: str
    subject: str
    tenant_id: str | None = None
    group_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.issuer, "issuer", maximum=255)
        _text(self.subject, "subject", maximum=512)
        _optional_text(self.tenant_id, "tenant_id", maximum=255)
        _unique_texts(self.group_ids, "group_id", maximum=512)
        _unique_texts(self.roles, "role", maximum=128)

    def owns(self, owner_issuer: str, owner_subject: str) -> bool:
        """只有 issuer 與 subject 同時相等才算 owner。"""

        return self.issuer == owner_issuer and self.subject == owner_subject


@dataclass(slots=True)
class Conversation:
    """Conversation aggregate root，包含安全 ownership 與 ordinal allocation invariant。"""

    id: UUID
    owner: Principal
    title: str | None = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    next_turn_ordinal: int = 1
    next_message_ordinal: int = 1
    row_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_activity_at: datetime | None = None

    def __post_init__(self) -> None:
        _uuid(self.id, "conversation_id")
        if not isinstance(self.owner, Principal):
            raise DomainValidationError("owner must be a Principal")
        if self.title is not None:
            _text(self.title, "title", maximum=MAX_TITLE_LENGTH)
        if not isinstance(self.status, ConversationStatus):
            raise DomainValidationError("status is invalid")
        _positive_integer(self.next_turn_ordinal, "next_turn_ordinal")
        _positive_integer(self.next_message_ordinal, "next_message_ordinal")
        _positive_integer(self.row_version, "row_version")
        _optional_aware_datetime(self.created_at, "created_at")
        _optional_aware_datetime(self.updated_at, "updated_at")
        _optional_aware_datetime(self.last_activity_at, "last_activity_at")

    def ensure_owner(self, principal: Principal) -> None:
        """以完整 owner identity 驗證存取；失敗時不透露資源是否存在。"""

        if not principal.owns(self.owner.issuer, self.owner.subject):
            raise OwnershipError("principal is not the conversation owner")

    def allocate_turn_ordinal(self) -> int:
        """以 aggregate counter 分配 turn ordinal，不使用 max(ordinal)+1。"""

        if self.status is ConversationStatus.ARCHIVED:
            raise ArchivedConversationError("archived conversations do not accept new turns")
        allocated = self.next_turn_ordinal
        self.next_turn_ordinal += 1
        self.row_version += 1
        return allocated

    def allocate_message_ordinal(self) -> int:
        """以 aggregate counter 分配 message ordinal，讓 persistence layer 可在 row lock 下使用。"""

        allocated = self.next_message_ordinal
        self.next_message_ordinal += 1
        self.row_version += 1
        return allocated

    def allocate_turn_and_user_message_ordinals(self) -> tuple[int, int]:
        """在同一個 transaction 分配 turn 與 user message ordinal。

        SubmitTurn 會在一個 conversation row lock 內同時建立這兩筆子資源；
        `row_version` 表示 transaction-level snapshot，所以這組合操作只增加一次。
        Infrastructure 不得分別呼叫兩個單項 allocation 後再寫回資料庫。
        """

        if self.status is ConversationStatus.ARCHIVED:
            raise ArchivedConversationError("archived conversations do not accept new turns")
        turn_ordinal = self.next_turn_ordinal
        message_ordinal = self.next_message_ordinal
        self.next_turn_ordinal += 1
        self.next_message_ordinal += 1
        self.row_version += 1
        return turn_ordinal, message_ordinal

    def rename(self, title: str | None) -> None:
        """更新 title 並增加 row version；None 代表清除標題。"""

        if title is not None:
            _text(title, "title", maximum=MAX_TITLE_LENGTH)
        self.title = title
        self.row_version += 1

    def set_status(self, status: ConversationStatus) -> None:
        """更新 active/archived 狀態，不讓 Domain 出現未規範值。"""

        if not isinstance(status, ConversationStatus):
            raise DomainValidationError("status is invalid")
        self.status = status
        self.row_version += 1


_ALLOWED_TRANSITIONS: Final[dict[InternalTurnState, frozenset[InternalTurnState]]] = {
    InternalTurnState.ACCEPTED: frozenset({InternalTurnState.WAITING_FOR_CAPACITY}),
    InternalTurnState.WAITING_FOR_CAPACITY: frozenset(
        {InternalTurnState.RETRIEVING, InternalTurnState.FAILED}
    ),
    InternalTurnState.RETRIEVING: frozenset(
        {
            InternalTurnState.GENERATING,
            InternalTurnState.COMPLETED,
            InternalTurnState.FAILED,
        }
    ),
    InternalTurnState.GENERATING: frozenset(
        {
            InternalTurnState.COMPLETED,
            InternalTurnState.FAILED,
            InternalTurnState.FAILED_UNKNOWN,
        }
    ),
    InternalTurnState.COMPLETED: frozenset(),
    InternalTurnState.FAILED: frozenset(),
    InternalTurnState.FAILED_UNKNOWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TurnFailure:
    """可永久保存並安全重播的 turn failure。

    這個 value object 只含 public-safe 錯誤，不允許 downstream body、URL、query、
    evidence 或 secret。HTTP mapping 仍屬於後續 Presentation phase。
    """

    response_http_status: int
    safe_error_code: str
    safe_error_message: str
    error_retryable: bool
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.response_http_status, bool)
            or not isinstance(self.response_http_status, int)
            or self.response_http_status < 400
            or self.response_http_status > 599
        ):
            raise DomainValidationError("response_http_status must be between 400 and 599")
        _text(self.safe_error_code, "safe_error_code", maximum=100)
        _text(
            self.safe_error_message,
            "safe_error_message",
            maximum=MAX_SAFE_ERROR_MESSAGE_LENGTH,
        )
        if not isinstance(self.error_retryable, bool):
            raise DomainValidationError("error_retryable must be boolean")
        if self.retry_after_seconds is not None:
            _non_negative_integer(self.retry_after_seconds, "retry_after_seconds")
            if self.retry_after_seconds > MAX_RETRY_AFTER_SECONDS:
                raise DomainValidationError("retry_after_seconds exceeds maximum")
            if not self.error_retryable:
                raise DomainValidationError(
                    "retry_after_seconds requires an error marked as retryable"
                )


@dataclass(frozen=True, slots=True)
class RetrievalDiagnostics:
    """Retrieval 可存檔的安全計數與 model revision，不含 evidence。"""

    candidate_count: int
    selected_count: int
    model_id: str
    model_revision: str

    def __post_init__(self) -> None:
        _non_negative_integer(self.candidate_count, "candidate_count")
        _non_negative_integer(self.selected_count, "selected_count")
        if self.selected_count > self.candidate_count:
            raise DomainValidationError("selected_count cannot exceed candidate_count")
        _text(self.model_id, "model_id", maximum=512)
        _text(self.model_revision, "model_revision", maximum=512)


@dataclass(frozen=True, slots=True)
class GenerationDiagnostics:
    """Generation 可存檔的安全診斷，不含 prompt/provider raw response。"""

    provider: str
    model: str
    prompt_version: str
    finish_reason: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def __post_init__(self) -> None:
        _text(self.provider, "provider", maximum=256)
        _text(self.model, "model", maximum=512)
        _text(self.prompt_version, "prompt_version", maximum=256)
        _text(self.finish_reason, "finish_reason", maximum=256)
        if self.prompt_tokens is not None:
            _non_negative_integer(self.prompt_tokens, "prompt_tokens")
        if self.completion_tokens is not None:
            _non_negative_integer(self.completion_tokens, "completion_tokens")


@dataclass(slots=True)
class ChatTurn:
    """單輪問題的 aggregate；public processing 是 state 的投影而不是新 persistence state。"""

    id: UUID
    conversation_id: UUID
    ordinal: int
    idempotency_key: str
    request_fingerprint: str
    original_question: str
    processed_query: str
    request_id: str
    language: str | None = None
    state: InternalTurnState = InternalTurnState.ACCEPTED
    retrieval_status: RetrievalStatus | None = None
    answer_status: AnswerStatus | None = None
    outcome: ChatOutcome | None = None
    failure: TurnFailure | None = None
    retrieval_diagnostics: RetrievalDiagnostics | None = None
    generation_diagnostics: GenerationDiagnostics | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _uuid(self.id, "turn_id")
        _uuid(self.conversation_id, "conversation_id")
        _positive_integer(self.ordinal, "ordinal")
        _text(self.idempotency_key, "idempotency_key", maximum=36)
        _text(self.request_fingerprint, "request_fingerprint", maximum=64)
        _text(self.original_question, "original_question", maximum=MAX_QUESTION_LENGTH)
        _text(self.processed_query, "processed_query", maximum=MAX_QUESTION_LENGTH)
        _text(self.request_id, "request_id", maximum=MAX_REQUEST_ID_LENGTH)
        _optional_text(self.language, "language", maximum=MAX_LANGUAGE_LENGTH)
        if not isinstance(self.state, InternalTurnState):
            raise DomainValidationError("turn state is invalid")
        if self.retrieval_status is not None and not isinstance(
            self.retrieval_status, RetrievalStatus
        ):
            raise DomainValidationError("retrieval status is invalid")
        if self.answer_status is not None and not isinstance(self.answer_status, AnswerStatus):
            raise DomainValidationError("answer status is invalid")
        if self.outcome is not None and not isinstance(self.outcome, ChatOutcome):
            raise DomainValidationError("chat outcome is invalid")
        if self.failure is not None and not isinstance(self.failure, TurnFailure):
            raise DomainValidationError("turn failure is invalid")
        if self.retrieval_diagnostics is not None and not isinstance(
            self.retrieval_diagnostics, RetrievalDiagnostics
        ):
            raise DomainValidationError("retrieval diagnostics are invalid")
        if self.generation_diagnostics is not None and not isinstance(
            self.generation_diagnostics, GenerationDiagnostics
        ):
            raise DomainValidationError("generation diagnostics are invalid")
        if self.state in {InternalTurnState.FAILED, InternalTurnState.FAILED_UNKNOWN}:
            if self.failure is None:
                raise DomainValidationError("failed turns require a safe failure")
        elif self.failure is not None:
            raise DomainValidationError("non-failed turns cannot contain a failure")
        _optional_aware_datetime(self.created_at, "created_at")
        _optional_aware_datetime(self.updated_at, "updated_at")
        _optional_aware_datetime(self.started_at, "started_at")
        _optional_aware_datetime(self.completed_at, "completed_at")

    @property
    def is_processing(self) -> bool:
        """回答是否仍在四種內部 processing phase 之一。"""

        return self.state in {
            InternalTurnState.ACCEPTED,
            InternalTurnState.WAITING_FOR_CAPACITY,
            InternalTurnState.RETRIEVING,
            InternalTurnState.GENERATING,
        }

    @property
    def public_state(self) -> PublicTurnState:
        """產生 public state 投影，不修改內部 state。"""

        return project_public_turn_state(self.state)

    def transition_to(self, state: InternalTurnState) -> None:
        """依規格流程前進；stale recovery 使用明確的 failed_unknown 方法。"""

        if state in {InternalTurnState.FAILED, InternalTurnState.FAILED_UNKNOWN}:
            raise InvalidStateTransitionError("failed transitions require a typed safe failure")
        if state not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransitionError(
                f"transition from {self.state.value} to {state.value} is not allowed"
            )
        self.state = state

    def fail(self, failure: TurnFailure, *, result_unknown: bool = False) -> None:
        """以 typed safe error 進入 failed/failed_unknown，供後續 idempotent replay。"""

        if not isinstance(failure, TurnFailure):
            raise DomainValidationError("turn failure is invalid")
        target = InternalTurnState.FAILED_UNKNOWN if result_unknown else InternalTurnState.FAILED
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise InvalidStateTransitionError(
                f"transition from {self.state.value} to {target.value} is not allowed"
            )
        self.state = target
        self.failure = failure

    def recover_as_unknown(self) -> None:
        """把仍在處理中的 turn 標記為無法確認 Provider 結果。"""

        if not self.is_processing:
            raise InvalidStateTransitionError("only processing turns can be recovered")
        self.state = InternalTurnState.FAILED_UNKNOWN
        self.failure = TurnFailure(
            response_http_status=503,
            safe_error_code=PROCESSING_INTERRUPTED_ERROR_CODE,
            safe_error_message=PROCESSING_INTERRUPTED_ERROR_MESSAGE,
            error_retryable=False,
        )

    @property
    def response_http_status(self) -> int | None:
        """ORM mapping 可用的 flattened failure status。"""

        return None if self.failure is None else self.failure.response_http_status

    @property
    def safe_error_code(self) -> str | None:
        """ORM mapping 可用的 flattened safe error code。"""

        return None if self.failure is None else self.failure.safe_error_code

    @property
    def safe_error_message(self) -> str | None:
        """ORM mapping 可用的 flattened safe error message。"""

        return None if self.failure is None else self.failure.safe_error_message

    @property
    def error_retryable(self) -> bool:
        """ORM mapping 可用的 flattened retryable flag。"""

        return False if self.failure is None else self.failure.error_retryable

    @property
    def retry_after_seconds(self) -> int | None:
        """ORM mapping 可用的 flattened retry delay。"""

        return None if self.failure is None else self.failure.retry_after_seconds

    def set_outcome(self, outcome: ChatOutcome) -> None:
        """只允許 terminal turn 保存 business outcome。"""

        if self.is_processing:
            raise InvalidStateTransitionError("processing turns cannot have a terminal outcome")
        if not isinstance(outcome, ChatOutcome):
            raise DomainValidationError("chat outcome is invalid")
        self.outcome = outcome


@dataclass(frozen=True, slots=True)
class AnswerPart:
    """回答段落；每段至少引用一個 public citation id。"""

    text: str
    citation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.text, "answer_part_text", maximum=200_000)
        if not self.citation_ids:
            raise DomainValidationError("answer part must contain a citation")
        _unique_texts(self.citation_ids, "citation_id", maximum=128)


@dataclass(frozen=True, slots=True)
class Citation:
    """安全 citation 加上 DB 稽核用 machine UUID；不含實體路徑或 URL。"""

    citation_id: str
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    section_path: str | None = None
    page_number: int | None = None

    def __post_init__(self) -> None:
        _text(self.citation_id, "citation_id", maximum=128)
        _uuid(self.chunk_id, "chunk_id")
        _uuid(self.document_id, "document_id")
        _uuid(self.version_id, "version_id")
        _text(self.title, "citation_title", maximum=MAX_CITATION_TITLE_LENGTH)
        _optional_text(self.section_path, "section_path", maximum=MAX_SECTION_PATH_LENGTH)
        if self.page_number is not None:
            _positive_integer(self.page_number, "page_number")


@dataclass(frozen=True, slots=True)
class Governance:
    """Evidence 的政策欄位；只有 allowed context 可進入 Answer gateway。"""

    confidentiality: Confidentiality
    language: str
    external_llm_allowed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.confidentiality, Confidentiality):
            raise DomainValidationError("confidentiality is invalid")
        _text(self.language, "evidence_language", maximum=MAX_LANGUAGE_LENGTH)
        if not isinstance(self.external_llm_allowed, bool):
            raise DomainValidationError("external_llm_allowed must be boolean")


@dataclass(frozen=True, slots=True)
class AllowedEvidence:
    """Retrieval llm_context 的可外送 evidence；content 僅存在於 allowed object。"""

    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: float
    content: str
    document_type: str
    token_count: int
    citation: Citation
    governance: Governance

    def __post_init__(self) -> None:
        _positive_integer(self.rank, "evidence_rank")
        _uuid(self.chunk_id, "chunk_id")
        _uuid(self.document_id, "document_id")
        _uuid(self.version_id, "version_id")
        if (
            not isinstance(self.score, (int, float))
            or isinstance(self.score, bool)
            or not isfinite(float(self.score))
        ):
            raise DomainValidationError("evidence score must be finite")
        _text(self.content, "evidence_content", maximum=200_000)
        _text(self.document_type, "document_type", maximum=100)
        _positive_integer(self.token_count, "token_count")
        if not isinstance(self.citation, Citation):
            raise DomainValidationError("citation is invalid")
        if not isinstance(self.governance, Governance):
            raise DomainValidationError("governance is invalid")
        if not self.governance.external_llm_allowed:
            raise DomainValidationError("restricted evidence cannot be an allowed evidence object")


@dataclass(frozen=True, slots=True)
class RestrictedEvidence:
    """Restricted evidence 的安全投影；刻意沒有 content 欄位。"""

    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    citation: Citation

    def __post_init__(self) -> None:
        _positive_integer(self.rank, "evidence_rank")
        _uuid(self.chunk_id, "chunk_id")
        _uuid(self.document_id, "document_id")
        _uuid(self.version_id, "version_id")
        if not isinstance(self.citation, Citation):
            raise DomainValidationError("citation is invalid")


@dataclass(frozen=True, slots=True)
class Message:
    """Conversation message；user 與 assistant payload 具有不同的安全 invariant。"""

    id: UUID
    conversation_id: UUID
    turn_id: UUID
    ordinal: int
    role: MessageRole
    content: str
    response_kind: ChatOutcome | None = None
    answer_parts: tuple[AnswerPart, ...] = ()
    citations: tuple[Citation, ...] = ()
    payload_schema_version: int = 1
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _uuid(self.id, "message_id")
        _uuid(self.conversation_id, "conversation_id")
        _uuid(self.turn_id, "turn_id")
        _positive_integer(self.ordinal, "message_ordinal")
        if not isinstance(self.role, MessageRole):
            raise DomainValidationError("message role is invalid")
        _text(self.content, "message_content", maximum=200_000)
        if isinstance(self.payload_schema_version, bool) or self.payload_schema_version <= 0:
            raise DomainValidationError("payload_schema_version must be positive")
        _optional_aware_datetime(self.created_at, "created_at")
        if any(not isinstance(part, AnswerPart) for part in self.answer_parts):
            raise DomainValidationError("answer_parts contain an invalid value")
        if any(not isinstance(citation, Citation) for citation in self.citations):
            raise DomainValidationError("citations contain an invalid value")

        if self.role is MessageRole.USER:
            if self.response_kind is not None or self.answer_parts or self.citations:
                raise DomainValidationError("user messages cannot contain assistant payload")
            return

        if self.response_kind is None:
            raise DomainValidationError("assistant messages require a response kind")
        citation_ids = [citation.citation_id for citation in self.citations]
        if len(set(citation_ids)) != len(citation_ids):
            raise DomainValidationError("assistant citation IDs must be unique")
        referenced_ids = {
            citation_id for part in self.answer_parts for citation_id in part.citation_ids
        }
        if not referenced_ids.issubset(set(citation_ids)):
            raise DomainValidationError("answer part references an unknown citation")
        if self.response_kind is ChatOutcome.ANSWERED:
            if not self.answer_parts or not self.citations:
                raise DomainValidationError("answered messages require parts and citations")
            if referenced_ids != set(citation_ids):
                raise DomainValidationError("answered messages cannot contain unused citations")
        elif self.answer_parts:
            raise DomainValidationError("non-answered messages cannot contain answer parts")


@dataclass(frozen=True, slots=True)
class PurgeSelector:
    """管理員 purge preview 的 bounded AND selector。"""

    conversation_ids: tuple[UUID, ...] = ()
    owner_issuer: str | None = None
    owner_subject: str | None = None
    created_before: datetime | None = None

    def __post_init__(self) -> None:
        if len(self.conversation_ids) > 100:
            raise DomainValidationError("conversation_ids must contain at most 100 values")
        if len(set(self.conversation_ids)) != len(self.conversation_ids):
            raise DomainValidationError("conversation_ids must not contain duplicates")
        for conversation_id in self.conversation_ids:
            _uuid(conversation_id, "conversation_id")
        if (self.owner_issuer is None) != (self.owner_subject is None):
            raise DomainValidationError("owner_issuer and owner_subject must be supplied together")
        _optional_text(self.owner_issuer, "owner_issuer", maximum=255)
        _optional_text(self.owner_subject, "owner_subject", maximum=512)
        _optional_aware_datetime(self.created_before, "created_before")
        if not self.conversation_ids and self.owner_issuer is None and self.created_before is None:
            raise DomainValidationError("purge selector must contain at least one condition")


@dataclass(frozen=True, slots=True)
class SelectedConversation:
    """Preview 只保存 conversation ID 與 row version，不保存內文。"""

    conversation_id: UUID
    row_version: int

    def __post_init__(self) -> None:
        _uuid(self.conversation_id, "conversation_id")
        _positive_integer(self.row_version, "row_version")


@dataclass(frozen=True, slots=True)
class PurgePreview:
    """兩階段 purge 的可過期、可消費 preview。"""

    id: UUID
    selector: PurgeSelector
    selected: tuple[SelectedConversation, ...]
    selector_digest: str
    conversation_count: int
    message_count: int
    creator: Principal
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None

    def __post_init__(self) -> None:
        _uuid(self.id, "purge_preview_id")
        if not isinstance(self.selector, PurgeSelector):
            raise DomainValidationError("purge selector is invalid")
        if any(not isinstance(item, SelectedConversation) for item in self.selected):
            raise DomainValidationError("selected conversation is invalid")
        _text(self.selector_digest, "selector_digest", maximum=64)
        _non_negative_integer(self.conversation_count, "conversation_count")
        _non_negative_integer(self.message_count, "message_count")
        if not isinstance(self.creator, Principal):
            raise DomainValidationError("creator must be a Principal")
        _aware_datetime(self.expires_at, "expires_at")
        _aware_datetime(self.created_at, "created_at")
        _optional_aware_datetime(self.consumed_at, "consumed_at")


@dataclass(frozen=True, slots=True)
class PurgeAudit:
    """Content-free purge audit；永不保存問題、回答、evidence 或文件名。"""

    id: UUID
    actor: Principal
    selector_digest: str
    conversation_count: int
    message_count: int
    reason: str
    request_id: str
    purged_at: datetime

    def __post_init__(self) -> None:
        _uuid(self.id, "purge_id")
        if not isinstance(self.actor, Principal):
            raise DomainValidationError("actor must be a Principal")
        _text(self.selector_digest, "selector_digest", maximum=64)
        _non_negative_integer(self.conversation_count, "conversation_count")
        _non_negative_integer(self.message_count, "message_count")
        _text(self.reason, "purge_reason", maximum=MAX_PURGE_REASON_LENGTH)
        _text(self.request_id, "request_id", maximum=MAX_REQUEST_ID_LENGTH)
        _aware_datetime(self.purged_at, "purged_at")
