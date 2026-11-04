"""Web Chat 的 client-facing Pydantic contract。

Request model 採 explicit allowlist 並拒絕未知欄位；response model 只接受 Chat Service 已公開的
安全投影。這個 layer 不知道 SQLAlchemy、下游 service key 或 retrieval evidence。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator


class ApiModel(BaseModel):
    """所有瀏覽器可見 JSON model 的 strict extra policy。"""

    model_config = ConfigDict(extra="forbid")


class CreateConversationRequest(ApiModel):
    """只允許 Chat Service 的 title 欄位。"""

    title: StrictStr | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value


class SubmitTurnRequest(ApiModel):
    """只允許 question/language；身分、provider、URL 與 prompt 不可由瀏覽器指定。"""

    question: StrictStr = Field(min_length=1, max_length=20_000)
    language: StrictStr | None = Field(default=None, max_length=20)

    @field_validator("question", "language")
    @classmethod
    def text_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("request text must not be blank")
        return value


class ConversationResponse(ApiModel):
    """安全的 conversation summary。"""

    conversation_id: UUID
    title: str | None
    status: Literal["active", "archived"]
    created_at: str
    updated_at: str
    last_activity_at: str


class AnswerPartResponse(ApiModel):
    """回答段落及其 public citation references。"""

    text: str
    citation_ids: list[str]


class CitationResponse(ApiModel):
    """只顯示文件名稱、章節與頁碼。"""

    citation_id: str
    title: str
    section_path: str | None
    page_number: StrictInt | None


class MessageResponse(ApiModel):
    """安全 user/assistant message projection。"""

    message_id: UUID
    role: Literal["user", "assistant"]
    content: str
    response_kind: str | None
    answer_parts: list[AnswerPartResponse] | None
    citations: list[CitationResponse]
    created_at: str


class SafeErrorResponse(ApiModel):
    """不含 downstream raw detail 的 terminal error。"""

    code: str
    message: str
    retryable: bool
    retry_after_seconds: StrictInt | None = Field(default=None, ge=0, le=86_400)


class TurnResponse(ApiModel):
    """同步結果與 processing polling handle 的共用 response。"""

    conversation_id: UUID
    turn_id: UUID
    state: Literal["processing", "completed", "failed", "failed_unknown"]
    outcome: str | None
    user_message: MessageResponse | None = None
    assistant_message: MessageResponse | None = None
    error: SafeErrorResponse | None = None
    request_id: str
    status_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ErrorResponse(ApiModel):
    """Web Chat 對瀏覽器的固定安全錯誤 envelope。"""

    error: str
    message: str
    request_id: str | None = None
    details: None = None
