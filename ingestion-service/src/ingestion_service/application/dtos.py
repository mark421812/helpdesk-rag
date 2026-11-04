"""Ingestion Application layer 的 immutable DTO 與 read views。

DTO 隔離 Domain、HTTP 與 KIS JSON contract；frozen dataclass 建立後不可修改，適合跨 use
case 邊界傳遞可信快照。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from ingestion_service.domain.entities import AccessControlEntry, Chunk
from ingestion_service.domain.enums import (
    Confidentiality,
    DocumentStatus,
    IngestionJobStatus,
    IngestionJobType,
    KnowledgeIndexingStatus,
    KnowledgeIndexJobStatus,
    SourceItemStatus,
    SyncRunStatus,
)


@dataclass(frozen=True, slots=True)
class KnowledgeIndexDocumentSubmission:
    """送往 KIS 建立／更新文件的完整 command DTO（含 chunks 與 ACL）。"""

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
    chunks: tuple[Chunk, ...]
    source_path: str | None = None
    source_url: str | None = None
    effective_at: date | None = None
    expires_at: date | None = None
    review_at: date | None = None
    custom_metadata: dict[str, Any] = field(default_factory=dict)
    acl: tuple[AccessControlEntry, ...] = ()


@dataclass(frozen=True, slots=True)
class KnowledgeIndexMetadataPatch:
    """內容未變時，只更新 KIS metadata 所需的 DTO。"""

    source_version: str
    version_number: str
    title: str
    document_type: str
    owner_department: str
    document_owner: str
    confidentiality: Confidentiality
    status: DocumentStatus
    language: str
    external_llm_allowed: bool
    source_path: str | None
    source_url: str | None
    effective_at: date | None
    expires_at: date | None
    review_at: date | None
    custom_metadata: dict[str, Any]
    acl: tuple[AccessControlEntry, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeIndexSubmissionResult:
    """KIS 接受文件後回傳的識別碼與索引狀態。"""

    document_id: UUID
    version_id: UUID
    job_id: UUID | None
    indexing_status: KnowledgeIndexingStatus
    created: bool


@dataclass(frozen=True, slots=True)
class KnowledgeIndexJobResult:
    """Ingestion 輪詢 KIS 非同步工作的最小結果。"""

    job_id: UUID
    document_id: UUID
    version_id: UUID | None
    status: KnowledgeIndexJobStatus
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ScanSourceResult:
    """一次來源掃描的計數摘要；API 與 CLI 都使用相同結果。"""

    run_id: UUID
    source_id: UUID
    status: SyncRunStatus
    discovered_count: int
    queued_count: int
    missing_count: int
    delete_queued_count: int
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class IngestFileResult:
    """單檔發現／排程結果；queued=False 可能代表同一 job 已存在。"""

    run_id: UUID
    item: SourceItemView
    job_id: UUID | None
    queued: bool


@dataclass(frozen=True, slots=True)
class ProcessJobResult:
    """Worker 處理一個 job 後的狀態快照。"""

    job_id: UUID
    item_id: UUID
    job_type: IngestionJobType
    status: IngestionJobStatus
    item_status: SourceItemStatus
    deferred: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SourceItemView:
    """讀取模型：對外提供 SourceItem 必要欄位，不暴露可變 domain entity。"""

    id: UUID
    source_id: UUID
    source_item_id: str
    source_path: str
    source_version: str
    mime_type: str | None
    file_size: int
    status: SourceItemStatus
    missing_scan_count: int
    knowledge_index_document_id: UUID | None
    knowledge_index_version_id: UUID | None
    knowledge_index_job_id: UUID | None
    error_code: str | None
    error_message: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IngestionJobView:
    """讀取模型：呈現 job、lease、重試及錯誤資訊。"""

    id: UUID
    source_id: UUID
    item_id: UUID
    job_type: IngestionJobType
    status: IngestionJobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    leased_by: str | None
    lease_expires_at: datetime | None
    error_code: str | None
    error_message: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SyncRunView:
    """讀取模型：呈現一次 scan run 的統計與完成時間。"""

    id: UUID
    source_id: UUID
    status: SyncRunStatus
    discovered_count: int
    queued_count: int
    missing_count: int
    delete_queued_count: int
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None
