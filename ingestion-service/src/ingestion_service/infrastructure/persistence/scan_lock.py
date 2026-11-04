"""以 PostgreSQL advisory lock 序列化同一 source 的 scan/單檔匯入。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ingestion_service.domain.exceptions import ConflictError


class PostgresSourceScanLock:
    """用 transaction-scoped advisory lock 序列化同一 source 的掃描。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def acquire(self, source_id: UUID) -> AsyncIterator[None]:
        key = _signed_bigint(source_id)
        async with self._engine.connect() as connection:
            async with connection.begin():
                acquired = await connection.scalar(
                    text("SELECT pg_try_advisory_xact_lock(:key)"),
                    {"key": key},
                )
                if acquired is not True:
                    raise ConflictError(f"Source {source_id} already has a scan in progress")
                yield


def _signed_bigint(source_id: UUID) -> int:
    """將 UUID 低 64 bits 轉成 PostgreSQL lock 接受的 signed bigint。"""

    unsigned = source_id.int & ((1 << 64) - 1)
    return unsigned if unsigned < (1 << 63) else unsigned - (1 << 64)
