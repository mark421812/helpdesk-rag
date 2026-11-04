"""SQLAlchemy Unit of Work adapter。

Unit of Work 讓多個 repository 共用同一個 async session/transaction。Use case 必須明確
呼叫 ``commit``；context 結束時絕不替呼叫者猜測是否應提交，尚未提交的 transaction
一律 rollback。
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.application.ports import (
    ConversationRepositoryPort,
    PurgeRepositoryPort,
    TurnRepositoryPort,
    UnitOfWorkPort,
)

ConversationRepositoryFactory = Callable[[AsyncSession], ConversationRepositoryPort]
TurnRepositoryFactory = Callable[[AsyncSession], TurnRepositoryPort]
PurgeRepositoryFactory = Callable[[AsyncSession], PurgeRepositoryPort]


class SqlAlchemyUnitOfWork:
    """以單一 ``AsyncSession`` 實作 application 的 transaction boundary。

    Repository factory 由 composition root 注入，讓此 adapter 不必猜測 concrete repository
    的類別名稱。``AsyncSession`` 僅在 Infrastructure 內傳給 factories，不會暴露到
    Application 或 Domain。
    """

    conversations: ConversationRepositoryPort
    turns: TurnRepositoryPort
    purges: PurgeRepositoryPort

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        conversation_repository_factory: ConversationRepositoryFactory,
        turn_repository_factory: TurnRepositoryFactory,
        purge_repository_factory: PurgeRepositoryFactory,
    ) -> None:
        """保存 factories；真正的 session 只在進入 context 時建立。"""

        self._session_factory = session_factory
        self._conversation_repository_factory = conversation_repository_factory
        self._turn_repository_factory = turn_repository_factory
        self._purge_repository_factory = purge_repository_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> SqlAlchemyUnitOfWork:
        """建立獨立 session，並把同一 transaction 注入三個 repositories。"""

        if self._session is not None:
            raise RuntimeError("Unit of Work is already active")

        session = self._session_factory()
        try:
            conversations = self._conversation_repository_factory(session)
            turns = self._turn_repository_factory(session)
            purges = self._purge_repository_factory(session)
        except BaseException:
            # Factory 建構若失敗，也不能遺留尚未關閉的 connection-owning session。
            await session.close()
            raise

        self._session = session
        self.conversations = conversations
        self.turns = turns
        self.purges = purges
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """回滾任何未提交 transaction，然後確實關閉並釋放 session。

        即使 context 沒有例外也不會自動 commit；這能讓漏寫 ``commit`` 的程式安全地
        fail closed。傳入的 exception 會由 ``async with`` 原樣往外傳遞。
        """

        del exc_type, exc, traceback  # 介面需要這些參數；cleanup policy 與成功/失敗無關。
        session = self._require_session()
        try:
            if session.in_transaction():
                await session.rollback()
        finally:
            try:
                await session.close()
            finally:
                self._session = None
                del self.conversations
                del self.turns
                del self.purges

    async def commit(self) -> None:
        """明確提交目前 transaction；database error 直接交由 use case 處理。"""

        await self._require_session().commit()

    async def rollback(self) -> None:
        """明確回滾目前 transaction；可供 use case 在 context 尚未結束時使用。"""

        await self._require_session().rollback()

    def _require_session(self) -> AsyncSession:
        """取得 active session，避免在 context 外誤用 Unit of Work。"""

        if self._session is None:
            raise RuntimeError("Unit of Work is not active")
        return self._session


class SqlAlchemyUnitOfWorkFactory:
    """保存 session/repository factories，為每次 use case 建立新的 Unit of Work。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        conversation_repository_factory: ConversationRepositoryFactory,
        turn_repository_factory: TurnRepositoryFactory,
        purge_repository_factory: PurgeRepositoryFactory,
    ) -> None:
        self._session_factory = session_factory
        self._conversation_repository_factory = conversation_repository_factory
        self._turn_repository_factory = turn_repository_factory
        self._purge_repository_factory = purge_repository_factory

    def __call__(self) -> UnitOfWorkPort:
        """回傳尚未進入 context、沒有持有 session 的 Unit of Work。"""

        return SqlAlchemyUnitOfWork(
            self._session_factory,
            conversation_repository_factory=self._conversation_repository_factory,
            turn_repository_factory=self._turn_repository_factory,
            purge_repository_factory=self._purge_repository_factory,
        )
