"""Create ingestion sources, runs, items and jobs.

Revision ID: 20260802_0001
Revises:
Create Date: 2026-08-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ingestion"


def _timestamp(name: str, *, nullable: bool = True) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.execute(sa.text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("allowed_extensions", postgresql.JSONB(), nullable=False),
        sa.Column("max_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("schedule_cron", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        _timestamp("last_successful_scan_at"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        schema=SCHEMA,
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("discovered_count", sa.Integer(), nullable=False),
        sa.Column("queued_count", sa.Integer(), nullable=False),
        sa.Column("missing_count", sa.Integer(), nullable=False),
        sa.Column("delete_queued_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        _timestamp("started_at", nullable=False),
        _timestamp("completed_at"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_id"], [f"{SCHEMA}.sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_sync_runs_source_created",
        "sync_runs",
        ["source_id", "created_at"],
        schema=SCHEMA,
    )
    op.create_table(
        "source_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_item_id", sa.Text(), nullable=False),
        sa.Column("source_item_hash", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("absolute_path", sa.Text(), nullable=False),
        sa.Column("source_version", sa.String(length=255), nullable=False),
        sa.Column("metadata_source_hash", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        _timestamp("modified_at", nullable=False),
        sa.Column("lifecycle_generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("missing_scan_count", sa.Integer(), nullable=False),
        sa.Column("last_seen_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        _timestamp("last_seen_at"),
        sa.Column("last_successful_content_hash", sa.String(length=64), nullable=True),
        sa.Column("last_successful_metadata_hash", sa.String(length=64), nullable=True),
        sa.Column("pending_content_hash", sa.String(length=64), nullable=True),
        sa.Column("pending_metadata_hash", sa.String(length=64), nullable=True),
        sa.Column("pending_operation", sa.String(length=40), nullable=True),
        sa.Column("restore_requested", sa.Boolean(), nullable=False),
        sa.Column("knowledge_index_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_index_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("knowledge_index_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _timestamp("completed_at"),
        _timestamp("deleted_at"),
        sa.ForeignKeyConstraint(["source_id"], [f"{SCHEMA}.sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["last_seen_run_id"],
            [f"{SCHEMA}.sync_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "source_item_hash", name="uq_source_items_identity"),
        schema=SCHEMA,
    )
    op.create_index("ix_source_items_status", "source_items", ["status"], schema=SCHEMA)
    op.create_index(
        "ix_source_items_last_seen",
        "source_items",
        ["source_id", "last_seen_run_id"],
        schema=SCHEMA,
    )
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sync_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("job_type", sa.String(length=30), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        _timestamp("available_at", nullable=False),
        sa.Column("leased_by", sa.String(length=255), nullable=True),
        _timestamp("lease_expires_at"),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        _timestamp("completed_at"),
        sa.ForeignKeyConstraint(["source_id"], [f"{SCHEMA}.sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["item_id"],
            [f"{SCHEMA}.source_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            [f"{SCHEMA}.sync_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_ingestion_jobs_idempotency"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ingestion_jobs_claim",
        "jobs",
        ["status", "available_at", "created_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_ingestion_jobs_item",
        "jobs",
        ["item_id", "created_at"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_item", table_name="jobs", schema=SCHEMA)
    op.drop_index("ix_ingestion_jobs_claim", table_name="jobs", schema=SCHEMA)
    op.drop_table("jobs", schema=SCHEMA)
    op.drop_index("ix_source_items_last_seen", table_name="source_items", schema=SCHEMA)
    op.drop_index("ix_source_items_status", table_name="source_items", schema=SCHEMA)
    op.drop_table("source_items", schema=SCHEMA)
    op.drop_index("ix_sync_runs_source_created", table_name="sync_runs", schema=SCHEMA)
    op.drop_table("sync_runs", schema=SCHEMA)
    op.drop_table("sources", schema=SCHEMA)
