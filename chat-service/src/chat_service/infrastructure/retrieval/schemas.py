"""Retrieval Service 的本地 HTTP contract schemas。

這些 Pydantic model 只存在於 Infrastructure adapter：它們把不可信的 JSON 轉成嚴格、
有界的 transport object，再由 adapter 映射成 Chat 的 Domain evidence。`extra='forbid'`
與 strict numeric fields 可避免下游新增欄位或 Python 的 bool/int 相容性悄悄繞過 contract。
跨欄位的治理分流、rank 連續性與計數關係也在此層 fail closed；restricted evidence 之後
只會映射成不含 content 的 Domain projection。
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

from chat_service.domain.enums import Confidentiality, RetrievalStatus

StrictPositiveInt = Annotated[StrictInt, Field(gt=0)]
StrictNonNegativeInt = Annotated[StrictInt, Field(ge=0)]
StrictFiniteFloat = Annotated[StrictFloat, Field(allow_inf_nan=False)]


class RetrievalApiModel(BaseModel):
    """所有 Retrieval transport object 的共同嚴格設定。"""

    model_config = ConfigDict(extra="forbid")


class RetrievalRequestSchema(RetrievalApiModel):
    """送往 Retrieval 的 allowlist body；principal、URL 與 API key 不在 JSON 中。"""

    query: str = Field(min_length=1, max_length=20_000)
    limit: StrictPositiveInt = Field(le=20)
    language: str | None = Field(default=None, max_length=20)

    @field_validator("query", "language")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("request text must not be blank")
        return value


class RetrievalCitationSchema(RetrievalApiModel):
    """下游 citation 的安全欄位；沒有 source path 或 URL。"""

    title: str = Field(min_length=1, max_length=1_000)
    section_path: str | None = Field(default=None, max_length=2_000)
    page_number: StrictPositiveInt | None = None

    @field_validator("title", "section_path")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("citation text must not be blank")
        return value


class RetrievalGovernanceSchema(RetrievalApiModel):
    """Retrieval policy metadata；external flag 必須是實際 JSON boolean。"""

    confidentiality: Confidentiality
    language: str = Field(min_length=1, max_length=20)
    external_llm_allowed: StrictBool

    @field_validator("language")
    @classmethod
    def reject_blank_language(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("governance language must not be blank")
        return value


class RetrievalEvidenceSchema(RetrievalApiModel):
    """Retrieval evidence transport shape；restricted content 只在 adapter memory 短暫存在。"""

    rank: StrictPositiveInt
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: StrictFiniteFloat
    content: str = Field(min_length=1, max_length=200_000)
    document_type: str = Field(min_length=1, max_length=100)
    token_count: StrictPositiveInt
    citation: RetrievalCitationSchema
    governance: RetrievalGovernanceSchema

    @field_validator("content", "document_type")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence text must not be blank")
        return value


class RetrievalDiagnosticsSchema(RetrievalApiModel):
    """只接受可安全保存的 Retrieval counts/model revision。"""

    candidate_count: StrictNonNegativeInt
    selected_count: StrictNonNegativeInt
    model_id: str = Field(min_length=1, max_length=512)
    model_revision: str = Field(min_length=1, max_length=512)

    @field_validator("model_id", "model_revision")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("diagnostic text must not be blank")
        return value


class RetrievalResponseSchema(RetrievalApiModel):
    """Retrieval 200 response，包含三種業務結果與治理 invariants。"""

    status: RetrievalStatus
    llm_context: list[RetrievalEvidenceSchema] = Field(max_length=20)
    restricted_evidence: list[RetrievalEvidenceSchema] = Field(max_length=20)
    external_generation_blocked: StrictBool
    context_attestation: str | None = Field(default=None, min_length=1, max_length=8_192)
    retrieval: RetrievalDiagnosticsSchema

    @field_validator("context_attestation")
    @classmethod
    def reject_blank_attestation(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("context_attestation must not be blank")
        return value

    @model_validator(mode="after")
    def validate_cross_field_contract(self) -> RetrievalResponseSchema:
        """驗證 rank、計數、治理分流與 status 的完整關係。"""

        allowed_ranks = [item.rank for item in self.llm_context]
        restricted_ranks = [item.rank for item in self.restricted_evidence]
        if allowed_ranks != sorted(allowed_ranks) or restricted_ranks != sorted(restricted_ranks):
            raise ValueError("evidence lists must be ordered by ascending rank")
        if any(not item.governance.external_llm_allowed for item in self.llm_context):
            raise ValueError("llm_context contains restricted evidence")
        if any(item.governance.external_llm_allowed for item in self.restricted_evidence):
            raise ValueError("restricted_evidence contains allowed evidence")

        all_ranks = allowed_ranks + restricted_ranks
        if sorted(all_ranks) != list(range(1, len(all_ranks) + 1)):
            raise ValueError("evidence ranks must be globally unique and contiguous")
        if self.retrieval.selected_count != len(all_ranks):
            raise ValueError("selected_count does not match evidence lists")
        if self.retrieval.candidate_count < self.retrieval.selected_count:
            raise ValueError("candidate_count cannot be smaller than selected_count")

        if self.status is RetrievalStatus.EVIDENCE_FOUND:
            if not self.llm_context or self.context_attestation is None:
                raise ValueError("evidence_found requires allowed context and attestation")
            if self.external_generation_blocked:
                raise ValueError("evidence_found cannot be externally blocked")
        elif self.status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED:
            if self.llm_context or not self.restricted_evidence:
                raise ValueError("external_generation_blocked requires restricted-only evidence")
            if self.context_attestation is not None or not self.external_generation_blocked:
                raise ValueError("restricted-only result has invalid attestation or blocked flag")
        elif self.status is RetrievalStatus.INSUFFICIENT_EVIDENCE:
            if self.llm_context or self.restricted_evidence:
                raise ValueError("insufficient_evidence cannot contain evidence")
            if self.context_attestation is not None or self.external_generation_blocked:
                raise ValueError("insufficient_evidence has invalid attestation or blocked flag")
        return self


class RetrievalErrorSchema(RetrievalApiModel):
    """下游錯誤 envelope 的安全解析形狀；adapter 只讀 error code。"""

    error: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2_000)
    request_id: str | None = Field(max_length=128)
    details: list[dict[str, object]] | None = None


class RetrievalHealthSchema(RetrievalApiModel):
    """Readiness response；不觸發檢索或其他副作用。"""

    status: str = Field(min_length=1, max_length=32)
    checks: dict[str, StrictBool]
