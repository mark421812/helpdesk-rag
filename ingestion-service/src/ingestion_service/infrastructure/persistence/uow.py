"""Ingestion SQLAlchemy Unit of Work；統一多 repository 的 transaction boundary。"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ingestion_service.application.ports import (
    IngestionJobRepository,
    SourceItemRepository,
    SourceRepository,
    SyncRunRepository,
    UnitOfWork,
)
from ingestion_service.infrastructure.persistence.repositories import (
    SqlAlchemyIngestionJobRepository,
    SqlAlchemySourceItemRepository,
    SqlAlchemySourceRepository,
    SqlAlchemySyncRunRepository,
)


class SqlAlchemyUnitOfWork:
    """用一個 AsyncSession 實作 Application UnitOfWork port。"""

    sources: SourceRepository
    items: SourceItemRepository
    sync_runs: SyncRunRepository
    jobs: IngestionJobRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        # 進入 ``async with`` 時建立 session，並把同一 session 注入各 repository。
        self._session = self._session_factory()
        self.sources = SqlAlchemySourceRepository(self._session)
        self.items = SqlAlchemySourceItemRepository(self._session)
        self.sync_runs = SqlAlchemySyncRunRepository(self._session)
        self.jobs = SqlAlchemyIngestionJobRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # 發生例外便 rollback；成功路徑由 use case 明確呼叫 commit。
        if exc_value is not None:
            await self.rollback()
        await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    """保存 session factory，呼叫此物件時產生新的 UnitOfWork instance。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
