"""Chat Service 的 async Alembic 執行環境。

Alembic version table 固定放在 chat.alembic_version。避免與共用 PostgreSQL database
中的 Knowledge 或 Ingestion migration history 混用。Online migration 使用單一 outer
transaction 建立 schema 並套用 DDL。migration 結束才會 commit。
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from chat_service.infrastructure.persistence.models import SCHEMA, Base

config = context.config
database_url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
if not database_url:
    raise RuntimeError("DATABASE_URL must be configured for migrations")
# ConfigParser 以百分號作 interpolation。URL encoded password 的百分號必須先 escape。
config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configure_context(**kwargs: object) -> None:
    """集中 Alembic schema/type/default 比對選項。確保 online/offline 行為一致。"""

    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        version_table="alembic_version",
        version_table_schema=SCHEMA,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """不連線產生 SQL。實際 SQL 仍只會操作 chat schema。"""

    _configure_context(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    # Offline script 會先產生 version table。因此必須在 run_migrations 前輸出 schema DDL。
    context.execute(text('CREATE SCHEMA IF NOT EXISTS "chat"'))
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在 Alembic 建 version table 前先確保專用 schema 存在。"""

    connection.execute(text('CREATE SCHEMA IF NOT EXISTS "chat"'))
    _configure_context(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """以 asyncpg engine 執行 migration。並在結束後確實釋放 pool。"""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        # begin() 擁有 CREATE SCHEMA、version row 與 migration DDL 的共同 transaction。
        async with connectable.begin() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
