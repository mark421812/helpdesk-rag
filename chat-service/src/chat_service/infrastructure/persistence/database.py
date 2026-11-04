"""建立 Chat Service 使用的 SQLAlchemy async engine 與 session factory。

這個 module 只負責 Infrastructure 的連線資源。Application 與 Domain 不會看見
``AsyncSession``，而是透過 Unit of Work port 使用 persistence 能力。
"""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from chat_service.infrastructure.config import Settings


def _validated_database_url(value: str) -> URL:
    """解析並驗證 PostgreSQL async URL，且不把 credential 放進錯誤訊息。

    ``make_url`` 回傳的 :class:`URL` 會分開保存密碼，預設字串表示也會遮蔽它；因此
    engine 不必再處理一份可能被記錄的明文 URL。Chat Service 僅支援 asyncpg，拒絕
    同步 driver 可避免 async engine 在啟動後才以難懂錯誤失敗。
    """

    normalized = value.strip()
    if not normalized:
        raise ValueError("DATABASE_URL must not be blank")

    try:
        url = make_url(normalized)
    except (ArgumentError, ValueError):
        # 不串接原始例外，因為 SQLAlchemy 的 parse error 可能包含帶密碼的輸入字串。
        raise ValueError("DATABASE_URL must be a valid SQLAlchemy URL") from None

    if url.drivername != "postgresql+asyncpg":
        raise ValueError("DATABASE_URL must use the postgresql+asyncpg driver")
    if not url.host:
        raise ValueError("DATABASE_URL must contain a database host")
    if not url.database or not url.database.strip():
        raise ValueError("DATABASE_URL must contain a database name")
    return url


def create_engine(settings: Settings) -> AsyncEngine:
    """依強型別設定建立 async connection pool。

    ``pool_pre_ping`` 會在借出既有連線前檢查它是否仍可用；``hide_parameters`` 則避免
    SQL bind parameter 出現在 SQLAlchemy 的例外文字與 log 中。函式不連線，實際連線
    會延後到第一個 database operation。
    """

    return create_async_engine(
        _validated_database_url(settings.database_url),
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """建立 session factory，讓每個 Unit of Work 取得獨立 ``AsyncSession``。

    關閉 autoflush 可讓 repository 明確決定何時 flush；關閉 expire-on-commit 則讓 use
    case 在明確 commit 後仍能安全讀取已載入的 domain/DTO 所需欄位。
    """

    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
