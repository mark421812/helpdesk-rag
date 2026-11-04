"""Application 對外能力的 Protocol ports。

Protocol 是 Python 的 structural typing：use case 只依賴這些方法形狀，Infrastructure
之後可注入 PostgreSQL、HTTP 或 bounded-capacity adapter，unit test 則可注入不連外的 fake。
Port 不攜帶 FastAPI Request、SQLAlchemy session、URL、API key 或 provider secret。
"""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Protocol
from uuid import UUID

from chat_service.application.dtos import (
    AnswerGenerationRequest,
    AnswerGenerationResult,
    CapacityLease,
    CreateConversationCommand,
    ExecuteConversationPurgeCommand,
    GetConversationQuery,
    GetTurnQuery,
    GetTurnResult,
    IdentityInput,
    ListConversationsQuery,
    ListConversationsResult,
    ListMessagesQuery,
    ListMessagesResult,
    MessageOrdinalAllocation,
    PreviewConversationPurgeCommand,
    PurgePreviewResult,
    PurgeResult,
    RetrievalGatewayResult,
    RetrievalRequest,
    TurnAndUserMessageOrdinalAllocation,
    UpdateConversationCommand,
)
from chat_service.domain.entities import ChatTurn, Conversation, Message, Principal, PurgePreview


class IdentityProviderPort(Protocol):
    """把未驗證 identity input 解析成可信 Principal。"""

    async def resolve(self, identity: IdentityInput) -> Principal: ...


class ConversationRepositoryPort(Protocol):
    """Conversation owner-scoped persistence 的抽象介面。"""

    async def add(self, command: CreateConversationCommand, owner: Principal) -> Conversation: ...

    async def get_for_owner(self, query: GetConversationQuery) -> Conversation | None: ...

    async def get_for_owner_for_update(
        self,
        query: GetConversationQuery,
    ) -> Conversation | None:
        """以 issuer+subject+ID 過濾後取得 row lock，lock 持續到 UoW 結束。"""

        ...

    async def list_for_owner(self, query: ListConversationsQuery) -> ListConversationsResult: ...

    async def update(self, command: UpdateConversationCommand) -> Conversation: ...

    async def allocate_turn_and_user_message_ordinals(
        self,
        query: GetConversationQuery,
    ) -> TurnAndUserMessageOrdinalAllocation | None:
        """在 owner-filtered row lock 中增加兩個 counter，row_version 只增加一次。"""

        ...

    async def allocate_message_ordinal(
        self,
        query: GetConversationQuery,
    ) -> MessageOrdinalAllocation | None:
        """在 owner-filtered row lock 中分配 assistant message ordinal。"""

        ...

    async def record_terminal_activity(
        self,
        query: GetConversationQuery,
    ) -> int | None:
        """無 assistant message 的 terminal transaction 增加一次 version/activity。"""

        ...


class TurnRepositoryPort(Protocol):
    """Turn/message persistence 的 owner filter 與 idempotency 查詢介面。"""

    async def find_by_idempotency(
        self,
        principal: Principal,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> GetTurnResult | None:
        """回傳可重播的完整結果，包含 failed/failed_unknown safe error。"""

        ...

    async def get_for_owner(self, query: GetTurnQuery) -> GetTurnResult | None: ...

    async def get_for_owner_for_update(self, query: GetTurnQuery) -> GetTurnResult | None:
        """以 owner filter 取得 turn row lock，供 CAS/避免重複 generation 使用。"""

        ...

    async def add(
        self,
        turn: ChatTurn,
        user_message: Message,
        principal: Principal,
    ) -> None: ...

    async def add_assistant_message(self, message: Message, principal: Principal) -> None:
        """在呼叫端同一 UoW 中寫入 assistant message 與其 safe citations。"""

        ...

    async def update(self, turn: ChatTurn, principal: Principal) -> None:
        """Conversation-first owner lock 後以 DB time/CAS 保存 state。"""

        ...

    async def list_messages(self, query: ListMessagesQuery) -> ListMessagesResult: ...

    async def recover_stale(self, processing_stale_seconds: float) -> int:
        """
        使用 PostgreSQL database time 計算 cutoff 並原子復原 stale turns。

        Adapter 不得把 application/system clock 計算的 datetime 當 cutoff；這個 seconds
        只是 duration，`updated_at/completed_at` 與 comparison time 都來自 DB。
        """

        ...


class PurgeRepositoryPort(Protocol):
    """Preview-first purge 的 persistence primitives。"""

    async def create_preview(
        self,
        command: PreviewConversationPurgeCommand,
    ) -> PurgePreviewResult: ...

    async def get_preview_for_creator(
        self,
        preview_id: UUID,
        creator: Principal,
    ) -> PurgePreview | None:
        """Preview ID 與 creator issuer+subject 一起過濾，mismatch 不可被區分。"""

        ...

    async def execute(self, command: ExecuteConversationPurgeCommand) -> PurgeResult: ...


class UnitOfWorkPort(Protocol):
    """把 repositories 與 transaction boundary 綁在一起。"""

    conversations: ConversationRepositoryPort
    turns: TurnRepositoryPort
    purges: PurgeRepositoryPort

    async def __aenter__(self) -> UnitOfWorkPort: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class RetrievalGatewayPort(Protocol):
    """Retrieval HTTP adapter 的 application contract。"""

    async def retrieve(self, request: RetrievalRequest) -> RetrievalGatewayResult: ...

    async def is_ready(self) -> bool: ...


class AnswerGenerationGatewayPort(Protocol):
    """Answer HTTP adapter 的 application contract；retry policy 留在 adapter/use case。"""

    async def generate(self, request: AnswerGenerationRequest) -> AnswerGenerationResult: ...

    async def is_ready(self) -> bool: ...


class CapacityGatePort(Protocol):
    """單一 Chat process 的 bounded active/queue gate。"""

    async def acquire(self, wait_timeout_seconds: float) -> CapacityLease | None: ...

    async def release(self, lease: CapacityLease) -> None: ...


class ClockPort(Protocol):
    """提供可注入的 timezone-aware 時間；Domain 不直接呼叫 datetime.now。"""

    def now(self) -> datetime: ...
