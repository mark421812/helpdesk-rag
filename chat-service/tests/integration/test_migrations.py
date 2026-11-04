"""Integration coverage for the PostgreSQL schema and ORM boundary."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.domain.entities import (
    ChatTurn,
    Citation,
    Conversation,
    Message,
    PurgeAudit,
    PurgePreview,
)
from chat_service.infrastructure.persistence.models import (
    SCHEMA,
    Base,
    CitationModel,
    ConversationModel,
    MessageModel,
    PurgeAuditModel,
    PurgePreviewModel,
    TurnModel,
)

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_BUSINESS_TABLES = {
    "citations",
    "conversations",
    "messages",
    "purge_audit",
    "purge_previews",
    "turns",
}
EXPECTED_COLUMNS: dict[
    str,
    tuple[tuple[str, str, int | None, bool, str | None], ...],
] = {
    "conversations": (
        ("id", "uuid", None, False, None),
        ("owner_issuer", "character varying", 255, False, None),
        ("owner_subject", "character varying", 512, False, None),
        ("tenant_id", "character varying", 255, True, None),
        ("title", "character varying", 200, True, None),
        ("status", "character varying", 20, False, None),
        ("next_turn_ordinal", "integer", None, False, "1"),
        ("next_message_ordinal", "integer", None, False, "1"),
        ("row_version", "bigint", None, False, "1"),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
        ("last_activity_at", "timestamp with time zone", None, False, "now()"),
    ),
    "turns": (
        ("id", "uuid", None, False, None),
        ("conversation_id", "uuid", None, False, None),
        ("ordinal", "integer", None, False, None),
        ("idempotency_key", "character varying", 36, False, None),
        ("request_fingerprint", "character", 64, False, None),
        ("original_question", "text", None, False, None),
        ("processed_query", "text", None, False, None),
        ("language", "character varying", 20, True, None),
        ("state", "character varying", 40, False, None),
        ("retrieval_status", "character varying", 40, True, None),
        ("answer_status", "character varying", 40, True, None),
        ("outcome", "character varying", 40, True, None),
        ("response_http_status", "smallint", None, True, None),
        ("safe_error_code", "character varying", 100, True, None),
        ("safe_error_message", "text", None, True, None),
        ("error_retryable", "boolean", None, False, "false"),
        ("retry_after_seconds", "integer", None, True, None),
        ("request_id", "character varying", 128, False, None),
        ("retrieval_diagnostics", "jsonb", None, True, None),
        ("generation_diagnostics", "jsonb", None, True, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
        ("updated_at", "timestamp with time zone", None, False, "now()"),
        ("started_at", "timestamp with time zone", None, True, None),
        ("completed_at", "timestamp with time zone", None, True, None),
    ),
    "messages": (
        ("id", "uuid", None, False, None),
        ("conversation_id", "uuid", None, False, None),
        ("turn_id", "uuid", None, False, None),
        ("ordinal", "integer", None, False, None),
        ("role", "character varying", 20, False, None),
        ("response_kind", "character varying", 40, True, None),
        ("content", "text", None, False, None),
        ("answer_parts", "jsonb", None, True, None),
        ("payload_schema_version", "integer", None, False, "1"),
        ("created_at", "timestamp with time zone", None, False, "now()"),
    ),
    "citations": (
        ("id", "uuid", None, False, None),
        ("assistant_message_id", "uuid", None, False, None),
        ("citation_id", "character varying", 128, False, None),
        ("ordinal", "integer", None, False, None),
        ("chunk_id", "uuid", None, False, None),
        ("document_id", "uuid", None, False, None),
        ("version_id", "uuid", None, False, None),
        ("title", "character varying", 1000, False, None),
        ("section_path", "character varying", 2000, True, None),
        ("page_number", "integer", None, True, None),
    ),
    "purge_previews": (
        ("id", "uuid", None, False, None),
        ("selector", "jsonb", None, False, None),
        ("selected_conversations", "jsonb", None, False, None),
        ("selector_digest", "character varying", 64, False, None),
        ("conversation_count", "integer", None, False, None),
        ("message_count", "integer", None, False, None),
        ("creator_issuer", "character varying", 255, False, None),
        ("creator_subject", "character varying", 512, False, None),
        ("expires_at", "timestamp with time zone", None, False, None),
        ("consumed_at", "timestamp with time zone", None, True, None),
        ("created_at", "timestamp with time zone", None, False, "now()"),
    ),
    "purge_audit": (
        ("id", "uuid", None, False, None),
        ("actor_issuer", "character varying", 255, False, None),
        ("actor_subject", "character varying", 512, False, None),
        ("selector_digest", "character varying", 64, False, None),
        ("conversation_count", "integer", None, False, None),
        ("message_count", "integer", None, False, None),
        ("reason", "character varying", 500, False, None),
        ("request_id", "character varying", 128, False, None),
        ("purged_at", "timestamp with time zone", None, False, "now()"),
    ),
}
EXPECTED_CHECKS = {
    "ck_citations_ordinal_positive",
    "ck_citations_page_number_positive",
    "ck_conversations_next_message_ordinal_positive",
    "ck_conversations_next_turn_ordinal_positive",
    "ck_conversations_row_version_positive",
    "ck_conversations_status",
    "ck_messages_ordinal_positive",
    "ck_messages_payload_schema_version_positive",
    "ck_messages_response_kind",
    "ck_messages_role",
    "ck_purge_audit_conversation_count_non_negative",
    "ck_purge_audit_digest_length",
    "ck_purge_audit_message_count_non_negative",
    "ck_purge_audit_reason_length",
    "ck_purge_previews_conversation_count_non_negative",
    "ck_purge_previews_digest_length",
    "ck_purge_previews_message_count_non_negative",
    "ck_turns_answer_status",
    "ck_turns_idempotency_key_length",
    "ck_turns_ordinal_positive",
    "ck_turns_original_question_length",
    "ck_turns_outcome",
    "ck_turns_processed_query_length",
    "ck_turns_request_fingerprint_length",
    "ck_turns_retrieval_status",
    "ck_turns_retry_after_non_negative",
    "ck_turns_state",
}
EXPECTED_FOREIGN_KEYS = {
    ("fk_citations_assistant_message_id_messages", "citations", "messages"),
    ("fk_messages_conversation_id_conversations", "messages", "conversations"),
    ("fk_messages_turn_id_turns", "messages", "turns"),
    ("fk_turns_conversation_id_conversations", "turns", "conversations"),
}
EXPECTED_UNIQUES = {
    (
        "turns",
        "uq_turns_conversation_ordinal",
        ("conversation_id", "ordinal"),
    ),
    (
        "turns",
        "uq_turns_conversation_idempotency",
        ("conversation_id", "idempotency_key"),
    ),
    ("messages", "uq_messages_turn_role", ("turn_id", "role")),
    (
        "messages",
        "uq_messages_conversation_ordinal",
        ("conversation_id", "ordinal"),
    ),
    (
        "citations",
        "uq_citations_message_citation_id",
        ("assistant_message_id", "citation_id"),
    ),
}
EXPECTED_PRIMARY_KEYS = {
    "alembic_version": ("version_num",),
    **{table_name: ("id",) for table_name in EXPECTED_BUSINESS_TABLES},
}


def _run_alembic(database_url: str, revision: str, *, downgrade: bool) -> None:
    """Run the real async Alembic environment from a worker thread."""

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    with patch.dict(os.environ, {"DATABASE_URL": database_url}):
        if downgrade:
            command.downgrade(alembic_config, revision)
        else:
            command.upgrade(alembic_config, revision)


async def test_alembic_upgrade_creates_chat_schema_and_six_tables(
    async_engine: AsyncEngine,
) -> None:
    async with async_engine.connect() as connection:
        schema_exists = await connection.scalar(
            text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'chat')")
        )
        tables = set(
            (
                await connection.scalars(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'chat' AND table_type = 'BASE TABLE'"
                    )
                )
            ).all()
        )
        revision = await connection.scalar(text("SELECT version_num FROM chat.alembic_version"))

    assert schema_exists is True
    assert tables == EXPECTED_BUSINESS_TABLES | {"alembic_version"}
    assert revision == "20260818_0001"


async def test_migration_columns_match_the_exact_postgresql_contract(
    async_engine: AsyncEngine,
) -> None:
    """Check every business column's order, native type, nullability, length, and default."""

    async with async_engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT table_name, column_name, data_type, "
                        "character_maximum_length, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_schema = 'chat' AND table_name <> 'alembic_version' "
                        "ORDER BY table_name, ordinal_position"
                    )
                )
            )
            .mappings()
            .all()
        )

    actual: dict[str, list[tuple[str, str, int | None, bool, str | None]]] = {}
    for row in rows:
        actual.setdefault(row["table_name"], []).append(
            (
                row["column_name"],
                row["data_type"],
                row["character_maximum_length"],
                row["is_nullable"] == "YES",
                row["column_default"],
            )
        )

    assert {table: tuple(columns) for table, columns in actual.items()} == EXPECTED_COLUMNS


