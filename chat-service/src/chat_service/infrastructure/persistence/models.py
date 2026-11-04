"""``chat`` schema 的 SQLAlchemy ORM models。

ORM class 只描述 PostgreSQL table shape，刻意不承載 Domain entity 的商業方法。Repository
負責在 ORM 與 Domain 之間轉換，避免 SQLAlchemy session 或 lazy-loading 行為滲入核心層。
JSONB 欄位只保存規格允許的結構化結果；本 module 不定義任何禁止持久化的敏感內容欄位。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "chat"

TURN_STATES = (
    "accepted",
    "waiting_for_capacity",
    "retrieving",
    "generating",
    "completed",
    "failed",
    "failed_unknown",
)
RETRIEVAL_STATUSES = (
    "evidence_found",
    "external_generation_blocked",
    "insufficient_evidence",
)
ANSWER_STATUSES = ("answered", "unable_to_answer")
CHAT_OUTCOMES = (
    "answered",
    "unable_to_answer",
    "external_generation_blocked",
    "insufficient_evidence",
    "outbound_policy_blocked",
)


def _sql_values(values: tuple[str, ...]) -> str:
    """把受控常數轉成 CHECK 使用的 SQL literal list。

    呼叫端只能傳本 module 的固定常數，不能傳入 runtime 或使用者輸入。集中產生字串可避免
    ORM 與 migration 的有限狀態集合因手寫拼字而不一致。
    """

    return ", ".join(f"'{value}'" for value in values)


class Base(DeclarativeBase):
    """Chat persistence adapters 共用的 declarative base。"""

    metadata = MetaData(schema=SCHEMA)


class ConversationModel(Base):
    """``chat.conversations`` aggregate root 的純 ORM mapping。"""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversations_status",
        ),
        CheckConstraint(
            "next_turn_ordinal > 0",
            name="ck_conversations_next_turn_ordinal_positive",
        ),
        CheckConstraint(
            "next_message_ordinal > 0",
            name="ck_conversations_next_message_ordinal_positive",
        ),
        CheckConstraint("row_version > 0", name="ck_conversations_row_version_positive"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    owner_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    next_turn_ordinal: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    next_message_ordinal: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    row_version: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
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
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


Index(
    "ix_conversations_owner_activity",
    ConversationModel.owner_issuer,
    ConversationModel.owner_subject,
    ConversationModel.last_activity_at.desc(),
    ConversationModel.id.desc(),
)


class TurnModel(Base):
    """``chat.turns`` 的持久狀態、冪等鍵與安全 diagnostics mapping。"""

    __tablename__ = "turns"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_turns_ordinal_positive"),
        CheckConstraint(
            "char_length(idempotency_key) = 36",
            name="ck_turns_idempotency_key_length",
        ),
        CheckConstraint(
            "char_length(request_fingerprint) = 64",
            name="ck_turns_request_fingerprint_length",
        ),
        CheckConstraint(
            "char_length(original_question) BETWEEN 1 AND 20000",
            name="ck_turns_original_question_length",
        ),
        CheckConstraint(
            "char_length(processed_query) BETWEEN 1 AND 20000",
            name="ck_turns_processed_query_length",
        ),
        CheckConstraint(
            f"state IN ({_sql_values(TURN_STATES)})",
            name="ck_turns_state",
        ),
        CheckConstraint(
            f"retrieval_status IS NULL OR retrieval_status IN ({_sql_values(RETRIEVAL_STATUSES)})",
            name="ck_turns_retrieval_status",
        ),
        CheckConstraint(
            f"answer_status IS NULL OR answer_status IN ({_sql_values(ANSWER_STATUSES)})",
            name="ck_turns_answer_status",
        ),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_sql_values(CHAT_OUTCOMES)})",
            name="ck_turns_outcome",
        ),
        CheckConstraint(
            "retry_after_seconds IS NULL OR retry_after_seconds >= 0",
            name="ck_turns_retry_after_non_negative",
        ),
        UniqueConstraint(
            "conversation_id",
            "ordinal",
            name="uq_turns_conversation_ordinal",
        ),
        UniqueConstraint(
            "conversation_id",
            "idempotency_key",
            name="uq_turns_conversation_idempotency",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(36), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    original_question: Mapped[str] = mapped_column(Text, nullable=False)
    processed_query: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    retrieval_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    answer_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    response_http_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    safe_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    safe_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_retryable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    generation_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageModel(Base):
    """``chat.messages`` 的 user/assistant payload mapping。"""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_messages_ordinal_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            f"response_kind IS NULL OR response_kind IN ({_sql_values(CHAT_OUTCOMES)})",
            name="ck_messages_response_kind",
        ),
        CheckConstraint(
            "payload_schema_version > 0",
            name="ck_messages_payload_schema_version_positive",
        ),
        UniqueConstraint("turn_id", "role", name="uq_messages_turn_role"),
        UniqueConstraint(
            "conversation_id",
            "ordinal",
            name="uq_messages_conversation_ordinal",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    turn_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.turns.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    response_kind: Mapped[str | None] = mapped_column(String(40), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    answer_parts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    payload_schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class CitationModel(Base):
    """``chat.citations`` 的安全引用與稽核用 machine UUID mapping。"""

    __tablename__ = "citations"
    __table_args__ = (
        CheckConstraint("ordinal > 0", name="ck_citations_ordinal_positive"),
        CheckConstraint(
            "page_number IS NULL OR page_number > 0",
            name="ck_citations_page_number_positive",
        ),
        UniqueConstraint(
            "assistant_message_id",
            "citation_id",
            name="uq_citations_message_citation_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    assistant_message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    citation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(1000), nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PurgePreviewModel(Base):
    """``chat.purge_previews`` 的 content-free selector snapshot。"""

    __tablename__ = "purge_previews"
    __table_args__ = (
        CheckConstraint(
            "char_length(selector_digest) = 64",
            name="ck_purge_previews_digest_length",
        ),
        CheckConstraint(
            "conversation_count >= 0",
            name="ck_purge_previews_conversation_count_non_negative",
        ),
        CheckConstraint(
            "message_count >= 0",
            name="ck_purge_previews_message_count_non_negative",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    selector: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    selected_conversations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    selector_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    creator_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    creator_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )


class PurgeAuditModel(Base):
    """``chat.purge_audit`` 的永久 content-free 稽核紀錄。"""

    __tablename__ = "purge_audit"
    __table_args__ = (
        CheckConstraint(
            "char_length(selector_digest) = 64",
            name="ck_purge_audit_digest_length",
        ),
        CheckConstraint(
            "conversation_count >= 0",
            name="ck_purge_audit_conversation_count_non_negative",
        ),
        CheckConstraint(
            "message_count >= 0",
            name="ck_purge_audit_message_count_non_negative",
        ),
        CheckConstraint(
            "char_length(reason) BETWEEN 1 AND 500",
            name="ck_purge_audit_reason_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    actor_issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(512), nullable=False)
    selector_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
