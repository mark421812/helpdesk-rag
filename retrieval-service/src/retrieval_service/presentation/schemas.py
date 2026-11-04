"""FastAPI 對外 JSON contract 的 Pydantic schemas。

Schema 屬於 Presentation layer；Domain entities 不應知道 JSON 欄位或 FastAPI。設定
``extra='forbid'`` 會拒絕未知欄位，避免 Client 拼錯欄位卻被系統靜默忽略。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from retrieval_service.domain.enums import Confidentiality, RetrievalStatus


class ApiModel(BaseModel):
    """所有 API schema 的共同嚴格設定。"""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class RetrieveRequest(ApiModel):
    """``POST /api/v1/retrieve`` 的 request body。身分不在 body 中。"""

    query: str = Field(min_length=1, max_length=20_000)
    limit: int | None = Field(default=None, ge=1, le=50)
    language: str | None = Field(default=None, max_length=20)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("language")
    @classmethod
    def reject_blank_language(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("language must not be blank")
        return value


class CitationResponse(ApiModel):
    """刻意只公開文件名稱、章節、頁碼的安全引用。"""

    title: str
    section_path: str | None
    page_number: int | None


class GovernanceResponse(ApiModel):
    """每筆 evidence 的治理屬性，供後續 Chat/LLM 層執行政策。"""

    confidentiality: Confidentiality
    language: str
    external_llm_allowed: bool


class EvidenceResponse(ApiModel):
    """回給上層的 evidence JSON 形狀。"""

    rank: int
    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    # Answer API 接受此有限數值作為相容診斷欄位；score 不屬於簽章 canonical context，
    # 也不能被下游當作答案可信度或放進 LLM Prompt。
    score: float
    content: str
    document_type: str
    token_count: int
    citation: CitationResponse
    governance: GovernanceResponse


class RetrievalDiagnosticsResponse(ApiModel):
    """不含敏感內容的檢索計數診斷資訊。"""

    candidate_count: int
    selected_count: int
    model_id: str
    model_revision: str


class RetrieveResponse(ApiModel):
    """檢索回應；允許與禁止外送的 evidence 使用不同欄位。"""

    status: RetrievalStatus
    llm_context: list[EvidenceResponse]
    restricted_evidence: list[EvidenceResponse]
    external_generation_blocked: bool
    # 只有 llm_context 非空時才會簽發；restricted/insufficient 結果必須是 None。
    context_attestation: str | None
    retrieval: RetrievalDiagnosticsResponse


class HealthResponse(ApiModel):
    """Liveness/readiness response。"""

    status: str
    checks: dict[str, bool]


class ErrorResponse(ApiModel):
    """一致的錯誤 JSON contract。"""

    error: str
    message: str
    request_id: str | None
    details: list[dict[str, Any]] | None = None
