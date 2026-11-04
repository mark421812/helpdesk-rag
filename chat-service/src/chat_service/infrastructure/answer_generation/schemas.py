"""Answer Generation Service 的本地 HTTP contract schemas。

這些 model 屬於 Infrastructure，專門把不可信的下游 JSON 轉成 strict transport object。
Request schema 只接受 Chat 已建立的 allowlisted allowed evidence；response schema 再驗證
answer parts、citation references 與 business status 的跨欄位關係，確保 malformed answer
不會進入 Application 或 PostgreSQL。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from chat_service.domain.enums import AnswerStatus, Confidentiality

StrictPositiveInt = Annotated[StrictInt, Field(gt=0)]
StrictNonNegativeInt = Annotated[StrictInt, Field(ge=0)]
StrictFiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]


class AnswerApiModel(BaseModel):
    """所有 Answer transport object 的共同 strict/extra-forbid 設定。"""

    model_config = ConfigDict(extra="forbid")


class AnswerCitationSchema(AnswerApiModel):
    """Answer response 的 citation；machine UUID 只供 Chat persistence 使用。"""

    citation_id: str = Field(min_length=1, max_length=128)
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    title: str = Field(min_length=1, max_length=1_000)
    section_path: str | None = Field(default=None, max_length=2_000)
    page_number: StrictPositiveInt | None = None

    @field_validator("citation_id", "title", "section_path")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("citation text must not be blank")
        return value


class AnswerPartSchema(AnswerApiModel):
    """Answer response part；跨欄位是否引用實際 citation 在 response model 驗證。"""

    text: str = Field(min_length=1, max_length=200_000)
    citation_ids: list[str] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer text must not be blank")
        return value

    @field_validator("citation_ids")
    @classmethod
    def validate_citation_ids(cls, values: list[str]) -> list[str]:
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 128 for value in values
        ):
            raise ValueError("citation IDs must be non-blank and bounded")
        if len(set(values)) != len(values):
            raise ValueError("citation IDs must be unique within a part")
        return values


class AnswerGenerationDiagnosticsSchema(AnswerApiModel):
    """僅保留 safe generation diagnostics；不接受 prompt 或 provider raw response。"""

    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=512)
    prompt_version: str = Field(min_length=1, max_length=256)
    finish_reason: str = Field(min_length=1, max_length=256)
    prompt_tokens: StrictNonNegativeInt | None = None
    completion_tokens: StrictNonNegativeInt | None = None

    @field_validator("provider", "model", "prompt_version", "finish_reason")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("diagnostic text must not be blank")
        return value


class AnswerResponseSchema(AnswerApiModel):
    """Answer 200 response 與 answered/unable-to-answer invariants。"""

    status: AnswerStatus
    answer_parts: list[AnswerPartSchema]
    citations: list[AnswerCitationSchema]
    generation: AnswerGenerationDiagnosticsSchema

    @model_validator(mode="after")
    def validate_cross_field_contract(self) -> AnswerResponseSchema:
        """確保所有引用都存在且沒有未使用 citation object。"""

        citation_ids = [citation.citation_id for citation in self.citations]
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("response citation IDs must be globally unique")
        referenced_ids = [
            citation_id for part in self.answer_parts for citation_id in part.citation_ids
        ]
        if not set(referenced_ids).issubset(set(citation_ids)):
            raise ValueError("answer part references an unknown citation")
        if self.status is AnswerStatus.ANSWERED:
            if not self.answer_parts:
                raise ValueError("answered response requires answer parts")
            if not self.citations:
                raise ValueError("answered response requires citations")
            if set(referenced_ids) != set(citation_ids):
                raise ValueError("answered response contains an unused citation")
        elif self.status is AnswerStatus.UNABLE_TO_ANSWER:
            if self.answer_parts or self.citations:
                raise ValueError("unable_to_answer response must be empty")
        return self


class AnswerRequestCitationSchema(AnswerApiModel):
    """Answer request 中的 safe citation projection。"""

    title: str = Field(min_length=1, max_length=1_000)
    section_path: str | None = Field(default=None, max_length=2_000)
    page_number: StrictPositiveInt | None = None

    @field_validator("title", "section_path")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("citation text must not be blank")
        return value


class AnswerRequestGovernanceSchema(AnswerApiModel):
    """Request 只能攜帶已核准的 external-LLM governance。"""

    confidentiality: Confidentiality
    language: str = Field(min_length=1, max_length=20)
    external_llm_allowed: StrictBool

    @field_validator("language")
    @classmethod
    def reject_blank_language(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("governance language must not be blank")
        return value

    @field_validator("external_llm_allowed")
    @classmethod
    def require_true_external_policy(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("external_llm_allowed must be true")
        return value


class AllowedEvidenceRequestSchema(AnswerApiModel):
    """Answer request 的 evidence allowlist；沒有 restricted evidence 型別。"""

    rank: StrictPositiveInt
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: StrictFiniteFloat
    content: str = Field(min_length=1, max_length=200_000)
    document_type: str = Field(min_length=1, max_length=100)
    token_count: StrictPositiveInt
    citation: AnswerRequestCitationSchema
    governance: AnswerRequestGovernanceSchema

    @field_validator("content", "document_type")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence text must not be blank")
        return value


class AnswerRequestSchema(AnswerApiModel):
    """送往 Answer 的 allowlist body；不含 request ID、restricted evidence 或 diagnostics。"""

    question: str = Field(min_length=1, max_length=20_000)
    language: str | None = Field(default=None, max_length=20)
    evidence: list[AllowedEvidenceRequestSchema] = Field(min_length=1, max_length=20)
    context_attestation: str = Field(min_length=1, max_length=8_192)

    @field_validator("question", "language", "context_attestation")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("request text must not be blank")
        return value


class AnswerErrorSchema(AnswerApiModel):
    """下游錯誤 envelope 的安全解析形狀；adapter 只讀 machine code。"""

    error: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    request_id: str | None = Field(max_length=128)
    details: dict[str, object] | list[dict[str, object]] | None = None


class AnswerHealthSchema(AnswerApiModel):
    """Readiness response；probe 不會呼叫 generation。"""

    status: str = Field(min_length=1, max_length=32)
    checks: dict[str, StrictBool]
