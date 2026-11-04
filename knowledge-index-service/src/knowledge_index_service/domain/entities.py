"""KIS Domain entities 與文件／ACL／索引工作的核心 invariants。

這裡只使用 Python 標準函式庫。Entity 透過 dataclass ``__post_init__`` 保證資料一建立就
合法，不把重要規則留給 API 或資料庫碰運氣。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from knowledge_index_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IndexingStatus,
    JobStatus,
    JobType,
    OutboxStatus,
    PermissionEffect,
    PrincipalType,
)
from knowledge_index_service.domain.exceptions import ValidationError


def utc_now() -> datetime:
    """回傳 timezone-aware UTC 時間，避免 naive datetime 時區歧義。"""

    return datetime.now(UTC)


@dataclass(slots=True)
class AccessControlEntry:
    """一筆 user/group allow/deny 規則。"""

    principal_type: PrincipalType
    principal_id: str
    effect: PermissionEffect = PermissionEffect.ALLOW

    def __post_init__(self) -> None:
        if not self.principal_id.strip():
            raise ValidationError("ACL principal_id must not be blank")


def _validate_unique_acl(entries: list[AccessControlEntry]) -> None:
    """使用 set 偵測完全重複的 ACL；set 查找平均為 O(1)。"""

    seen: set[tuple[PrincipalType, str, PermissionEffect]] = set()
    for entry in entries:
        identity = (entry.principal_type, entry.principal_id, entry.effect)
        if identity in seen:
            raise ValidationError(
                "Duplicate ACL entry: "
                f"{entry.principal_type.value}:{entry.principal_id}:{entry.effect.value}"
            )
        seen.add(identity)


@dataclass(slots=True)
class Document:
    """文件 aggregate root；管理 metadata、目前版本、ACL 與 soft-delete。"""

    id: UUID
    source_type: str
    source_item_id: str
    title: str
    document_type: str
    owner_department: str
    document_owner: str
    confidentiality: Confidentiality
    status: DocumentStatus
    language: str
    external_llm_allowed: bool
    source_path: str | None = None
    source_url: str | None = None
    current_version_id: UUID | None = None
    custom_metadata: dict[str, Any] = field(default_factory=dict)
    acl: list[AccessControlEntry] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        required = {
            "source_type": self.source_type,
            "source_item_id": self.source_item_id,
            "title": self.title,
            "document_type": self.document_type,
            "owner_department": self.owner_department,
            "document_owner": self.document_owner,
            "language": self.language,
        }
        # list comprehension：從 dict.items() 篩出所有空白欄位名稱。
        blank = [name for name, value in required.items() if not value.strip()]
        if blank:
            raise ValidationError(f"Required document fields are blank: {', '.join(blank)}")
        _validate_unique_acl(self.acl)
        if self.confidentiality in {
            Confidentiality.CONFIDENTIAL,
            Confidentiality.RESTRICTED,
        } and not any(entry.effect == PermissionEffect.ALLOW for entry in self.acl):
            raise ValidationError(
                "Confidential and restricted documents require at least one allow ACL"
            )

    @property
    def is_acl_public(self) -> bool:
        """沒有任何 allow 時代表一般公司內部文件；deny 仍然有效。"""

        return not any(entry.effect == PermissionEffect.ALLOW for entry in self.acl)

    @property
    def allowed_user_ids(self) -> list[str]:
        return [
            entry.principal_id
            for entry in self.acl
            if entry.principal_type == PrincipalType.USER and entry.effect == PermissionEffect.ALLOW
        ]

    @property
    def allowed_group_ids(self) -> list[str]:
        return [
            entry.principal_id
            for entry in self.acl
            if entry.principal_type == PrincipalType.GROUP
            and entry.effect == PermissionEffect.ALLOW
        ]

    @property
    def denied_user_ids(self) -> list[str]:
        return [
            entry.principal_id
            for entry in self.acl
            if entry.principal_type == PrincipalType.USER and entry.effect == PermissionEffect.DENY
        ]

    @property
    def denied_group_ids(self) -> list[str]:
        return [
            entry.principal_id
            for entry in self.acl
            if entry.principal_type == PrincipalType.GROUP and entry.effect == PermissionEffect.DENY
        ]


@dataclass(slots=True)
class DocumentVersion:
    """文件不可變內容的一個版本及其生命週期／embedding 身分。"""

    id: UUID
    document_id: UUID
    version_number: str
    source_version: str
    content_hash: str
    mime_type: str
    file_size: int
    effective_at: date | None
    expires_at: date | None
    review_at: date | None
    parser_name: str
    parser_version: str
    embedding_model: str
    embedding_model_revision: str
    embedding_dimension: int
    chunk_count: int
    indexing_status: IndexingStatus = IndexingStatus.PENDING
    indexed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.version_number.strip():
            raise ValidationError("version_number must not be blank")
        if not self.source_version.strip():
            raise ValidationError("source_version must not be blank")
        if not self.content_hash.strip():
            raise ValidationError("content_hash must not be blank")
        if not self.embedding_model.strip() or not self.embedding_model_revision.strip():
            raise ValidationError("Embedding model and revision must not be blank")
        if self.file_size < 0:
            raise ValidationError("file_size must not be negative")
        if self.embedding_dimension <= 0:
            raise ValidationError("embedding_dimension must be positive")
        if self.chunk_count <= 0:
            raise ValidationError("A document version must contain at least one chunk")
        if self.expires_at and self.effective_at and self.expires_at < self.effective_at:
            raise ValidationError("expires_at must not be before effective_at")


@dataclass(slots=True)
class Chunk:
    """可獨立 embedding 與引用的最小檢索內容。"""

    id: UUID
    document_id: UUID
    version_id: UUID
    chunk_index: int
    content: str
    content_hash: str
    section_path: str | None
    page_number: int | None
    token_count: int | None
    qdrant_point_id: str | None = None
    indexing_status: IndexingStatus = IndexingStatus.PENDING
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.chunk_index < 0:
            raise ValidationError("chunk_index must not be negative")
        if not self.content.strip():
            raise ValidationError("Chunk content must not be blank")
        if not self.content_hash.strip():
            raise ValidationError("Chunk content_hash must not be blank")
        if self.page_number is not None and self.page_number <= 0:
            raise ValidationError("page_number must be positive")
        if self.token_count is not None and self.token_count <= 0:
            raise ValidationError("token_count must be positive")


@dataclass(slots=True)
class IndexingJob:
    """Client 可查詢的長時間索引工作狀態。"""

    id: UUID
    job_type: JobType
    document_id: UUID
    version_id: UUID | None
    status: JobStatus = JobStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 3
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class OutboxEvent:
    """與 Domain 寫入同交易保存、供 Worker 至少一次處理的事件。"""

    id: UUID
    event_type: JobType
    aggregate_id: UUID
    job_id: UUID
    payload: dict[str, Any]
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    available_at: datetime = field(default_factory=utc_now)
    locked_at: datetime | None = None
    processed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """Application 傳給 vector adapter 的資料，不依賴 Qdrant SDK class。"""

    point_id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VectorSearchHit:
    """Vector adapter 回傳的候選；尚未通過 PostgreSQL canonical recheck。"""

    point_id: str
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """向量初篩與 PostgreSQL canonical recheck 共用的搜尋條件。"""

    user_id: str | None = None
    group_ids: tuple[str, ...] = ()
    statuses: tuple[DocumentStatus, ...] = (DocumentStatus.PUBLISHED,)
    confidentialities: tuple[Confidentiality, ...] = ()
    language: str | None = None
