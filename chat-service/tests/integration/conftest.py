"""PostgreSQL 16 integration fixtures shared by persistence adapter tests."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from chat_service.infrastructure.config import Settings
from chat_service.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUSINESS_TABLES = (
    "citations",
    "messages",
    "turns",
    "purge_audit",
    "purge_previews",
    "conversations",
)


def _asyncpg_url(postgres: PostgresContainer) -> str:
    """Convert Testcontainers' synchronous URL into the production asyncpg form."""

    return (
        make_url(postgres.get_connection_url())
        .set(drivername="postgresql+asyncpg")
        .render_as_string(hide_password=False)
    )


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """Start required ephemeral PostgreSQL; Docker failures deliberately fail the suite."""

    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """Expose the ephemeral database through the async driver required by production."""

    return _asyncpg_url(postgres_container)


@pytest.fixture(scope="session")
def migrated_database_url(database_url: str) -> str:
    """Apply the real Alembic history to the empty container, never ``create_all``."""

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    with patch.dict(os.environ, {"DATABASE_URL": database_url}):
        command.upgrade(alembic_config, "head")
    return database_url


@pytest_asyncio.fixture
async def async_engine(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Create a production-configured engine on each test's event loop."""

    engine = create_engine(
        Settings(
            app_env="test",
            database_url=migrated_database_url,
            database_pool_size=2,
            database_max_overflow=0,
        )
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(
    async_engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide the same session factory consumed by repositories and Unit of Work."""

    yield create_session_factory(async_engine)


@pytest_asyncio.fixture(autouse=True)
async def clean_database(async_engine: AsyncEngine) -> AsyncIterator[None]:
    """Isolate integration tests while preserving Alembic's version table."""

    table_list = ", ".join(f'chat."{table}"' for table in BUSINESS_TABLES)
    statement = text(f"TRUNCATE TABLE {table_list} CASCADE")
    async with async_engine.begin() as connection:
        await connection.execute(statement)
    try:
        yield
    finally:
        async with async_engine.begin() as connection:
            await connection.execute(statement)
