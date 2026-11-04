"""SQLAlchemy ORM persistence models。

這些 class 描述 PostgreSQL table shape；它們是 Infrastructure 資料模型，不是 Domain
entities。Repository 負責在兩種模型之間轉換，避免 Domain 綁定 ORM session。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 KIS ORM models 的 SQLAlchemy declarative base。"""

    pass


class DocumentModel(Base):
    """documents table；保存目前可治理的文件 aggregate 欄位。"""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_type", "source_item_id", name="uq_documents_source"),
        Index("ix_documents_status_confidentiality", "status", "confidentiality"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_item_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)
    owner_department: Mapped[str] = mapped_column(String(255), nullable=False)
    document_owner: Mapped[str] = mapped_column(String(255), nullable=False)
    confidentiality: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    language: Mapped[str] = mapped_column(String(20), nullable=False)
    external_llm_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_path: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    current_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    custom_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentAclModel(Base):
    """一文件多筆 ACL 的正規化資料表。"""

    __tablename__ = "document_acl"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "effect",
            name="uq_document_acl_entry",
        ),
        Index("ix_document_acl_principal", "principal_type", "principal_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    principal_type: Mapped[str] = mapped_column(String(20), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(512), nullable=False)
    effect: Mapped[str] = mapped_column(String(20), nullable=False)


class DocumentVersionModel(Base):
    """不可變內容版本與 parser/model/lifecycle metadata。"""

    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "content_hash",
            "embedding_model",
            "embedding_model_revision",
            name="uq_document_versions_fingerprint",
        ),
        Index("ix_document_versions_status", "indexing_status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_number: Mapped[str] = mapped_column(String(100), nullable=False)
    source_version: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[date | None] = mapped_column(Date)
    expires_at: Mapped[date | None] = mapped_column(Date)
    review_at: Mapped[date | None] = mapped_column(Date)
    parser_name: Mapped[str] = mapped_column(String(100), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_model_revision: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False)
    indexing_status: Mapped[str] = mapped_column(String(30), nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class ChunkModel(Base):
    """完整 chunk 內容與 citation/indexing 欄位。"""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "chunk_index", name="uq_chunks_version_index"),
        Index("ix_chunks_document_version", "document_id", "version_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section_path: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(64))
    indexing_status: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class IndexingJobModel(Base):
    """供 API 查詢的非同步 job table。"""

    __tablename__ = "indexing_jobs"
    __table_args__ = (
        Index("ix_indexing_jobs_document", "document_id", "created_at"),
        Index("ix_indexing_jobs_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("document_versions.id", ondelete="CASCADE"),
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error_code: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class OutboxEventModel(Base):
    """與 Domain 寫入同交易保存的 durable outbox table。"""

    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_claim", "status", "available_at", "created_at"),)

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    job_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("indexing_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
