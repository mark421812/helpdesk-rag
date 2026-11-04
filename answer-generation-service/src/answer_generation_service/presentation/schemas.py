"""Answer Generation 對外 JSON contract；未知欄位一律拒絕。"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from answer_generation_service.domain.enums import AnswerStatus, Confidentiality

PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CitationRequest(ApiModel):
    title: str = Field(min_length=1, max_length=1_000)
    section_path: str | None = Field(default=None, max_length=2_000)
    page_number: PositiveStrictInt | None = None

    @field_validator("title", "section_path")
    @classmethod
    def reject_blank_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("citation text must not be blank")
        return value


class GovernanceRequest(ApiModel):
    confidentiality: Confidentiality
    language: str = Field(min_length=1, max_length=20)
    external_llm_allowed: Literal[True]

    @field_validator("language")
    @classmethod
    def reject_blank_language(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("governance language must not be blank")
        return value


class ApprovedEvidenceRequest(ApiModel):
    rank: PositiveStrictInt
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    # Retrieval 的相似度分數是跨服務相容所需的診斷欄位。它必須是有限數值，但 Answer
    # 不會把它加入 attestation digest、Prompt 或回答決策，避免把 model-specific score
    # 誤當成答案可信度。
    score: float = Field(strict=True, allow_inf_nan=False)
    content: str = Field(min_length=1, max_length=200_000)
    document_type: str = Field(min_length=1, max_length=100)
    token_count: PositiveStrictInt
    citation: CitationRequest
    governance: GovernanceRequest

    @field_validator("content", "document_type")
    @classmethod
    def reject_blank_evidence_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence text must not be blank")
        return value


class GenerateAnswerRequest(ApiModel):
    question: str = Field(min_length=1, max_length=20_000)
    language: str | None = Field(default=None, max_length=20)
    evidence: list[ApprovedEvidenceRequest] = Field(min_length=1, max_length=20)
    context_attestation: str = Field(min_length=1, max_length=8_192)

    @field_validator("question", "language", "context_attestation")
    @classmethod
    def reject_blank_request_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("request text must not be blank")
        return value


class AnswerPartResponse(ApiModel):
    text: str
    citation_ids: list[str]


class CitationResponse(ApiModel):
    citation_id: str
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    title: str
    section_path: str | None
    page_number: int | None


class GenerationDiagnosticsResponse(ApiModel):
    provider: str
    model: str
    prompt_version: str
    finish_reason: str
    prompt_tokens: int | None
    completion_tokens: int | None


class GenerateAnswerResponse(ApiModel):
    status: AnswerStatus
    answer_parts: list[AnswerPartResponse]
    citations: list[CitationResponse]
    generation: GenerationDiagnosticsResponse


class HealthResponse(ApiModel):
    status: str
    checks: dict[str, bool]


class ErrorResponse(ApiModel):
    error: str
    message: str
    request_id: str | None
    details: dict[str, Any] | list[dict[str, Any]] | None = None
