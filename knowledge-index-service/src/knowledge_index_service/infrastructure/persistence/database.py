"""建立 SQLAlchemy async engine 與 session factory。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from knowledge_index_service.infrastructure.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """建立 async connection pool；pool_pre_ping 會在借出連線前測試有效性。"""

    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """回傳可重複呼叫的 session factory，而不是共用單一 session。"""

    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
