"""建立 Ingestion 使用的 SQLAlchemy async engine/session factory。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ingestion_service.infrastructure.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """依設定建立 SQLAlchemy async engine 與 connection pool。"""

    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """建立 session factory；每個 UnitOfWork 會從它取得獨立 AsyncSession。"""

    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
