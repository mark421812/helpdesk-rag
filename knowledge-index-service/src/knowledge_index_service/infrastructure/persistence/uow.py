"""SQLAlchemy Unit of Work adapter。

Unit of Work 把多個 repository 操作包在同一個 database transaction；成功時由 use case
明確 commit，發生例外則 rollback，避免只寫入一半的文件／job／outbox。
"""

from __future__ import annotations

from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledge_index_service.application.ports import (
    DocumentRepository,
    JobRepository,
    OutboxRepository,
    UnitOfWork,
)
from knowledge_index_service.infrastructure.persistence.repositories import (
    SqlAlchemyDocumentRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyOutboxRepository,
)


class SqlAlchemyUnitOfWork:
    """一個 async SQLAlchemy session 所代表的 transaction boundary。"""

    documents: DocumentRepository
    jobs: JobRepository
    outbox: OutboxRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        """進入 ``async with`` 時建立 session 與 repositories。"""

        self._session = self._session_factory()
        self.documents = SqlAlchemyDocumentRepository(self._session)
        self.jobs = SqlAlchemyJobRepository(self._session)
        self.outbox = SqlAlchemyOutboxRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """離開 context 時 rollback 未提交變更並關閉 session。"""

        if exc is not None:
            await self.rollback()
        await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class SqlAlchemyUnitOfWorkFactory:
    """保存 session factory，讓每個 use case 建立新的 UnitOfWork。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> UnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
