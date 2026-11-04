"""KIS 對外 HTTP/JSON contract 的 Pydantic schemas。"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    EmbeddingInputType,
    IndexingStatus,
    JobStatus,
    PermissionEffect,
    PrincipalType,
)


class ApiModel(BaseModel):
    """共同設定 extra=forbid，拒絕未知 JSON 欄位。"""

    model_config = ConfigDict(extra="forbid")


class AclEntryRequest(ApiModel):
    """單筆 ACL JSON schema。"""

    principal_type: PrincipalType
    principal_id: str = Field(min_length=1, max_length=512)
    effect: PermissionEffect = PermissionEffect.ALLOW


def _reject_duplicate_acl(
    entries: list[AclEntryRequest] | None,
) -> list[AclEntryRequest] | None:
    if entries is None:
        return None
    seen: set[tuple[PrincipalType, str, PermissionEffect]] = set()
    for entry in entries:
        identity = (entry.principal_type, entry.principal_id, entry.effect)
        if identity in seen:
            raise ValueError(
                "duplicate ACL entry: "
                f"{entry.principal_type.value}:{entry.principal_id}:{entry.effect.value}"
            )
        seen.add(identity)
    return entries


class ChunkRequest(ApiModel):
    """Ingestion 已完成 chunking 的單筆輸入 schema。"""

    content: str = Field(min_length=1, max_length=100_000)
    section_path: str | None = Field(default=None, max_length=2000)
    page_number: int | None = Field(default=None, gt=0)
    token_count: int | None = Field(default=None, gt=0)


class DocumentSubmissionRequest(ApiModel):
    """Ingestion 送入已解析文件、metadata、ACL 與 chunks 的 request contract。"""

    source_type: str = Field(min_length=1, max_length=50)
    source_item_id: str = Field(min_length=1, max_length=512)
    source_version: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=1000)
    document_type: str = Field(min_length=1, max_length=100)
    owner_department: str = Field(min_length=1, max_length=255)
    document_owner: str = Field(min_length=1, max_length=255)
    confidentiality: Confidentiality = Confidentiality.INTERNAL
    status: DocumentStatus = DocumentStatus.DRAFT
    language: str = Field(default="zh-TW", min_length=1, max_length=20)
    external_llm_allowed: bool = False
    version_number: str = Field(min_length=1, max_length=100)
    content_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    mime_type: str = Field(min_length=1, max_length=255)
    file_size: int = Field(ge=0)
    parser_name: str = Field(min_length=1, max_length=100)
    parser_version: str = Field(min_length=1, max_length=100)
    chunks: list[ChunkRequest] = Field(min_length=1, max_length=10_000)
    source_path: str | None = None
    source_url: str | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    review_at: date | None = None
    custom_metadata: dict[str, Any] = Field(default_factory=dict)
    acl: list[AclEntryRequest] = Field(default_factory=list, max_length=1000)

    @field_validator("source_type", "source_item_id", "title", "document_type")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("acl")
    @classmethod
    def reject_duplicate_acl(cls, value: list[AclEntryRequest]) -> list[AclEntryRequest]:
        validated = _reject_duplicate_acl(value)
        return validated if validated is not None else []


class DocumentSubmissionResponse(ApiModel):
    """文件提交 response；job_id 非空代表需等待背景索引。"""

    document_id: UUID
    version_id: UUID
    job_id: UUID | None
    indexing_status: IndexingStatus
    created: bool


class EmbeddingRequest(ApiModel):
    """內部批次 embedding request。"""

    texts: list[str] = Field(min_length=1)
    input_type: EmbeddingInputType


class EmbeddingResponse(ApiModel):
    """內部 embedding response。"""

    vectors: list[list[float]]
    model_id: str
    model_revision: str
    dimension: int


class SearchFiltersRequest(ApiModel):
    """搜尋時的可信身分與文件治理 filters。"""

    user_id: str | None = Field(default=None, max_length=512)
    group_ids: list[str] = Field(default_factory=list, max_length=100)
    statuses: list[DocumentStatus] = Field(default_factory=lambda: [DocumentStatus.PUBLISHED])
    confidentialities: list[Confidentiality] = Field(default_factory=list)
    language: str | None = Field(default=None, max_length=20)


class SearchRequestBody(ApiModel):
    """KIS 搜尋 API body；Retrieval 會填入可信 user/group filters。"""

    query: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(default=8, ge=1, le=50)
    score_threshold: float | None = Field(default=None, ge=-1, le=1)
    filters: SearchFiltersRequest = Field(default_factory=SearchFiltersRequest)


class SearchResultItemResponse(ApiModel):
    """Qdrant score 加上 PostgreSQL canonical 欄位的搜尋項目。"""

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    score: float
    content: str
    title: str
    document_type: str
    source_path: str | None
    source_url: str | None
    section_path: str | None
    page_number: int | None
    token_count: int = Field(gt=0)
    chunk_index: int
    version_number: str
    effective_at: date | None
    expires_at: date | None
    external_llm_allowed: bool
    confidentiality: Confidentiality
    language: str
    status: DocumentStatus
    custom_metadata: dict[str, Any]


class SearchResponse(ApiModel):
    """搜尋結果與 embedding model identity。"""

    items: list[SearchResultItemResponse]
    model_id: str
    model_revision: str


class ServiceAuthResponse(ApiModel):
    """內部 API key 驗證 probe response。"""

    status: str


class MetadataPatchRequest(ApiModel):
    """metadata 部分更新；model_fields_set 可辨認 Client 是否真的傳了欄位。"""

    title: str | None = Field(default=None, min_length=1, max_length=1000)
    document_type: str | None = Field(default=None, min_length=1, max_length=100)
    owner_department: str | None = Field(default=None, min_length=1, max_length=255)
    document_owner: str | None = Field(default=None, min_length=1, max_length=255)
    confidentiality: Confidentiality | None = None
    status: DocumentStatus | None = None
    language: str | None = Field(default=None, min_length=1, max_length=20)
    external_llm_allowed: bool | None = None
    source_path: str | None = None
    source_url: str | None = None
    custom_metadata: dict[str, Any] | None = None
    acl: list[AclEntryRequest] | None = Field(default=None, max_length=1000)
    version_number: str | None = Field(default=None, min_length=1, max_length=100)
    source_version: str | None = Field(default=None, min_length=1, max_length=255)
    effective_at: date | None = None
    expires_at: date | None = None
    review_at: date | None = None

    @field_validator("acl")
    @classmethod
    def reject_duplicate_acl(
        cls,
        value: list[AclEntryRequest] | None,
    ) -> list[AclEntryRequest] | None:
        return _reject_duplicate_acl(value)

    @model_validator(mode="after")
    def reject_null_version_identifiers(self) -> MetadataPatchRequest:
        for field_name in ("version_number", "source_version"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")
        return self


class JobResponse(ApiModel):
    """索引／同步／刪除工作的公開狀態。"""

    job_id: UUID
    document_id: UUID
    version_id: UUID | None
    status: JobStatus
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None


class HealthResponse(ApiModel):
    """Liveness/readiness response。"""

    status: str
    checks: dict[str, bool] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    """一致的錯誤 JSON contract。"""

    code: str
    message: str
    request_id: str | None = None
