"""Chat Service wire schemas。

這些 Pydantic model 僅存在 Infrastructure，用來在 trust boundary 驗證下游 response；它們不會
成為 application 或前端可自由建立的資料模型。``extra='forbid'`` 讓契約新增欄位時先明確失敗。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class WireModel(BaseModel):
    """所有下游 response 的 strict unknown-field policy。"""

    model_config = ConfigDict(extra="forbid")


class ConversationWire(WireModel):
    """Chat conversation public projection。"""

    conversation_id: UUID
    title: StrictStr | None
    status: Literal["active", "archived"]
    created_at: StrictStr
    updated_at: StrictStr
    last_activity_at: StrictStr


class AnswerPartWire(WireModel):
    """Answer part 的安全文字與 public citation reference。"""

    text: StrictStr
    citation_ids: list[StrictStr]


class CitationWire(WireModel):
    """只接受 employee-safe citation 欄位。"""

    citation_id: StrictStr
    title: StrictStr
    section_path: StrictStr | None
    page_number: StrictInt | None


class MessageWire(WireModel):
    """Chat message public projection，不包含 machine ID 或 evidence。"""

    message_id: UUID
    role: Literal["user", "assistant"]
    content: StrictStr
    response_kind: str | None
    answer_parts: list[AnswerPartWire] | None
    citations: list[CitationWire]
    created_at: StrictStr


class SafeErrorWire(WireModel):
    """Terminal turn 可公開的安全錯誤。"""

    code: StrictStr
    message: StrictStr
    retryable: bool
    retry_after_seconds: StrictInt | None = Field(default=None, ge=0, le=86_400)


class TurnWire(WireModel):
    """同步 terminal 與 202 processing 共用的 Chat response。"""

    conversation_id: UUID
    turn_id: UUID
    state: Literal["processing", "completed", "failed", "failed_unknown"]
    outcome: str | None
    user_message: MessageWire | None = None
    assistant_message: MessageWire | None = None
    error: SafeErrorWire | None = None
    request_id: StrictStr
    status_url: StrictStr | None = None
    created_at: StrictStr | None = None
    updated_at: StrictStr | None = None


class HealthWire(WireModel):
    """Readiness response 僅接受固定 status 與 boolean checks。"""

    status: Literal["ok", "not_ready"]
    checks: dict[str, bool]