async def test_migration_installs_checks_and_only_chat_local_foreign_keys(
    async_engine: AsyncEngine,
) -> None:
    async with async_engine.connect() as connection:
        check_names = set(
            (
                await connection.scalars(
                    text(
                        "SELECT constraint_record.conname "
                        "FROM pg_constraint constraint_record "
                        "JOIN pg_class table_relation "
                        "ON table_relation.oid = constraint_record.conrelid "
                        "JOIN pg_namespace namespace "
                        "ON namespace.oid = table_relation.relnamespace "
                        "WHERE namespace.nspname = 'chat' "
                        "AND constraint_record.contype = 'c'"
                    )
                )
            ).all()
        )
        foreign_keys = (
            (
                await connection.execute(
                    text(
                        "SELECT constraint_name, table_name, "
                        "unique_constraint_schema, unique_constraint_name "
                        "FROM information_schema.referential_constraints rc "
                        "JOIN information_schema.table_constraints tc "
                        "USING (constraint_catalog, constraint_schema, constraint_name) "
                        "WHERE rc.constraint_schema = 'chat'"
                    )
                )
            )
            .mappings()
            .all()
        )
        referenced_tables = (
            (
                await connection.execute(
                    text(
                        "SELECT c.conname, source.relname AS source_table, "
                        "target_ns.nspname AS target_schema, target.relname AS target_table, "
                        "c.confdeltype::text AS confdeltype "
                        "FROM pg_constraint c "
                        "JOIN pg_class source ON source.oid = c.conrelid "
                        "JOIN pg_namespace source_ns ON source_ns.oid = source.relnamespace "
                        "JOIN pg_class target ON target.oid = c.confrelid "
                        "JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace "
                        "WHERE c.contype = 'f' AND source_ns.nspname = 'chat'"
                    )
                )
            )
            .mappings()
            .all()
        )

    assert check_names == EXPECTED_CHECKS
    assert len(foreign_keys) == 4
    assert {row["unique_constraint_schema"] for row in foreign_keys} == {"chat"}
    assert {
        (row["conname"], row["source_table"], row["target_table"]) for row in referenced_tables
    } == EXPECTED_FOREIGN_KEYS
    assert {row["target_schema"] for row in referenced_tables} == {"chat"}
    assert {row["confdeltype"] for row in referenced_tables} == {"c"}


