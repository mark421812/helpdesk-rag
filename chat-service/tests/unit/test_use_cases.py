"""Phase 5 application orchestration tests。

這些 fake 只實作 application ports，不模擬 PostgreSQL SQL 語法；真正的 PostgreSQL
transaction、row lock、CAS 與 migration 仍由 integration suite 驗證。測試的重點是 use
case 的 transaction 邊界、冪等 replay、三路 Retrieval 分流與安全 terminal projection。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from chat_service.application.dtos import (
    UNSET,
    AnswerGenerationResult,
    CapacityLease,
    ConversationSummary,
    CreateConversationCommand,
    GetConversationQuery,
    GetTurnQuery,
    GetTurnResult,
    ListConversationsQuery,
    ListConversationsResult,
    ListMessagesQuery,
    ListMessagesResult,
    MessageOrdinalAllocation,
    RetrievalGatewayResult,
    SafeError,
    SubmitTurnCommand,
    TurnAndUserMessageOrdinalAllocation,
    TurnResult,
    UpdateConversationCommand,
)
from chat_service.application.ports import (
    AnswerGenerationGatewayPort,
    CapacityGatePort,
    RetrievalGatewayPort,
    UnitOfWorkPort,
)
from chat_service.application.use_cases import (
    CreateConversation,
    GetConversation,
    GetTurn,
    ListConversations,
    ListMessages,
    SubmitTurn,
    UpdateConversation,
    canonicalize_idempotency_key,
    request_fingerprint,
)
from chat_service.domain.entities import (
    MAX_RETRY_AFTER_SECONDS,
    AllowedEvidence,
    AnswerPart,
    ChatTurn,
    Citation,
    Conversation,
    GenerationDiagnostics,
    Governance,
    Message,
    Principal,
    RestrictedEvidence,
    TurnFailure,
)
from chat_service.domain.enums import (
    AnswerStatus,
    ChatOutcome,
    Confidentiality,
    ConversationStatus,
    InternalTurnState,
    RetrievalStatus,
)
from chat_service.domain.exceptions import DependencyError, IdempotencyConflictError, OwnershipError

UTC_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CONVERSATION_ID = UUID("10000000-0000-0000-0000-000000000001")
CHUNK_ID = UUID("20000000-0000-0000-0000-000000000001")
DOCUMENT_ID = UUID("30000000-0000-0000-0000-000000000001")
VERSION_ID = UUID("40000000-0000-0000-0000-000000000001")


def owner(subject: str = "employee-001") -> Principal:
    """建立 owner；測試刻意保留 issuer，驗證完整 identity filter 的輸入形狀。"""

    return Principal(
        issuer="urn:helpdesk:development",
        subject=subject,
        group_ids=("all-employees",),
        roles=("employee",),
    )


def make_conversation(principal: Principal | None = None) -> Conversation:
    """建立帶有 database-owned timestamp 的 fake aggregate。"""

    return Conversation(
        id=CONVERSATION_ID,
        owner=principal or owner(),
        created_at=UTC_NOW,
        updated_at=UTC_NOW,
        last_activity_at=UTC_NOW,
    )


def make_citation(citation_id: str = "R1") -> Citation:
    return Citation(
        citation_id=citation_id,
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        title="遠端工作規章",
        section_path="第三條",
        page_number=3,
    )


def make_allowed(rank: int = 1) -> AllowedEvidence:
    return AllowedEvidence(
        rank=rank,
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        score=0.95,
        content="員工每週可以遠端工作兩天。",
        document_type="policy",
        token_count=8,
        citation=make_citation("R1"),
        governance=Governance(
            confidentiality=Confidentiality.INTERNAL,
            language="zh-TW",
            external_llm_allowed=True,
        ),
    )


def make_restricted(rank: int = 1) -> RestrictedEvidence:
    return RestrictedEvidence(
        rank=rank,
        chunk_id=CHUNK_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        citation=make_citation("restricted-machine-id"),
    )


def retrieval_result(
    status: RetrievalStatus = RetrievalStatus.EVIDENCE_FOUND,
    *,
    mixed: bool = False,
) -> RetrievalGatewayResult:
    """產生三種合法 Retrieval 業務結果，供 fake gateway 重複使用。"""

    if status is RetrievalStatus.EVIDENCE_FOUND:
        allowed = (make_allowed(2),) if mixed else (make_allowed(1),)
        restricted = (make_restricted(1),) if mixed else ()
        return RetrievalGatewayResult(
            status=status,
            llm_context=allowed,
            restricted_evidence=restricted,
            external_generation_blocked=False,
            context_attestation="opaque-attestation-token",
            candidate_count=5,
            selected_count=len(allowed) + len(restricted),
            model_id="embedding-model",
            model_revision="revision-1",
        )
    if status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED:
        return RetrievalGatewayResult(
            status=status,
            llm_context=(),
            restricted_evidence=(make_restricted(1),),
            external_generation_blocked=True,
            context_attestation=None,
            candidate_count=2,
            selected_count=1,
            model_id="embedding-model",
            model_revision="revision-1",
        )
    return RetrievalGatewayResult(
        status=RetrievalStatus.INSUFFICIENT_EVIDENCE,
        llm_context=(),
        restricted_evidence=(),
        external_generation_blocked=False,
        context_attestation=None,
        candidate_count=0,
        selected_count=0,
        model_id="embedding-model",
        model_revision="revision-1",
    )


def answer_result() -> AnswerGenerationResult:
    citation = make_citation("C1")
    return AnswerGenerationResult(
        status=AnswerStatus.ANSWERED,
        answer_parts=(
            AnswerPart(text="每週可遠端工作兩天。", citation_ids=("C1",)),
            AnswerPart(text="請依主管流程申請。", citation_ids=("C1",)),
        ),
        citations=(replace(citation, citation_id="C1"),),
        diagnostics=GenerationDiagnostics(
            provider="fake-provider",
            model="fake-model",
            prompt_version="v1",
            finish_reason="stop",
            prompt_tokens=20,
            completion_tokens=10,
        ),
    )


class FakeStore:
    """跨 UoW 共用的 in-memory port state；不宣稱是 PostgreSQL 替代品。"""

    def __init__(self) -> None:
        self.conversations: dict[UUID, Conversation] = {}
        self.turns: dict[UUID, GetTurnResult] = {}
        self.active_uow_count = 0
        self.commit_count = 0
        self.locked_turn_ids: list[UUID] = []
        self.now_counter = 0

    def now(self) -> datetime:
        self.now_counter += 1
        return UTC_NOW.replace(microsecond=self.now_counter)

    def find_turn(self, conversation_id: UUID, key: str) -> GetTurnResult | None:
        for result in self.turns.values():
            turn = result.result.turn
            if turn.conversation_id == conversation_id and turn.idempotency_key == key:
                return result
        return None


class FakeConversationRepository:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def add(self, command: Any, principal: Principal) -> Conversation:
        conversation = Conversation(
            id=uuid4(),
            owner=principal,
            title=command.title,
            created_at=self.store.now(),
            updated_at=self.store.now(),
            last_activity_at=self.store.now(),
        )
        self.store.conversations[conversation.id] = conversation
        return conversation

    async def get_for_owner(self, query: GetConversationQuery) -> Conversation | None:
        conversation = self.store.conversations.get(query.conversation_id)
        if conversation is None or not conversation.owner.owns(
            query.principal.issuer,
            query.principal.subject,
        ):
            return None
        return conversation

    async def get_for_owner_for_update(self, query: GetConversationQuery) -> Conversation | None:
        return await self.get_for_owner(query)

    async def list_for_owner(self, query: ListConversationsQuery) -> ListConversationsResult:
        items = tuple(
            ConversationSummary(
                conversation_id=item.id,
                title=item.title,
                status=item.status,
                created_at=item.created_at or UTC_NOW,
                updated_at=item.updated_at or UTC_NOW,
                last_activity_at=item.last_activity_at or UTC_NOW,
            )
            for item in self.store.conversations.values()
            if item.owner.owns(query.principal.issuer, query.principal.subject)
        )
        return ListConversationsResult(items=items[: query.limit], next_cursor=None)

    async def update(self, command: UpdateConversationCommand) -> Conversation:
        conversation = await self.get_for_owner(
            GetConversationQuery(
                principal=command.principal,
                conversation_id=command.conversation_id,
            )
        )
        if conversation is None:
            raise RuntimeError("conversation missing in fake")
        if command.title is not UNSET:
            conversation.rename(command.title)
        if command.status is not None:
            conversation.set_status(command.status)
        conversation.updated_at = self.store.now()
        return conversation

    async def allocate_turn_and_user_message_ordinals(
        self,
        query: GetConversationQuery,
    ) -> TurnAndUserMessageOrdinalAllocation | None:
        conversation = await self.get_for_owner(query)
        if conversation is None:
            return None
        turn_ordinal, message_ordinal = conversation.allocate_turn_and_user_message_ordinals()
        now = self.store.now()
        conversation.updated_at = now
        conversation.last_activity_at = now
        return TurnAndUserMessageOrdinalAllocation(
            turn_ordinal=turn_ordinal,
            user_message_ordinal=message_ordinal,
            row_version=conversation.row_version,
        )

    async def allocate_message_ordinal(
        self,
        query: GetConversationQuery,
    ) -> MessageOrdinalAllocation | None:
        conversation = await self.get_for_owner(query)
        if conversation is None:
            return None
        message_ordinal = conversation.allocate_message_ordinal()
        now = self.store.now()
        conversation.updated_at = now
        conversation.last_activity_at = now
        return MessageOrdinalAllocation(
            message_ordinal=message_ordinal,
            row_version=conversation.row_version,
        )

    async def record_terminal_activity(self, query: GetConversationQuery) -> int | None:
        conversation = await self.get_for_owner(query)
        if conversation is None:
            return None
        conversation.row_version += 1
        now = self.store.now()
        conversation.updated_at = now
        conversation.last_activity_at = now
        return conversation.row_version


class FakeTurnRepository:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def find_by_idempotency(
        self,
        principal: Principal,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> GetTurnResult | None:
        result = self.store.find_turn(conversation_id, idempotency_key)
        if result is None:
            return None
        if not result.result.turn.conversation_id == conversation_id:
            return None
        conversation = self.store.conversations.get(conversation_id)
        if conversation is None or not conversation.owner.owns(principal.issuer, principal.subject):
            return None
        return result

    async def get_for_owner(self, query: GetTurnQuery) -> GetTurnResult | None:
        return await self._get(query)

    async def get_for_owner_for_update(self, query: GetTurnQuery) -> GetTurnResult | None:
        self.store.locked_turn_ids.append(query.turn_id)
        return await self._get(query)

    async def _get(self, query: GetTurnQuery) -> GetTurnResult | None:
        result = self.store.turns.get(query.turn_id)
        if result is None or result.result.turn.conversation_id != query.conversation_id:
            return None
        conversation = self.store.conversations.get(query.conversation_id)
        if conversation is None or not conversation.owner.owns(
            query.principal.issuer, query.principal.subject
        ):
            return None
        return result

    async def add(self, turn: ChatTurn, user_message: Message, principal: Principal) -> None:
        del principal
        user_message = replace(user_message, created_at=self.store.now())
        turn.created_at = self.store.now()
        turn.updated_at = turn.created_at
        self.store.turns[turn.id] = GetTurnResult(
            result=TurnResult(turn=turn, user_message=user_message, assistant_message=None)
        )

    async def add_assistant_message(self, message: Message, principal: Principal) -> None:
        del principal
        result = self.store.turns[message.turn_id]
        self.store.turns[message.turn_id] = GetTurnResult(
            result=replace(
                result.result, assistant_message=replace(message, created_at=self.store.now())
            ),
            error=result.error,
        )

    async def update(self, turn: ChatTurn, principal: Principal) -> None:
        del principal
        result = self.store.turns[turn.id]
        now = self.store.now()
        turn.updated_at = now
        if turn.state is InternalTurnState.RETRIEVING and turn.started_at is None:
            turn.started_at = now
        if (
            turn.state
            in {
                InternalTurnState.COMPLETED,
                InternalTurnState.FAILED,
                InternalTurnState.FAILED_UNKNOWN,
            }
            and turn.completed_at is None
        ):
            turn.completed_at = now
        self.store.turns[turn.id] = GetTurnResult(
            result=replace(result.result, turn=turn),
            error=None if turn.failure is None else SafeError.from_turn_failure(turn.failure),
        )

    async def list_messages(self, query: ListMessagesQuery) -> ListMessagesResult:
        result_items = tuple(
            message
            for result in self.store.turns.values()
            if result.result.turn.conversation_id == query.conversation_id
            for message in (result.result.user_message, result.result.assistant_message)
            if message is not None
        )
        return ListMessagesResult(items=result_items[: query.limit], next_cursor=None)

    async def recover_stale(self, processing_stale_seconds: float) -> int:
        del processing_stale_seconds
        return 0


class FakePurgeRepository:
    async def create_preview(self, command: Any) -> Any:
        del command
        raise NotImplementedError

    async def get_preview_for_creator(self, preview_id: UUID, creator: Principal) -> Any:
        del preview_id, creator
        raise NotImplementedError

    async def execute(self, command: Any) -> Any:
        del command
        raise NotImplementedError


class FakeUnitOfWork(UnitOfWorkPort):
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.conversations = FakeConversationRepository(store)
        self.turns = FakeTurnRepository(store)
        self.purges = FakePurgeRepository()

    async def __aenter__(self) -> FakeUnitOfWork:
        self.store.active_uow_count += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.store.active_uow_count -= 1

    async def commit(self) -> None:
        self.store.commit_count += 1

    async def rollback(self) -> None:
        return None


class FakeCapacityGate(CapacityGatePort):
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.acquire_calls = 0
        self.release_calls = 0
        self.released: list[CapacityLease] = []

    async def acquire(self, wait_timeout_seconds: float) -> CapacityLease | None:
        del wait_timeout_seconds
        self.acquire_calls += 1
        if not self.available:
            return None
        return CapacityLease(lease_id=uuid4())

    async def release(self, lease: CapacityLease) -> None:
        self.release_calls += 1
        self.released.append(lease)


class FakeRetrievalGateway(RetrievalGatewayPort):
    def __init__(self, store: FakeStore, result: RetrievalGatewayResult | None = None) -> None:
        self.store = store
        self.result = result or retrieval_result()
        self.requests: list[Any] = []
        self.started = asyncio.Event()
        self.continue_event: asyncio.Event | None = None
        self.error: Exception | None = None

    async def retrieve(self, request: Any) -> RetrievalGatewayResult:
        assert self.store.active_uow_count == 0
        self.requests.append(request)
        self.started.set()
        if self.continue_event is not None:
            await self.continue_event.wait()
        if self.error is not None:
            raise self.error
        return self.result

    async def is_ready(self) -> bool:
        return True


class FakeAnswerGateway(AnswerGenerationGatewayPort):
    def __init__(self, store: FakeStore, result: AnswerGenerationResult | None = None) -> None:
        self.store = store
        self.result = result or answer_result()
        self.requests: list[Any] = []
        self.error: Exception | None = None

    async def generate(self, request: Any) -> AnswerGenerationResult:
        assert self.store.active_uow_count == 0
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result

    async def is_ready(self) -> bool:
        return True


def make_command(
    *,
    principal: Principal | None = None,
    key: str = "00000000-0000-0000-0000-000000000001",
    question: str = "員工每週可以遠端工作幾天？",
) -> SubmitTurnCommand:
    return SubmitTurnCommand(
        principal=principal or owner(),
        conversation_id=CONVERSATION_ID,
        idempotency_key=key,
        question=question,
        language=" zh-TW ",
        request_id="request-001",
    )


def make_submitter(
    *,
    result: RetrievalGatewayResult | None = None,
    answer: AnswerGenerationResult | None = None,
    capacity: FakeCapacityGate | None = None,
    store: FakeStore | None = None,
    retrieval_limit: int = 8,
    queue_wait_timeout_seconds: float = 2.0,
) -> tuple[SubmitTurn, FakeStore, FakeRetrievalGateway, FakeAnswerGateway, FakeCapacityGate]:
    shared = store or FakeStore()
    shared.conversations[CONVERSATION_ID] = make_conversation()
    retrieval = FakeRetrievalGateway(shared, result)
    generation = FakeAnswerGateway(shared, answer)
    gate = capacity or FakeCapacityGate()
    submitter = SubmitTurn(
        lambda: FakeUnitOfWork(shared),
        retrieval,
        generation,
        gate,
        retrieval_limit=retrieval_limit,
        queue_wait_timeout_seconds=queue_wait_timeout_seconds,
    )
    return submitter, shared, retrieval, generation, gate


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000001"),
        ("00000000000000000000000000000001", "00000000-0000-0000-0000-000000000001"),
        (" 00000000-0000-0000-0000-000000000001 ", "00000000-0000-0000-0000-000000000001"),
    ],
)
def test_idempotency_key_is_canonicalized(raw: str, expected: str) -> None:
    assert canonicalize_idempotency_key(raw) == expected


def test_request_fingerprint_uses_canonical_json() -> None:
    assert request_fingerprint("A\nB", "zh-TW") == request_fingerprint("A\nB", "zh-TW")
    assert request_fingerprint("A\nB", "zh-TW") != request_fingerprint("A\nB", None)


@pytest.mark.parametrize(
    "value",
    [True, 0, -1, MAX_RETRY_AFTER_SECONDS + 1, float("nan"), float("inf")],
)
def test_submit_turn_rejects_invalid_queue_wait_timeout_before_execution(value: object) -> None:
    with pytest.raises(ValueError):
        make_submitter(queue_wait_timeout_seconds=value)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_submit_turn_projects_mixed_evidence_and_replays_without_downstream_calls() -> None:
    submitter, store, retrieval, generation, gate = make_submitter(
        result=retrieval_result(mixed=True),
        retrieval_limit=6,
    )

    first = await submitter.execute(make_command())
    second = await submitter.execute(make_command())

    assert first.result.turn.state is InternalTurnState.COMPLETED
    assert first.result.turn.outcome is ChatOutcome.ANSWERED
    assert first.result.assistant_message is not None
    assert first.result.assistant_message.content == "每週可遠端工作兩天。\n\n請依主管流程申請。"
    assert len(retrieval.requests) == 1
    assert len(generation.requests) == 1
    assert retrieval.requests[0].limit == 6
    assert retrieval.requests[0].query == generation.requests[0].question
    assert retrieval.requests[0].language == generation.requests[0].language == "zh-TW"
    assert len(generation.requests[0].evidence) == 1
    assert generation.requests[0].evidence[0].rank == 2
    assert generation.requests[0].context_attestation == "opaque-attestation-token"
    assert second.result.turn.id == first.result.turn.id
    assert store.commit_count >= 5
    assert gate.acquire_calls == gate.release_calls == 1
    assert "opaque-attestation-token" not in repr(store.turns)


@pytest.mark.asyncio
async def test_same_key_processing_replay_returns_existing_turn_and_does_not_duplicate_work() -> (
    None
):
    submitter, _store, retrieval, generation, _gate = make_submitter()
    retrieval.continue_event = asyncio.Event()
    first_task = asyncio.create_task(submitter.execute(make_command()))
    await retrieval.started.wait()

    replay = await submitter.execute(make_command())

    assert replay.result.turn.state is InternalTurnState.RETRIEVING
    assert replay.result.turn.public_state.value == "processing"
    assert generation.requests == []
    assert len(retrieval.requests) == 1

    retrieval.continue_event.set()
    completed = await first_task
    assert completed.result.turn.state is InternalTurnState.COMPLETED
    assert len(retrieval.requests) == len(generation.requests) == 1


@pytest.mark.asyncio
async def test_same_key_different_processed_payload_is_conflict() -> None:
    submitter, _store, _retrieval, _generation, _gate = make_submitter()
    await submitter.execute(make_command())

    with pytest.raises(IdempotencyConflictError):
        await submitter.execute(make_command(question="不同問題"))


@pytest.mark.asyncio
async def test_restricted_only_and_insufficient_evidence_never_call_answer() -> None:
    restricted_submitter, restricted_store, restricted_retrieval, restricted_answer, _ = (
        make_submitter(
            result=retrieval_result(RetrievalStatus.EXTERNAL_GENERATION_BLOCKED),
        )
    )
    restricted = await restricted_submitter.execute(make_command())
    assert restricted.result.turn.outcome is ChatOutcome.EXTERNAL_GENERATION_BLOCKED
    assert restricted.result.assistant_message is not None
    assert restricted.result.assistant_message.answer_parts == ()
    assert len(restricted.result.assistant_message.citations) == 1
    assert restricted_answer.requests == []
    assert "restricted-machine-id" not in restricted.result.assistant_message.content
    assert "restricted-machine-id" not in repr(restricted_store.turns)
    assert len(restricted_retrieval.requests) == 1

    insufficient_submitter, _store, _retrieval, insufficient_answer, _ = make_submitter(
        result=retrieval_result(RetrievalStatus.INSUFFICIENT_EVIDENCE),
    )
    insufficient = await insufficient_submitter.execute(make_command())
    assert insufficient.result.turn.outcome is ChatOutcome.INSUFFICIENT_EVIDENCE
    assert insufficient.result.assistant_message is not None
    assert insufficient.result.assistant_message.citations == ()
    assert insufficient_answer.requests == []


@pytest.mark.asyncio
async def test_answer_outbound_policy_block_is_business_result_without_fallback() -> None:
    submitter, _store, retrieval, answer, _gate = make_submitter()
    answer.error = DependencyError("outbound_policy_blocked", "raw restricted provider body")

    result = await submitter.execute(make_command())

    assert result.result.turn.state is InternalTurnState.COMPLETED
    assert result.result.turn.outcome is ChatOutcome.OUTBOUND_POLICY_BLOCKED
    assert result.result.assistant_message is not None
    assert result.result.assistant_message.answer_parts == ()
    assert "raw restricted provider body" not in repr(result)
    assert len(retrieval.requests) == len(answer.requests) == 1


@pytest.mark.asyncio
async def test_provider_timeout_is_failed_unknown_and_is_replayed_without_retry() -> None:
    submitter, _store, retrieval, answer, _gate = make_submitter()
    answer.error = DependencyError("provider_timeout", "provider raw timeout body")

    failed = await submitter.execute(make_command())
    replay = await submitter.execute(make_command())

    assert failed.result.turn.state is InternalTurnState.FAILED_UNKNOWN
    assert failed.error is not None
    assert failed.error.code == "provider_timeout"
    assert failed.error.retryable is False
    assert "provider raw timeout body" not in repr(failed)
    assert replay.error == failed.error
    assert len(retrieval.requests) == len(answer.requests) == 1


@pytest.mark.asyncio
async def test_capacity_rejection_is_terminal_429_and_replayable() -> None:
    gate = FakeCapacityGate(available=False)
    submitter, _store, retrieval, answer, _ = make_submitter(
        capacity=gate,
        queue_wait_timeout_seconds=1.1,
    )

    failed = await submitter.execute(make_command())
    replay = await submitter.execute(make_command())

    assert failed.result.turn.state is InternalTurnState.FAILED
    assert failed.error is not None
    assert failed.error.code == "chat_capacity_exceeded"
    assert failed.error.retryable is True
    assert failed.error.retry_after_seconds == 2
    assert replay.error == failed.error
    assert retrieval.requests == []
    assert answer.requests == []
    assert gate.release_calls == 0


@pytest.mark.asyncio
async def test_capacity_rejection_caps_retry_after_at_one_day_and_replays() -> None:
    gate = FakeCapacityGate(available=False)
    submitter, _store, retrieval, answer, _ = make_submitter(
        capacity=gate,
        queue_wait_timeout_seconds=MAX_RETRY_AFTER_SECONDS,
    )

    failed = await submitter.execute(make_command())
    replay = await submitter.execute(make_command())

    assert failed.error is not None
    assert failed.error.retry_after_seconds == MAX_RETRY_AFTER_SECONDS
    assert replay.error == failed.error
    assert retrieval.requests == []
    assert answer.requests == []


@pytest.mark.asyncio
async def test_malformed_retrieval_or_answer_response_fails_closed() -> None:
    malformed_retrieval = replace(retrieval_result(), selected_count=0)
    submitter, _store, retrieval, answer, _gate = make_submitter(result=malformed_retrieval)
    failed = await submitter.execute(make_command())
    assert failed.result.turn.state is InternalTurnState.FAILED
    assert failed.error is not None
    assert failed.error.code == "retrieval_contract_error"
    assert answer.requests == []
    assert len(retrieval.requests) == 1

    malformed_answer = replace(answer_result(), answer_parts=())
    submitter, _store, retrieval, answer, _gate = make_submitter(answer=malformed_answer)
    failed = await submitter.execute(make_command())
    assert failed.result.turn.state is InternalTurnState.FAILED
    assert failed.error is not None
    assert failed.error.code == "answer_contract_error"
    assert len(retrieval.requests) == len(answer.requests) == 1


@pytest.mark.asyncio
async def test_crud_turn_and_message_use_cases_keep_repository_boundaries() -> None:
    store = FakeStore()
    principal = owner()
    created = await CreateConversation(lambda: FakeUnitOfWork(store)).execute(
        CreateConversationCommand(title="Help Desk"),
        principal,
    )
    assert created.title == "Help Desk"
    conversation_id = created.conversation_id

    listed = await ListConversations(lambda: FakeUnitOfWork(store)).execute(
        ListConversationsQuery(principal=principal, limit=10)
    )
    assert listed.items[0].conversation_id == conversation_id

    updated = await UpdateConversation(lambda: FakeUnitOfWork(store)).execute(
        UpdateConversationCommand(
            principal=principal,
            conversation_id=conversation_id,
            status=ConversationStatus.ARCHIVED,
        )
    )
    assert updated.status is ConversationStatus.ARCHIVED
    fetched = await GetConversation(lambda: FakeUnitOfWork(store)).execute(
        GetConversationQuery(principal=principal, conversation_id=conversation_id)
    )
    assert fetched.status is ConversationStatus.ARCHIVED

    # Empty turn/message pages still exercise the owner-scoped read use cases.
    turn_query = GetTurnQuery(principal=principal, conversation_id=conversation_id, turn_id=uuid4())
    with pytest.raises(OwnershipError):
        await GetTurn(lambda: FakeUnitOfWork(store)).execute(turn_query)
    messages = await ListMessages(lambda: FakeUnitOfWork(store)).execute(
        ListMessagesQuery(principal=principal, conversation_id=conversation_id, limit=10)
    )
    assert messages.items == ()


def test_failure_value_object_remains_safe_and_bounded() -> None:
    failure = TurnFailure(
        response_http_status=503,
        safe_error_code="provider_unavailable",
        safe_error_message="目前的回答模型暫時無法使用。",
        error_retryable=True,
        retry_after_seconds=3,
    )
    assert SafeError.from_turn_failure(failure).code == "provider_unavailable"


@pytest.mark.asyncio
async def test_answer_diagnostics_are_validated_before_terminal_persistence() -> None:
    malformed = replace(answer_result(), diagnostics="provider raw payload")
    submitter, _store, _retrieval, answer, _gate = make_submitter(answer=malformed)

    result = await submitter.execute(make_command())

    assert result.error is not None
    assert result.error.code == "answer_contract_error"
    assert answer.requests
