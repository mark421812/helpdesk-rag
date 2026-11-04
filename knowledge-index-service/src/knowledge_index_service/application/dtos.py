"""Knowledge Index Application layer 的 DTO。

DTO（Data Transfer Object）用來跨 use case 邊界傳遞資料。這些 frozen dataclass 不知道
FastAPI、JSON、SQLAlchemy 或 Qdrant，讓 Application 可以被不同介面重用與單元測試。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from knowledge_index_service.domain.entities import AccessControlEntry, SearchFilters
from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IndexingStatus,
    JobStatus,
)


class UnsetValue:
    """PATCH sentinel：區分「欄位省略」與「明確傳入 JSON null」。

    ``None`` 已代表 Client 要清除欄位，所以還需要第三種 singleton 值表示完全不修改。
    """

    __slots__ = ()


# 模組只建立一個 sentinel instance，呼叫端以 identity/type 判斷。
UNSET = UnsetValue()


@dataclass(frozen=True, slots=True)
class ChunkSubmission:
    """Ingestion 已切分完成、準備寫入 KIS 的單一 chunk。"""

    content: str
    section_path: str | None = None
    page_number: int | None = None
    token_count: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentSubmission:
    """建立／恢復文件版本所需的完整 Application 輸入。"""

    source_type: str
    source_item_id: str
    source_version: str
    title: str
    document_type: str
    owner_department: str
    document_owner: str
    confidentiality: Confidentiality
    status: DocumentStatus
    language: str
    external_llm_allowed: bool
    version_number: str
    content_hash: str
    mime_type: str
    file_size: int
    parser_name: str
    parser_version: str
    chunks: tuple[ChunkSubmission, ...]
    source_path: str | None = None
    source_url: str | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    review_at: date | None = None
    custom_metadata: dict[str, Any] = field(default_factory=dict)
    acl: tuple[AccessControlEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class SubmitDocumentResult:
    """文件提交結果；非空 job_id 代表後續需等待非同步索引。"""

    document_id: UUID
    version_id: UUID
    job_id: UUID | None
    indexing_status: IndexingStatus
    created: bool


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """KIS use case 的向量搜尋輸入。"""

    query: str
    limit: int = 8
    score_threshold: float | None = None
    filters: SearchFilters = field(default_factory=SearchFilters)


@dataclass(frozen=True, slots=True)
class SearchResultItem:
    """Qdrant score 加上 PostgreSQL canonical 內容／metadata 後的結果。"""

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
    token_count: int
    chunk_index: int
    version_number: str
    effective_at: date | None
    expires_at: date | None
    external_llm_allowed: bool
    confidentiality: Confidentiality
    language: str
    status: DocumentStatus
    custom_metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchResult:
    """一次搜尋的結果集合，並回報實際使用的 embedding model 身分。"""

    items: tuple[SearchResultItem, ...]
    model_id: str
    model_revision: str


@dataclass(frozen=True, slots=True)
class MetadataPatch:
    """文件 metadata 部分更新；部分欄位支援 omitted/value/null 三態。"""

    title: str | None = None
    document_type: str | None = None
    owner_department: str | None = None
    document_owner: str | None = None
    confidentiality: Confidentiality | None = None
    status: DocumentStatus | None = None
    language: str | None = None
    external_llm_allowed: bool | None = None
    source_path: str | None = None
    source_url: str | UnsetValue | None = UNSET
    custom_metadata: dict[str, Any] | None = None
    acl: tuple[AccessControlEntry, ...] | None = None
    version_number: str | None = None
    source_version: str | None = None
    effective_at: date | UnsetValue | None = UNSET
    expires_at: date | UnsetValue | None = UNSET
    review_at: date | UnsetValue | None = UNSET


@dataclass(frozen=True, slots=True)
class JobView:
    """不暴露 ORM model 的索引工作唯讀 view。"""

    job_id: UUID
    document_id: UUID
    version_id: UUID | None
    status: JobStatus
    attempt_count: int
    max_attempts: int
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    """批次 embedding 結果；tuple 的外層順序與輸入 texts 相同。"""

    vectors: tuple[tuple[float, ...], ...]
    model_id: str
    model_revision: str
    dimension: int
