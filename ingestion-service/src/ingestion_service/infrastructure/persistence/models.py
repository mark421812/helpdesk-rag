"""Ingestion schema 的 SQLAlchemy ORM models；不等同於 Domain entities。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "ingestion"


class Base(DeclarativeBase):
    """所有 ingestion ORM models 的 SQLAlchemy declarative base。"""

    metadata = MetaData(schema=SCHEMA)


class SourceModel(Base):
    """``ingestion.sources`` table 的 ORM mapping，不承載 Domain 商業方法。"""

    __tablename__ = "sources"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_extensions: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
    )
    max_file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schedule_cron: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    last_successful_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SyncRunModel(Base):
    """``ingestion.sync_runs`` table mapping。"""

    __tablename__ = "sync_runs"
    __table_args__ = (Index("ix_sync_runs_source_created", "source_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    discovered_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delete_queued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class SourceItemModel(Base):
    """``ingestion.source_items`` table mapping；保存文件狀態與 KIS IDs。"""

    __tablename__ = "source_items"
    __table_args__ = (
        UniqueConstraint("source_id", "source_item_hash", name="uq_source_items_identity"),
        Index("ix_source_items_status", "status"),
        Index("ix_source_items_last_seen", "source_id", "last_seen_run_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_item_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    absolute_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_version: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifecycle_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    missing_scan_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.sync_runs.id", ondelete="SET NULL"),
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_successful_content_hash: Mapped[str | None] = mapped_column(String(64))
    last_successful_metadata_hash: Mapped[str | None] = mapped_column(String(64))
    pending_content_hash: Mapped[str | None] = mapped_column(String(64))
    pending_metadata_hash: Mapped[str | None] = mapped_column(String(64))
    pending_operation: Mapped[str | None] = mapped_column(String(40))
    restore_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    knowledge_index_document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    knowledge_index_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    knowledge_index_job_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    error_code: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionJobModel(Base):
    """``ingestion.jobs`` table mapping；PostgreSQL 同時扮演可靠工作佇列。"""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ingestion_jobs_idempotency"),
        Index("ix_ingestion_jobs_claim", "status", "available_at", "created_at"),
        Index("ix_ingestion_jobs_item", "item_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.source_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    sync_run_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.sync_runs.id", ondelete="SET NULL"),
    )
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    leased_by: Mapped[str | None] = mapped_column(String(255))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
