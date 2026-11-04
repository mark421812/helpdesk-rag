"""Chat Service 對外 HTTP schema。

這些 Pydantic model 是 Presentation-only transport contract；它們不會被 Application import。
所有 request model 都拒絕未知欄位，並在進入 use case 前驗證長度、空白、selector 與 pagination
範圍。Response model 只投影 employee 可見欄位，刻意不包含 owner identity、evidence、source
path/URL、attestation 或下游診斷內容。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from chat_service.domain.enums import ChatOutcome, ConversationStatus, MessageRole, PublicTurnState


class ApiModel(BaseModel):
    """所有 public schema 的共同 strict unknown-field policy。"""

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(ApiModel):
    """統一安全錯誤 envelope；details 僅允許欄位名稱等非敏感驗證資訊。"""

    error: StrictStr
    message: StrictStr
    request_id: StrictStr | None = None
    details: list[dict[str, Any]] | dict[str, Any] | None = None


class HealthResponse(ApiModel):
    """live/ready response；checks 只使用低 cardinality 固定名稱。"""

    status: StrictStr
    checks: dict[str, bool]


class CreateConversationRequest(ApiModel):
    """建立 conversation 的 request；owner 只來自 identity header。"""

    title: StrictStr | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value


class ConversationResponse(ApiModel):
    """employee-safe conversation summary。"""

    conversation_id: UUID
    title: str | None
    status: ConversationStatus
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


class ListConversationsResponse(ApiModel):
    """conversation keyset page；cursor 為 opaque string。"""

    items: list[ConversationResponse]
    next_cursor: str | None = None


class UpdateConversationRequest(ApiModel):
    """PATCH request；title/status 至少提供一個，明確 null 可清除 title。"""

    title: StrictStr | None = Field(default=None, max_length=200)
    status: ConversationStatus | None = None

    @field_validator("title")
    @classmethod
    def patch_title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def at_least_one_field(self) -> UpdateConversationRequest:
        if not self.model_fields_set.intersection({"title", "status"}):
            raise ValueError("at least one conversation field is required")
        return self


class SubmitTurnRequest(ApiModel):
    """送出單輪問題；不得攜帶 identity、provider、prompt、URL 或 retrieval limit。"""

    question: StrictStr = Field(min_length=1, max_length=20_000)
    language: StrictStr | None = Field(default=None, max_length=20)

    @field_validator("question", "language")
    @classmethod
    def request_text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("request text must not be blank")
        return value


class AnswerPartResponse(ApiModel):
    """回答段落的安全 public projection。"""

    text: StrictStr
    citation_ids: list[StrictStr]


class CitationResponse(ApiModel):
    """只顯示文件名稱、章節與頁碼，不暴露 machine UUID/path/URL。"""

    citation_id: StrictStr
    title: StrictStr
    section_path: StrictStr | None
    page_number: StrictInt | None


class MessageResponse(ApiModel):
    """安全 message projection；machine message/turn IDs 是 API resource IDs。"""

    message_id: UUID
    role: MessageRole
    content: StrictStr
    response_kind: ChatOutcome | None
    answer_parts: list[AnswerPartResponse] | None
    citations: list[CitationResponse]
    created_at: datetime


class SafeErrorResponse(ApiModel):
    """可重播的 safe terminal error；不帶下游 raw detail。"""

    code: StrictStr
    message: StrictStr
    retryable: bool
    retry_after_seconds: StrictInt | None = None


class TurnResponse(ApiModel):
    """processing/terminal turn 的共用 response model。"""

    conversation_id: UUID
    turn_id: UUID
    state: PublicTurnState
    outcome: ChatOutcome | None
    user_message: MessageResponse | None = None
    assistant_message: MessageResponse | None = None
    error: SafeErrorResponse | None = None
    request_id: StrictStr
    status_url: StrictStr | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ListMessagesResponse(ApiModel):
    """依 assistant/user message ordinal ASC 的 keyset page。"""

    items: list[MessageResponse]
    next_cursor: str | None = None


class PurgePreviewRequest(ApiModel):
    """Admin preview selector；多欄 selector 在 repository 以 AND 套用。"""

    conversation_ids: list[UUID] = Field(default_factory=list, max_length=100)
    owner_issuer: StrictStr | None = Field(default=None, max_length=255)
    owner_subject: StrictStr | None = Field(default=None, max_length=512)
    created_before: datetime | None = None

    @field_validator("owner_issuer", "owner_subject")
    @classmethod
    def selector_text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("selector identity must not be blank")
        return value

    @field_validator("created_before")
    @classmethod
    def selector_time_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("created_before must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_selector(self) -> PurgePreviewRequest:
        if (self.owner_issuer is None) != (self.owner_subject is None):
            raise ValueError("owner_issuer and owner_subject must be supplied together")
        if len(set(self.conversation_ids)) != len(self.conversation_ids):
            raise ValueError("conversation_ids must not contain duplicates")
        if not self.conversation_ids and self.owner_issuer is None and self.created_before is None:
            raise ValueError("at least one purge selector is required")
        return self


class PurgePreviewResponse(ApiModel):
    """Preview metadata；不回傳 conversation 內容或 IDs。"""

    preview_id: UUID
    conversation_count: StrictInt
    message_count: StrictInt
    busy_count: StrictInt
    expires_at: datetime


class ExecutePurgeRequest(ApiModel):
    """執行 hard purge 的 preview ID 與人類可追蹤但不含內容的 reason。"""

    preview_id: UUID
    reason: StrictStr = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class PurgeResponse(ApiModel):
    """Hard purge 結果只回安全計數與時間。"""

    purge_id: UUID
    conversation_count: StrictInt
    message_count: StrictInt
    purged_at: datetime
