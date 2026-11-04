"""建立 Chat Service 的六張持久化資料表。

Revision ID: 20260818_0001
Revises:
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260818_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "chat"


def _timestamp(
    name: str,
    *,
    nullable: bool,
    server_default: sa.TextClause | None = None,
) -> sa.Column:
    """建立 PostgreSQL timestamptz 欄位。集中維持 migration 型別一致。"""

    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=server_default,
    )


def upgrade() -> None:
    """建立 chat schema、六張 table、constraints 與 owner keyset index。"""

    op.execute(sa.text('CREATE SCHEMA IF NOT EXISTS "chat"'))
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_issuer", sa.String(length=255), nullable=False),
        sa.Column("owner_subject", sa.String(length=512), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "next_turn_ordinal",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "next_message_ordinal",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "row_version",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        _timestamp("created_at", nullable=False, server_default=sa.text("now()")),
        _timestamp("updated_at", nullable=False, server_default=sa.text("now()")),
        _timestamp("last_activity_at", nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversations_status",
        ),
        sa.CheckConstraint(
            "next_turn_ordinal > 0",
            name="ck_conversations_next_turn_ordinal_positive",
        ),
        sa.CheckConstraint(
            "next_message_ordinal > 0",
            name="ck_conversations_next_message_ordinal_positive",
        ),
        sa.CheckConstraint(
            "row_version > 0",
            name="ck_conversations_row_version_positive",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_conversations_owner_activity",
        "conversations",
        [
            "owner_issuer",
            "owner_subject",
            sa.text("last_activity_at DESC"),
            sa.text("id DESC"),
        ],
        schema=SCHEMA,
    )

    op.create_table(
        "turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column("original_question", sa.Text(), nullable=False),
        sa.Column("processed_query", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=True),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("retrieval_status", sa.String(length=40), nullable=True),
        sa.Column("answer_status", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=40), nullable=True),
        sa.Column("response_http_status", sa.SmallInteger(), nullable=True),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column("safe_error_message", sa.Text(), nullable=True),
        sa.Column(
            "error_retryable",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("retry_after_seconds", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("retrieval_diagnostics", postgresql.JSONB(), nullable=True),
        sa.Column("generation_diagnostics", postgresql.JSONB(), nullable=True),
        _timestamp("created_at", nullable=False, server_default=sa.text("now()")),
        _timestamp("updated_at", nullable=False, server_default=sa.text("now()")),
        _timestamp("started_at", nullable=True),
        _timestamp("completed_at", nullable=True),
        sa.CheckConstraint("ordinal > 0", name="ck_turns_ordinal_positive"),
        sa.CheckConstraint(
            "char_length(idempotency_key) = 36",
            name="ck_turns_idempotency_key_length",
        ),
        sa.CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="ck_turns_request_fingerprint_length",
        ),
        sa.CheckConstraint(
            "char_length(original_question) BETWEEN 1 AND 20000",
            name="ck_turns_original_question_length",
        ),
        sa.CheckConstraint(
            "char_length(processed_query) BETWEEN 1 AND 20000",
            name="ck_turns_processed_query_length",
        ),
        sa.CheckConstraint(
            "state IN ('accepted', 'waiting_for_capacity', 'retrieving', 'generating', "
            "'completed', 'failed', 'failed_unknown')",
            name="ck_turns_state",
        ),
        sa.CheckConstraint(
            "retrieval_status IS NULL OR retrieval_status IN "
            "('evidence_found', 'external_generation_blocked', 'insufficient_evidence')",
            name="ck_turns_retrieval_status",
        ),
        sa.CheckConstraint(
            "answer_status IS NULL OR answer_status IN ('answered', 'unable_to_answer')",
            name="ck_turns_answer_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('answered', 'unable_to_answer', 'external_generation_blocked', "
            "'insufficient_evidence', 'outbound_policy_blocked')",
            name="ck_turns_outcome",
        ),
        sa.CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds >= 0",
            name="ck_turns_retry_after_non_negative",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{SCHEMA}.conversations.id"],
            name="fk_turns_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_turns"),
        sa.UniqueConstraint(
            "conversation_id",
            "ordinal",
            name="uq_turns_conversation_ordinal",
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_turns_conversation_idempotency",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("response_kind", sa.String(length=40), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer_parts", postgresql.JSONB(), nullable=True),
        sa.Column(
            "payload_schema_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        _timestamp("created_at", nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("ordinal > 0", name="ck_messages_ordinal_positive"),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_messages_role",
        ),
        sa.CheckConstraint(
            "response_kind IS NULL OR response_kind IN "
            "('answered', 'unable_to_answer', 'external_generation_blocked', "
            "'insufficient_evidence', 'outbound_policy_blocked')",
            name="ck_messages_response_kind",
        ),
        sa.CheckConstraint(
            "payload_schema_version > 0",
            name="ck_messages_payload_schema_version_positive",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{SCHEMA}.conversations.id"],
            name="fk_messages_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            [f"{SCHEMA}.turns.id"],
            name="fk_messages_turn_id_turns",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.UniqueConstraint("turn_id", "role", name="uq_messages_turn_role"),
        sa.UniqueConstraint(
            "conversation_id",
            "ordinal",
            name="uq_messages_conversation_ordinal",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "citations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assistant_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("citation_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("section_path", sa.String(length=2000), nullable=True),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.CheckConstraint("ordinal > 0", name="ck_citations_ordinal_positive"),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number > 0",
            name="ck_citations_page_number_positive",
        ),
        sa.ForeignKeyConstraint(
            ["assistant_message_id"],
            [f"{SCHEMA}.messages.id"],
            name="fk_citations_assistant_message_id_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_citations"),
        sa.UniqueConstraint(
            "assistant_message_id",
            "citation_id",
            name="uq_citations_message_citation_id",
        ),
        schema=SCHEMA,
    )

    op.create_table(
        "purge_previews",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selector", postgresql.JSONB(), nullable=False),
        sa.Column("selected_conversations", postgresql.JSONB(), nullable=False),
        sa.Column("selector_digest", sa.String(length=64), nullable=False),
        sa.Column("conversation_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("creator_issuer", sa.String(length=255), nullable=False),
        sa.Column("creator_subject", sa.String(length=512), nullable=False),
        _timestamp("expires_at", nullable=False),
        _timestamp("consumed_at", nullable=True),
        _timestamp("created_at", nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "char_length(selector_digest) = 64",
            name="ck_purge_previews_digest_length",
        ),
        sa.CheckConstraint(
            "conversation_count >= 0",
            name="ck_purge_previews_conversation_count_non_negative",
        ),
        sa.CheckConstraint(
            "message_count >= 0",
            name="ck_purge_previews_message_count_non_negative",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_purge_previews"),
        schema=SCHEMA,
    )

    op.create_table(
        "purge_audit",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_issuer", sa.String(length=255), nullable=False),
        sa.Column("actor_subject", sa.String(length=512), nullable=False),
        sa.Column("selector_digest", sa.String(length=64), nullable=False),
        sa.Column("conversation_count", sa.Integer(), nullable=False),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        _timestamp("purged_at", nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "char_length(selector_digest) = 64",
            name="ck_purge_audit_digest_length",
        ),
        sa.CheckConstraint(
            "conversation_count >= 0",
            name="ck_purge_audit_conversation_count_non_negative",
        ),
        sa.CheckConstraint(
            "message_count >= 0",
            name="ck_purge_audit_message_count_non_negative",
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500",
            name="ck_purge_audit_reason_length",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_purge_audit"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    """依 FK 反向順序移除六張業務表。Alembic 自行管理 version table。"""

    op.drop_table("purge_audit", schema=SCHEMA)
    op.drop_table("purge_previews", schema=SCHEMA)
    op.drop_table("citations", schema=SCHEMA)
    op.drop_table("messages", schema=SCHEMA)
    op.drop_table("turns", schema=SCHEMA)
    op.drop_index(
        "ix_conversations_owner_activity",
        table_name="conversations",
        schema=SCHEMA,
    )
    op.drop_table("conversations", schema=SCHEMA)
