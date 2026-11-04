"""Ingestion HTTP request/response 的 Pydantic schemas。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ingestion_service.domain.enums import (
    IngestionJobStatus,
    IngestionJobType,
    SourceItemStatus,
    SourceType,
    SyncRunStatus,
)


class AttributeModel(BaseModel):
    """共用 Pydantic 設定：允許直接從 dataclass／entity attributes 建立 response。"""

    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    """統一錯誤 response contract。"""

    error: str
    message: str
    request_id: str | None = None


class SourceResponse(AttributeModel):
    """文件來源設定的公開 response。"""

    id: UUID
    name: str
    source_type: SourceType
    root_path: str
    enabled: bool
    allowed_extensions: tuple[str, ...]
    max_file_size_bytes: int
    schedule_cron: str
    timezone: str
    last_successful_scan_at: datetime | None


class ScanSourceResponse(AttributeModel):
    """完整掃描的計數摘要 response。"""

    run_id: UUID
    source_id: UUID
    status: SyncRunStatus
    discovered_count: int
    queued_count: int
    missing_count: int
    delete_queued_count: int
    error_message: str | None


class IngestFileRequest(BaseModel):
    """單檔匯入 request；更細的 path 安全規則由 Application 驗證。"""

    source_item_id: str = Field(min_length=1, max_length=4096)


class SourceItemResponse(AttributeModel):
    """單份文件的處理與 KIS 同步狀態。"""

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


class IngestFileResponse(AttributeModel):
    """單檔匯入是否成功排程及其 job ID。"""

    run_id: UUID
    item: SourceItemResponse
    job_id: UUID | None
    queued: bool


class IngestionJobResponse(AttributeModel):
    """Background job、lease、retry 與錯誤資訊。"""

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


class SyncRunResponse(AttributeModel):
    """供後續查詢的 scan run 詳細資料。"""

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


class HealthResponse(BaseModel):
    """Liveness/readiness response。"""

    status: str
    checks: dict[str, bool]
