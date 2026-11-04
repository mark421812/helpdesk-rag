from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from ingestion_service.domain.entities import IngestionJob, Source, SourceItem
from ingestion_service.domain.enums import IngestionJobType, SourceItemStatus
from ingestion_service.domain.exceptions import ConflictError
from ingestion_service.infrastructure.persistence.database import create_session_factory
from ingestion_service.infrastructure.persistence.scan_lock import PostgresSourceScanLock
from ingestion_service.infrastructure.persistence.uow import SqlAlchemyUnitOfWorkFactory

pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _async_url(container: PostgresContainer) -> str:
    return container.get_connection_url().replace(
        "postgresql+psycopg2://",
        "postgresql+asyncpg://",
    )


@pytest.mark.asyncio
async def test_round_trip_and_only_one_active_job_per_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        pytest.skip(f"Docker PostgreSQL is unavailable: {exc}")

    database_url = _async_url(container)
    monkeypatch.setenv("DATABASE_URL", database_url)
    alembic_config = Config(PROJECT_ROOT / "alembic.ini")
    alembic_config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    engine = None

    try:
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        engine = create_async_engine(database_url, pool_pre_ping=True)
        async with engine.connect() as connection:
            tables = (
                await connection.execute(
                    text(
                        "SELECT to_regclass('ingestion.sources'), "
                        "to_regclass('ingestion.sync_runs'), "
                        "to_regclass('ingestion.source_items'), "
                        "to_regclass('ingestion.jobs')"
                    )
                )
            ).one()
            revision = await connection.scalar(
                text("SELECT version_num FROM ingestion.alembic_version")
            )
        assert tuple(tables) == (
            "ingestion.sources",
            "ingestion.sync_runs",
            "ingestion.source_items",
            "ingestion.jobs",
        )
        assert revision == "20260802_0001"

        uow_factory = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
        scan_lock = PostgresSourceScanLock(engine)
        now = datetime.now(UTC)
        source = Source(id=uuid4(), name="integration", root_path="/data/documents")
        item = SourceItem(
            id=uuid4(),
            source_id=source.id,
            source_item_id="policy.md",
            source_path="policy.md",
            absolute_path="/data/documents/policy.md",
            source_version="mtime:1;size:10",
            metadata_source_hash="a" * 64,
            mime_type="text/markdown",
            file_size=10,
            modified_at=now,
            status=SourceItemStatus.PENDING,
            restore_requested=True,
            created_at=now,
            updated_at=now,
        )
        jobs = tuple(
            IngestionJob(
                id=uuid4(),
                source_id=source.id,
                item_id=item.id,
                job_type=IngestionJobType.PROCESS_ITEM,
                idempotency_key=f"process:{item.id}:{index}",
                available_at=now,
                created_at=now + timedelta(microseconds=index),
                updated_at=now,
            )
            for index in range(2)
        )
        async with uow_factory() as uow:
            await uow.sources.add(source)
            await uow.items.add(item)
            for job in jobs:
                await uow.jobs.add(job)
            await uow.commit()

        async with uow_factory() as uow:
            first = await uow.jobs.claim_next(
                worker_id="worker-1",
                now=now,
                lease_for=timedelta(minutes=5),
            )
            await uow.commit()
        assert first is not None

        async with uow_factory() as uow:
            second = await uow.jobs.claim_next(
                worker_id="worker-2",
                now=now,
                lease_for=timedelta(minutes=5),
            )
            loaded = await uow.items.get(item.id)
        assert second is None
        assert loaded is not None
        assert loaded.modified_at == item.modified_at
        assert loaded.restore_requested is True

        async with scan_lock.acquire(source.id):
            with pytest.raises(ConflictError, match="scan in progress"):
                async with scan_lock.acquire(source.id):
                    pytest.fail("The same source lock must not be acquired twice")
    finally:
        if engine is not None:
            await engine.dispose()
        container.stop()