async def test_migration_installs_exact_primary_keys(async_engine: AsyncEngine) -> None:
    """Require one single-column primary key on every chat table."""

    async with async_engine.connect() as connection:
        primary_key_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT tc.table_name, "
                        "array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "USING (constraint_catalog, constraint_schema, constraint_name) "
                        "WHERE tc.constraint_schema = 'chat' "
                        "AND tc.constraint_type = 'PRIMARY KEY' "
                        "GROUP BY tc.table_name"
                    )
                )
            )
            .mappings()
            .all()
        )

    actual_primary_keys = {row["table_name"]: tuple(row["columns"]) for row in primary_key_rows}
    assert actual_primary_keys == EXPECTED_PRIMARY_KEYS


async def test_migration_installs_exact_uniques_and_owner_descending_index(
    async_engine: AsyncEngine,
) -> None:
    """Verify uniqueness keys and the owner keyset index's physical key ordering."""

    async with async_engine.connect() as connection:
        unique_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT tc.table_name, tc.constraint_name, "
                        "array_agg(kcu.column_name ORDER BY kcu.ordinal_position) AS columns "
                        "FROM information_schema.table_constraints tc "
                        "JOIN information_schema.key_column_usage kcu "
                        "USING (constraint_catalog, constraint_schema, constraint_name) "
                        "WHERE tc.constraint_schema = 'chat' "
                        "AND tc.constraint_type = 'UNIQUE' "
                        "GROUP BY tc.table_name, tc.constraint_name"
                    )
                )
            )
            .mappings()
            .all()
        )
        index_rows = (
            (
                await connection.execute(
                    text(
                        "SELECT keys.position, "
                        "attribute.attname AS column_name, "
                        "(i.indoption[(keys.position - 1)::integer]::integer & 1) = 1 "
                        "AS is_descending, "
                        "am.amname, i.indisunique, i.indisvalid, i.indisready, "
                        "i.indnkeyatts, i.indnatts "
                        "FROM pg_index i "
                        "JOIN pg_class index_relation ON index_relation.oid = i.indexrelid "
                        "JOIN pg_class table_relation ON table_relation.oid = i.indrelid "
                        "JOIN pg_namespace namespace "
                        "ON namespace.oid = table_relation.relnamespace "
                        "JOIN pg_am am ON am.oid = index_relation.relam "
                        "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY "
                        "AS keys(attnum, position) "
                        "JOIN pg_attribute attribute "
                        "ON attribute.attrelid = table_relation.oid "
                        "AND attribute.attnum = keys.attnum "
                        "WHERE namespace.nspname = 'chat' "
                        "AND table_relation.relname = 'conversations' "
                        "AND index_relation.relname = 'ix_conversations_owner_activity' "
                        "AND keys.position <= i.indnkeyatts "
                        "ORDER BY keys.position"
                    )
                )
            )
            .mappings()
            .all()
        )

    actual_uniques = {
        (row["table_name"], row["constraint_name"], tuple(row["columns"])) for row in unique_rows
    }
    assert actual_uniques == EXPECTED_UNIQUES
    assert [(row["column_name"], row["is_descending"]) for row in index_rows] == [
        ("owner_issuer", False),
        ("owner_subject", False),
        ("last_activity_at", True),
        ("id", True),
    ]
    assert len(index_rows) == 4
    assert {row["amname"] for row in index_rows} == {"btree"}
    assert {row["indisunique"] for row in index_rows} == {False}
    assert {row["indisvalid"] for row in index_rows} == {True}
    assert {row["indisready"] for row in index_rows} == {True}
    assert {row["indnkeyatts"] for row in index_rows} == {4}
    assert {row["indnatts"] for row in index_rows} == {4}


