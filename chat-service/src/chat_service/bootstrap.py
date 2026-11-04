"""Chat Service composition root。

``bootstrap`` 是唯一知道 concrete adapter 的地方：它把 PostgreSQL UoW、development
identity、Retrieval/Answer HTTP gateway 與 bounded capacity 組成 application use cases。FastAPI
只取得這個 Runtime，不在 route 內建立 SQLAlchemy session、HTTP client 或 provider object。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import perf_counter

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from chat_service.application.dtos import (
    AnswerGenerationRequest,
    AnswerGenerationResult,
    RetrievalGatewayResult,
    RetrievalRequest,
)
from chat_service.application.ports import (
    AnswerGenerationGatewayPort,
    IdentityProviderPort,
    RetrievalGatewayPort,
)
from chat_service.application.use_cases import (
    CreateConversation,
    ExecuteConversationPurge,
    GetConversation,
    GetTurn,
    ListConversations,
    ListMessages,
    PreviewConversationPurge,
    SubmitTurn,
    UpdateConversation,
)
from chat_service.infrastructure.answer_generation.http_client import AnswerGenerationHttpClient
from chat_service.infrastructure.capacity import InMemoryCapacityGate
from chat_service.infrastructure.config import Settings
from chat_service.infrastructure.identity.development import (
    DevelopmentHeaderIdentityProvider,
    FailClosedIdentityProvider,
)
from chat_service.infrastructure.observability import Metrics
from chat_service.infrastructure.persistence.conversation_repository import (
    SqlAlchemyConversationRepository,
)
from chat_service.infrastructure.persistence.database import create_engine, create_session_factory
from chat_service.infrastructure.persistence.purge_repository import SqlAlchemyPurgeRepository
from chat_service.infrastructure.persistence.repositories import (
    SqlAlchemyTurnRepository,
)
from chat_service.infrastructure.persistence.uow import SqlAlchemyUnitOfWorkFactory
from chat_service.infrastructure.retrieval.http_client import RetrievalHttpClient


@dataclass(slots=True)
class Runtime:
    """FastAPI lifespan 期間共用的強型別依賴容器。"""

    settings: Settings
    engine: AsyncEngine
    uow_factory: SqlAlchemyUnitOfWorkFactory
    identity_provider: IdentityProviderPort
    retrieval_gateway: RetrievalGatewayPort
    answer_gateway: AnswerGenerationGatewayPort
    capacity_gate: InMemoryCapacityGate
    create_conversation: CreateConversation
    list_conversations: ListConversations
    get_conversation: GetConversation
    update_conversation: UpdateConversation
    submit_turn: SubmitTurn
    get_turn: GetTurn
    list_messages: ListMessages
    preview_conversation_purge: PreviewConversationPurge
    execute_conversation_purge: ExecuteConversationPurge
    http_client: httpx.AsyncClient
    _started: bool = False
    _closed: bool = False

    async def startup(self) -> int:
        """啟動前執行一次 stale-turn recovery；任何 persistence failure 都阻止 ready。"""

        if self._closed:
            raise RuntimeError("runtime is already closed")
        if self._started:
            return 0
        async with self.uow_factory() as uow:
            recovered = await uow.turns.recover_stale(self.settings.processing_stale_seconds)
            await uow.commit()
        self._started = True
        return recovered

    async def readiness_checks(self) -> dict[str, bool]:
        """檢查 DB、Retrieval、Answer 與 identity；不呼叫 generation。"""

        database_ready = await self._database_ready()
        retrieval_ready, answer_ready = await asyncio.gather(
            self.retrieval_gateway.is_ready(),
            self.answer_gateway.is_ready(),
        )
        return {
            "process": True,
            "postgres": database_ready,
            "retrieval": retrieval_ready,
            "answer_generation": answer_ready,
            "identity_provider": not isinstance(self.identity_provider, FailClosedIdentityProvider),
        }

    async def close(self) -> None:
        """依序釋放 HTTP pool 與 SQLAlchemy engine；重複 close 是安全 no-op。"""

        if self._closed:
            return
        self._closed = True
        try:
            for gateway in (self.retrieval_gateway, self.answer_gateway):
                close = getattr(gateway, "close", None)
                if close is not None:
                    await close()
        finally:
            await self.http_client.aclose()
            await self.engine.dispose()

    async def _database_ready(self) -> bool:
        """以短且無副作用的 SELECT 1 檢查 PostgreSQL。"""

        try:
            timeout_seconds = min(
                self.settings.retrieval_readiness_timeout_seconds,
                self.settings.answer_readiness_timeout_seconds,
            )
            async with asyncio.timeout(timeout_seconds):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


class _MeasuredRetrievalGateway:
    """在 composition root 包裝 Retrieval，讓 latency metric 不污染 Application layer。"""

    def __init__(self, delegate: RetrievalGatewayPort, metrics: Metrics) -> None:
        self._delegate = delegate
        self._metrics = metrics

    async def retrieve(self, request: RetrievalRequest) -> RetrievalGatewayResult:
        started = perf_counter()
        try:
            return await self._delegate.retrieve(request)
        finally:
            self._metrics.observe_dependency("retrieval", started)

    async def is_ready(self) -> bool:
        return await self._delegate.is_ready()

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if close is not None:
            await close()


class _MeasuredAnswerGateway:
    """在 composition root 包裝 Answer，記錄整個 non-retrying POST latency。"""

    def __init__(self, delegate: AnswerGenerationGatewayPort, metrics: Metrics) -> None:
        self._delegate = delegate
        self._metrics = metrics

    async def generate(self, request: AnswerGenerationRequest) -> AnswerGenerationResult:
        started = perf_counter()
        try:
            return await self._delegate.generate(request)
        finally:
            self._metrics.observe_dependency("answer", started)

    async def is_ready(self) -> bool:
        return await self._delegate.is_ready()

    async def close(self) -> None:
        close = getattr(self._delegate, "close", None)
        if close is not None:
            await close()


def build_runtime(settings: Settings, *, metrics: Metrics | None = None) -> Runtime:
    """建立 production-shaped dependency graph；尚未實作的 Entra mode 直接 fail closed。"""

    if settings.app_env == "production" or settings.identity_mode != "development_headers":
        raise RuntimeError(
            "The Entra JWT identity adapter is not implemented; Chat Service refuses to start"
        )

    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    http_client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    retrieval_gateway: RetrievalGatewayPort = RetrievalHttpClient(
        settings.retrieval_url,
        api_key=settings.retrieval_api_key,
        retrieval_limit=settings.chat_retrieval_limit,
        development_identity_headers=True,
        connect_timeout_seconds=settings.retrieval_connect_timeout_seconds,
        read_timeout_seconds=settings.retrieval_read_timeout_seconds,
        readiness_timeout_seconds=settings.retrieval_readiness_timeout_seconds,
        total_timeout_seconds=settings.retrieval_total_timeout_seconds,
        max_response_body_bytes=settings.retrieval_max_response_body_bytes,
        client=http_client,
    )
    answer_gateway: AnswerGenerationGatewayPort = AnswerGenerationHttpClient(
        settings.answer_generation_url,
        api_key=settings.answer_generation_api_key,
        connect_timeout_seconds=settings.answer_connect_timeout_seconds,
        read_timeout_seconds=settings.answer_read_timeout_seconds,
        total_timeout_seconds=settings.answer_total_timeout_seconds,
        readiness_timeout_seconds=settings.answer_readiness_timeout_seconds,
        max_response_body_bytes=settings.answer_max_response_body_bytes,
        client=http_client,
    )
    if metrics is not None:
        retrieval_gateway = _MeasuredRetrievalGateway(retrieval_gateway, metrics)
        answer_gateway = _MeasuredAnswerGateway(answer_gateway, metrics)

    uow_factory = SqlAlchemyUnitOfWorkFactory(
        session_factory,
        conversation_repository_factory=SqlAlchemyConversationRepository,
        turn_repository_factory=SqlAlchemyTurnRepository,
        purge_repository_factory=lambda session: SqlAlchemyPurgeRepository(
            session,
            preview_ttl_seconds=settings.purge_preview_ttl_seconds,
        ),
    )
    identity_provider: IdentityProviderPort = DevelopmentHeaderIdentityProvider()
    capacity_gate = InMemoryCapacityGate(
        max_active_turns=settings.max_active_turns,
        max_queue_size=settings.max_turn_queue_size,
    )
    create_conversation = CreateConversation(uow_factory)
    list_conversations = ListConversations(uow_factory)
    get_conversation = GetConversation(uow_factory)
    update_conversation = UpdateConversation(uow_factory)
    submit_turn = SubmitTurn(
        uow_factory,
        retrieval_gateway,
        answer_gateway,
        capacity_gate,
        queue_wait_timeout_seconds=settings.turn_queue_wait_timeout_seconds,
        retrieval_limit=settings.chat_retrieval_limit,
    )
    get_turn = GetTurn(uow_factory)
    list_messages = ListMessages(uow_factory)
    preview_conversation_purge = PreviewConversationPurge(
        uow_factory,
        admin_role=settings.admin_role,
    )
    execute_conversation_purge = ExecuteConversationPurge(
        uow_factory,
        admin_role=settings.admin_role,
    )
    return Runtime(
        settings=settings,
        engine=engine,
        uow_factory=uow_factory,
        identity_provider=identity_provider,
        retrieval_gateway=retrieval_gateway,
        answer_gateway=answer_gateway,
        capacity_gate=capacity_gate,
        create_conversation=create_conversation,
        list_conversations=list_conversations,
        get_conversation=get_conversation,
        update_conversation=update_conversation,
        submit_turn=submit_turn,
        get_turn=get_turn,
        list_messages=list_messages,
        preview_conversation_purge=preview_conversation_purge,
        execute_conversation_purge=execute_conversation_purge,
        http_client=http_client,
    )
