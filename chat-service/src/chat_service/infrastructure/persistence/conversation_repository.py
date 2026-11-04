"""Conversation aggregate 的 SQLAlchemy persistence adapter。

所有單筆存取都把 owner issuer、subject 與 conversation ID 放在同一個 SQL predicate，
避免先查資源再做 Python ownership 判斷而洩漏存在性。會配置 ordinal 或改變 aggregate 的
操作先取得 PostgreSQL row lock，timestamp 則只取 database time；transaction 的 commit/
rollback 仍由 Unit of Work 決定。
"""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.application.dtos import (
    UNSET,
    ConversationCursorPayload,
    ConversationSummary,
    CreateConversationCommand,
    GetConversationQuery,
    ListConversationsQuery,
    ListConversationsResult,
    MessageOrdinalAllocation,
    TurnAndUserMessageOrdinalAllocation,
    UpdateConversationCommand,
)
from chat_service.domain.entities import Conversation, Principal
from chat_service.domain.enums import ConversationStatus
from chat_service.domain.exceptions import ArchivedConversationError, OwnershipError
from chat_service.infrastructure.persistence.models import ConversationModel


class SqlAlchemyConversationRepository:
    """以呼叫端提供的 ``AsyncSession`` 實作 owner-scoped conversation port。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        command: CreateConversationCommand,
        owner: Principal,
    ) -> Conversation:
        """建立 conversation，UUID 由 adapter 配置而 timestamps 使用 server defaults。"""

        model = ConversationModel(
            id=uuid4(),
            owner_issuer=owner.issuer,
            owner_subject=owner.subject,
            tenant_id=owner.tenant_id,
            title=command.title,
            status=ConversationStatus.ACTIVE.value,
        )
        self._session.add(model)
        await self._session.flush()
        # PostgreSQL INSERT RETURNING 通常已載入 server defaults；明確 refresh 讓 mapping
        # 不依賴 dialect 的 implicit-returning 細節，且仍不跨越 UoW transaction boundary。
        await self._session.refresh(model)
        return self._to_domain(model)

    async def get_for_owner(self, query: GetConversationQuery) -> Conversation | None:
        """以 issuer、subject 與 ID 一次過濾，找不到與非 owner 都回 ``None``。"""

        model = await self._get_model_for_owner(query, for_update=False)
        return None if model is None else self._to_domain(model)

    async def get_for_owner_for_update(
        self,
        query: GetConversationQuery,
    ) -> Conversation | None:
        """執行 owner-filtered ``SELECT FOR UPDATE``，row lock 持續到 UoW 結束。"""

        model = await self._get_model_for_owner(query, for_update=True)
        return None if model is None else self._to_domain(model)

    async def list_for_owner(
        self,
        query: ListConversationsQuery,
    ) -> ListConversationsResult:
        """以 ``(last_activity_at, id)`` DESC keyset 讀取一頁安全 summary。"""

        statement = select(ConversationModel).where(
            ConversationModel.owner_issuer == query.principal.issuer,
            ConversationModel.owner_subject == query.principal.subject,
        )
        if query.cursor is not None:
            statement = statement.where(
                or_(
                    ConversationModel.last_activity_at < query.cursor.last_activity_at,
                    and_(
                        ConversationModel.last_activity_at == query.cursor.last_activity_at,
                        ConversationModel.id < query.cursor.conversation_id,
                    ),
                )
            )
        statement = statement.order_by(
            ConversationModel.last_activity_at.desc(),
            ConversationModel.id.desc(),
        ).limit(query.limit + 1)

        models = list((await self._session.scalars(statement)).all())
        has_next_page = len(models) > query.limit
        page = models[: query.limit]
        items = tuple(self._to_summary(model) for model in page)

        next_cursor: ConversationCursorPayload | None = None
        if has_next_page:
            last = page[-1]
            next_cursor = ConversationCursorPayload(
                version=1,
                last_activity_at=last.last_activity_at,
                conversation_id=last.id,
            )
        return ListConversationsResult(items=items, next_cursor=next_cursor)

    async def update(self, command: UpdateConversationCommand) -> Conversation:
        """在 owner row lock 內套用 PATCH，整筆 PATCH 的 row version 只增加一次。"""

        query = GetConversationQuery(
            principal=command.principal,
            conversation_id=command.conversation_id,
        )
        model = await self._get_model_for_owner(query, for_update=True)
        if model is None:
            raise OwnershipError("conversation is unavailable to this principal")

        if command.title is not UNSET:
            model.title = cast(str | None, command.title)
        if command.status is not None:
            model.status = command.status.value

        model.row_version += 1
        model.updated_at = await self._database_now()
        # PATCH 不代表新訊息或 terminal result，刻意不碰 last_activity_at。
        await self._session.flush()
        return self._to_domain(model)

    async def allocate_turn_and_user_message_ordinals(
        self,
        query: GetConversationQuery,
    ) -> TurnAndUserMessageOrdinalAllocation | None:
        """鎖住 parent 後同時配置 turn/user-message ordinal，並只增加一次版本。"""

        model = await self._get_model_for_owner(query, for_update=True)
        if model is None:
            return None
        if model.status == ConversationStatus.ARCHIVED.value:
            raise ArchivedConversationError("archived conversations do not accept new turns")

        turn_ordinal = model.next_turn_ordinal
        user_message_ordinal = model.next_message_ordinal
        model.next_turn_ordinal += 1
        model.next_message_ordinal += 1
        model.row_version += 1
        database_now = await self._database_now()
        model.updated_at = database_now
        model.last_activity_at = database_now
        await self._session.flush()

        return TurnAndUserMessageOrdinalAllocation(
            turn_ordinal=turn_ordinal,
            user_message_ordinal=user_message_ordinal,
            row_version=model.row_version,
        )

    async def allocate_message_ordinal(
        self,
        query: GetConversationQuery,
    ) -> MessageOrdinalAllocation | None:
        """鎖住 parent 後配置 assistant message ordinal，並更新版本與 activity。"""

        model = await self._get_model_for_owner(query, for_update=True)
        if model is None:
            return None

        message_ordinal = model.next_message_ordinal
        model.next_message_ordinal += 1
        model.row_version += 1
        database_now = await self._database_now()
        model.updated_at = database_now
        model.last_activity_at = database_now
        await self._session.flush()

        return MessageOrdinalAllocation(
            message_ordinal=message_ordinal,
            row_version=model.row_version,
        )

    async def record_terminal_activity(self, query: GetConversationQuery) -> int | None:
        """鎖住 parent，為沒有 assistant message 的 terminal transaction 記錄一次變更。"""

        model = await self._get_model_for_owner(query, for_update=True)
        if model is None:
            return None

        model.row_version += 1
        database_now = await self._database_now()
        model.updated_at = database_now
        model.last_activity_at = database_now
        await self._session.flush()
        return model.row_version

    async def _get_model_for_owner(
        self,
        query: GetConversationQuery,
        *,
        for_update: bool,
    ) -> ConversationModel | None:
        """集中 owner predicate，確保一般讀取與 lock 路徑使用完全相同的隔離條件。"""

        statement = select(ConversationModel).where(
            ConversationModel.id == query.conversation_id,
            ConversationModel.owner_issuer == query.principal.issuer,
            ConversationModel.owner_subject == query.principal.subject,
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self._session.execute(statement)
        return result.scalar_one_or_none()

    async def _database_now(self) -> datetime:
        """只向 PostgreSQL 取得 transaction-aware database time，不讀 application clock。"""

        value = await self._session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("database did not return its current time")
        return cast(datetime, value)

    @staticmethod
    def _to_domain(model: ConversationModel) -> Conversation:
        """把 ORM row 複製為 framework-free aggregate，不讓 session state 逸出 adapter。"""

        return Conversation(
            id=model.id,
            owner=Principal(
                issuer=model.owner_issuer,
                subject=model.owner_subject,
                tenant_id=model.tenant_id,
            ),
            title=model.title,
            status=ConversationStatus(model.status),
            next_turn_ordinal=model.next_turn_ordinal,
            next_message_ordinal=model.next_message_ordinal,
            row_version=model.row_version,
            created_at=model.created_at,
            updated_at=model.updated_at,
            last_activity_at=model.last_activity_at,
        )

    @staticmethod
    def _to_summary(model: ConversationModel) -> ConversationSummary:
        """建立 public-safe list item；刻意不映射任何 owner 欄位。"""

        return ConversationSummary(
            conversation_id=model.id,
            title=model.title,
            status=ConversationStatus(model.status),
            created_at=model.created_at,
            updated_at=model.updated_at,
            last_activity_at=model.last_activity_at,
        )