async def test_real_alembic_downgrade_and_upgrade_round_trip_restores_head(
    async_engine: AsyncEngine,
    migrated_database_url: str,
) -> None:
    """Exercise the real history and always restore head for later fixture cleanup."""

    try:
        await asyncio.to_thread(
            _run_alembic,
            migrated_database_url,
            "base",
            downgrade=True,
        )
        async with async_engine.connect() as connection:
            downgraded_tables = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'chat' AND table_type = 'BASE TABLE'"
                        )
                    )
                ).all()
            )
            downgraded_revision = await connection.scalar(
                text("SELECT version_num FROM chat.alembic_version")
            )
        assert downgraded_tables == {"alembic_version"}
        assert downgraded_revision is None

        await asyncio.to_thread(
            _run_alembic,
            migrated_database_url,
            "head",
            downgrade=False,
        )
        async with async_engine.connect() as connection:
            restored_tables = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = 'chat' AND table_type = 'BASE TABLE'"
                        )
                    )
                ).all()
            )
            restored_revision = await connection.scalar(
                text("SELECT version_num FROM chat.alembic_version")
            )
        assert restored_tables == EXPECTED_BUSINESS_TABLES | {"alembic_version"}
        assert restored_revision == "20260818_0001"
    finally:
        # A failed assertion after downgrade must not leave the shared ephemeral DB unusable.
        await asyncio.to_thread(
            _run_alembic,
            migrated_database_url,
            "head",
            downgrade=False,
        )


def test_orm_models_remain_separate_from_domain_entities() -> None:
    domain_types = (Conversation, ChatTurn, Message, Citation, PurgePreview, PurgeAudit)
    model_types = (
        ConversationModel,
        TurnModel,
        MessageModel,
        CitationModel,
        PurgePreviewModel,
        PurgeAuditModel,
    )

    assert Base.metadata.schema == SCHEMA == "chat"
    assert {table.schema for table in Base.metadata.tables.values()} == {"chat"}
    assert all(inspect(domain_type, raiseerr=False) is None for domain_type in domain_types)
    assert all(inspect(model_type, raiseerr=False) is not None for model_type in model_types)
    assert all(
        not issubclass(model_type, domain_type)
        for model_type, domain_type in zip(model_types, domain_types, strict=True)
    )
