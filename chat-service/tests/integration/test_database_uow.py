"""Integration tests for database construction and Unit of Work transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from chat_service.application.ports import (
    ConversationRepositoryPort,
    PurgeRepositoryPort,
    TurnRepositoryPort,
)
from chat_service.infrastructure.config import Settings
from chat_service.infrastructure.persistence.database import (
    create_engine,
    create_session_factory,
)
from chat_service.infrastructure.persistence.models import ConversationModel
from chat_service.infrastructure.persistence.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _RepositoryProbe:
    session: AsyncSession


class _RepositoryFactories:
    def __init__(self) -> None:
        self.repositories: list[_RepositoryProbe] = []

    def conversations(self, session: AsyncSession) -> ConversationRepositoryPort:
        repository = _RepositoryProbe(session)
        self.repositories.append(repository)
        return cast(ConversationRepositoryPort, repository)

    def turns(self, session: AsyncSession) -> TurnRepositoryPort:
        repository = _RepositoryProbe(session)
        self.repositories.append(repository)
        return cast(TurnRepositoryPort, repository)

    def purges(self, session: AsyncSession) -> PurgeRepositoryPort:
        repository = _RepositoryProbe(session)
        self.repositories.append(repository)
        return cast(PurgeRepositoryPort, repository)


def _unit_of_work(
    session_factory: async_sessionmaker[AsyncSession],
    factories: _RepositoryFactories,
) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(
        session_factory,
        conversation_repository_factory=factories.conversations,
        turn_repository_factory=factories.turns,
        purge_repository_factory=factories.purges,
    )


async def _insert_conversation(session: AsyncSession, conversation_id: UUID) -> None:
    session.add(
        ConversationModel(
            id=conversation_id,
            owner_issuer="urn:integration-test",
            owner_subject="uow-owner",
            title="transaction probe",
            status="active",
        )
    )
    await session.flush()


async def _conversation_count(
    session_factory: async_sessionmaker[AsyncSession], conversation_id: UUID
) -> int:
    async with session_factory() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ConversationModel)
            .where(ConversationModel.id == conversation_id)
        )
    assert count is not None
    return count


async def test_database_url_errors_are_redacted_and_factory_is_async() -> None:
    secret = "integration-super-secret"
    malformed_url = f"postgresql+asyncpg://chat:{secret}@[invalid"
    with pytest.raises(ValueError) as captured:
        create_engine(Settings(app_env="test", database_url=malformed_url))
    assert secret not in str(captured.value)
    assert malformed_url not in str(captured.value)

    engine = create_engine(
        Settings(
            app_env="test",
            database_url=(f"postgresql+asyncpg://chat:{secret}@127.0.0.1:5432/integration"),
            database_pool_size=1,
            database_max_overflow=0,
        )
    )
    session: AsyncSession | None = None
    try:
        assert isinstance(engine, AsyncEngine)
        assert engine.dialect.driver == "asyncpg"
        assert engine.sync_engine.hide_parameters is True
        assert secret not in str(engine.url)
        assert secret not in repr(engine.url)

        factory = create_session_factory(engine)
        session = factory()
        assert isinstance(session, AsyncSession)
        assert session.autoflush is False
        assert session.sync_session.expire_on_commit is False
    finally:
        if session is not None:
            await session.close()
        await engine.dispose()


async def test_shared_session_repositories_and_explicit_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    factories = _RepositoryFactories()
    uow = _unit_of_work(session_factory, factories)

    async with uow as active:
        assert len(factories.repositories) == 3
        assert len({id(repository.session) for repository in factories.repositories}) == 1
        assert id(active.conversations) == id(factories.repositories[0])
        assert id(active.turns) == id(factories.repositories[1])
        assert id(active.purges) == id(factories.repositories[2])

        await _insert_conversation(factories.repositories[0].session, conversation_id)
        await active.commit()

    assert await _conversation_count(session_factory, conversation_id) == 1


async def test_uow_does_not_auto_commit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    factories = _RepositoryFactories()

    async with _unit_of_work(session_factory, factories):
        await _insert_conversation(factories.repositories[0].session, conversation_id)

    assert await _conversation_count(session_factory, conversation_id) == 0


async def test_uow_rolls_back_and_propagates_exceptions(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    conversation_id = uuid4()
    factories = _RepositoryFactories()

    with pytest.raises(RuntimeError, match="transaction failed"):
        async with _unit_of_work(session_factory, factories):
            await _insert_conversation(factories.repositories[0].session, conversation_id)
            raise RuntimeError("transaction failed")

    assert await _conversation_count(session_factory, conversation_id) == 0


async def test_migrated_session_factory_executes_asyncpg_queries(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await session.scalar(text("SELECT 1")) == 1
