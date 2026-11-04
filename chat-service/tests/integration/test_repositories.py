"""PostgreSQL integration tests for the concrete persistence repositories."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from chat_service.application.dtos import (
    ConversationCursorPayload,
    CreateConversationCommand,
    ExecuteConversationPurgeCommand,
    GetConversationQuery,
    GetTurnQuery,
    ListConversationsQuery,
    ListMessagesQuery,
    MessageCursorPayload,
    PreviewConversationPurgeCommand,
    UpdateConversationCommand,
)
from chat_service.domain.entities import (
    MAX_RETRY_AFTER_SECONDS,
    PROCESSING_INTERRUPTED_ERROR_CODE,
    PROCESSING_INTERRUPTED_ERROR_MESSAGE,
    AnswerPart,
    ChatTurn,
    Citation,
    GenerationDiagnostics,
    Message,
    Principal,
    PurgeSelector,
    RetrievalDiagnostics,
    TurnFailure,
)
from chat_service.domain.enums import (
    AnswerStatus,
    ChatOutcome,
    ConversationStatus,
    InternalTurnState,
    MessageRole,
    RetrievalStatus,
)
from chat_service.domain.exceptions import (
    ArchivedConversationError,
    ConversationBusyError,
    DomainInvariantError,
    IdempotencyConflictError,
    OwnershipError,
    PurgePreviewConsumedError,
    PurgePreviewExpiredError,
    PurgePreviewNotFoundError,
    PurgePreviewStaleError,
    PurgeSelectionTooLargeError,
)
from chat_service.infrastructure.persistence.models import (
    CitationModel,
    ConversationModel,
    MessageModel,
    PurgeAuditModel,
    PurgePreviewModel,
    TurnModel,
)
from chat_service.infrastructure.persistence.repositories import (
    SqlAlchemyConversationRepository,
    SqlAlchemyPurgeRepository,
    SqlAlchemyTurnRepository,
)

pytestmark = pytest.mark.integration

LOCK_TIMEOUT_SECONDS = 5.0
TEST_ISSUER = "urn:repository-integration"


def _principal(
    subject: str,
    *,
    issuer: str = TEST_ISSUER,
    tenant_id: str | None = "tenant-a",
    admin: bool = False,
) -> Principal:
    return Principal(
        issuer=issuer,
        subject=subject,
        tenant_id=tenant_id,
        group_ids=("all-employees",),
        roles=("helpdesk-admin",) if admin else (),
    )


def _conversation_query(principal: Principal, conversation_id: UUID) -> GetConversationQuery:
    return GetConversationQuery(principal=principal, conversation_id=conversation_id)


async def _create_conversation(
    session: AsyncSession,
    owner: Principal,
    *,
    title: str | None = None,
) -> UUID:
    conversation = await SqlAlchemyConversationRepository(session).add(
        CreateConversationCommand(title=title),
        owner,
    )
    return conversation.id


async def _create_turn(
    session: AsyncSession,
    owner: Principal,
    conversation_id: UUID,
    *,
    idempotency_key: str | None = None,
    question: str = "How many remote work days are allowed?",
    fingerprint: str = "a" * 64,
) -> tuple[ChatTurn, Message]:
    allocation = await SqlAlchemyConversationRepository(
        session
    ).allocate_turn_and_user_message_ordinals(_conversation_query(owner, conversation_id))
    assert allocation is not None
    turn = ChatTurn(
        id=uuid4(),
        conversation_id=conversation_id,
        ordinal=allocation.turn_ordinal,
        idempotency_key=idempotency_key or str(uuid4()),
        request_fingerprint=fingerprint,
        original_question=question,
        processed_query=question.strip(),
        language="en",
        request_id=f"turn-request-{allocation.turn_ordinal}",
    )
    user_message = Message(
        id=uuid4(),
        conversation_id=conversation_id,
        turn_id=turn.id,
        ordinal=allocation.user_message_ordinal,
        role=MessageRole.USER,
        content=question,
    )
    await SqlAlchemyTurnRepository(session).add(turn, user_message, owner)
    return turn, user_message


async def _load_turn(
    session_factory: async_sessionmaker[AsyncSession],
    owner: Principal,
    conversation_id: UUID,
    turn_id: UUID,
) -> ChatTurn:
    async with session_factory() as session:
        loaded = await SqlAlchemyTurnRepository(session).get_for_owner(
            GetTurnQuery(
                principal=owner,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        )
        assert loaded is not None
        return loaded.result.turn


async def _transition_turn(
    session_factory: async_sessionmaker[AsyncSession],
    owner: Principal,
    conversation_id: UUID,
    turn_id: UUID,
    target: InternalTurnState,
) -> ChatTurn:
    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        loaded = await repository.get_for_owner_for_update(
            GetTurnQuery(
                principal=owner,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        )
        assert loaded is not None
        turn = loaded.result.turn
        turn.transition_to(target)
        await repository.update(turn, owner)
        await session.commit()
        return turn


async def _set_conversation_times(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    created_at: datetime | None = None,
    last_activity_at: datetime | None = None,
) -> None:
    values: dict[str, datetime] = {}
    if created_at is not None:
        values["created_at"] = created_at
    if last_activity_at is not None:
        values["last_activity_at"] = last_activity_at
    await session.execute(
        update(ConversationModel).where(ConversationModel.id == conversation_id).values(**values)
    )


async def _insert_turn_with_messages(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    state: str,
    message_count: int = 1,
    turn_ordinal: int = 1,
    first_message_ordinal: int = 1,
    secret: str = "sensitive conversation content",
) -> UUID:
    """Create valid child rows directly so purge tests isolate the purge repository."""

    turn_id = uuid4()
    session.add(
        TurnModel(
            id=turn_id,
            conversation_id=conversation_id,
            ordinal=turn_ordinal,
            idempotency_key=str(uuid4()),
            request_fingerprint="f" * 64,
            original_question=secret,
            processed_query=secret,
            language="zh-TW",
            state=state,
            request_id="purge-fixture-request",
        )
    )
    # The fixture intentionally inserts ORM rows directly, without a relationship
    # for SQLAlchemy to use when ordering the unit of work.  Flush the parent
    # first so the message foreign key is visible to PostgreSQL on every driver.
    await session.flush()
    for offset in range(message_count):
        ordinal = first_message_ordinal + offset
        role = "user" if offset == 0 else "assistant"
        response_kind = None if role == "user" else "insufficient_evidence"
        session.add(
            MessageModel(
                id=uuid4(),
                conversation_id=conversation_id,
                turn_id=turn_id,
                ordinal=ordinal,
                role=role,
                response_kind=response_kind,
                content=f"{secret}-{ordinal}",
                answer_parts=None,
            )
        )
    await session.flush()
    return turn_id


def _preview_command(
    principal: Principal,
    selector: PurgeSelector,
) -> PreviewConversationPurgeCommand:
    return PreviewConversationPurgeCommand(
        principal=principal,
        selector=selector,
        request_id="purge-preview-request",
    )


def _execute_command(
    principal: Principal,
    preview_id: UUID,
    *,
    reason: str = "approved retention request",
) -> ExecuteConversationPurgeCommand:
    return ExecuteConversationPurgeCommand(
        principal=principal,
        preview_id=preview_id,
        reason=reason,
        request_id="purge-execute-request",
    )


async def _row_count(session: AsyncSession, model: type[object]) -> int:
    value = await session.scalar(select(func.count()).select_from(model))
    assert value is not None
    return int(value)


async def test_conversation_owner_isolation_crud_and_domain_roundtrip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("owner")
    wrong_subject = _principal("other-user")
    wrong_issuer = _principal("owner", issuer="urn:other-issuer")

    async with session_factory() as session:
        repository = SqlAlchemyConversationRepository(session)
        created = await repository.add(CreateConversationCommand(title="initial title"), owner)
        await session.commit()

    assert created.owner.issuer == owner.issuer
    assert created.owner.subject == owner.subject
    assert created.owner.tenant_id == owner.tenant_id
    assert created.title == "initial title"
    assert created.status is ConversationStatus.ACTIVE
    assert created.next_turn_ordinal == 1
    assert created.next_message_ordinal == 1
    assert created.row_version == 1
    assert created.created_at is not None
    assert created.updated_at is not None
    assert created.last_activity_at is not None
    assert created.created_at.tzinfo is not None

    async with session_factory() as session:
        repository = SqlAlchemyConversationRepository(session)
        owned = await repository.get_for_owner(_conversation_query(owner, created.id))
        assert owned == created
        assert (
            await repository.get_for_owner(_conversation_query(wrong_subject, created.id)) is None
        )
        assert await repository.get_for_owner(_conversation_query(wrong_issuer, created.id)) is None
        assert (
            await repository.get_for_owner_for_update(
                _conversation_query(wrong_subject, created.id)
            )
            is None
        )
        assert (
            await repository.allocate_turn_and_user_message_ordinals(
                _conversation_query(wrong_subject, created.id)
            )
            is None
        )
        assert (
            await repository.allocate_message_ordinal(_conversation_query(wrong_issuer, created.id))
            is None
        )
        assert (
            await repository.record_terminal_activity(
                _conversation_query(wrong_subject, created.id)
            )
            is None
        )
        with pytest.raises(OwnershipError):
            await repository.update(
                UpdateConversationCommand(
                    principal=wrong_subject,
                    conversation_id=created.id,
                    title="must not be written",
                )
            )
        await session.rollback()

    async with session_factory() as session:
        repository = SqlAlchemyConversationRepository(session)
        updated = await repository.update(
            UpdateConversationCommand(
                principal=owner,
                conversation_id=created.id,
                title=None,
                status=ConversationStatus.ARCHIVED,
            )
        )
        await session.commit()

    assert updated.title is None
    assert updated.status is ConversationStatus.ARCHIVED
    assert updated.row_version == 2
    assert updated.updated_at is not None
    assert created.updated_at is not None
    assert updated.updated_at >= created.updated_at
    assert updated.last_activity_at == created.last_activity_at

    async with session_factory() as session:
        repository = SqlAlchemyConversationRepository(session)
        with pytest.raises(ArchivedConversationError):
            await repository.allocate_turn_and_user_message_ordinals(
                _conversation_query(owner, created.id)
            )
        await session.rollback()


async def test_conversation_descending_keyset_is_stable_and_owner_scoped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("page-owner")
    outsider = _principal("page-outsider")
    newer = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    older = newer - timedelta(days=1)

    async with session_factory() as session:
        owner_ids = [
            await _create_conversation(session, owner, title=f"owner-{index}") for index in range(4)
        ]
        outsider_id = await _create_conversation(session, outsider, title="outsider")
        for conversation_id in owner_ids[:3]:
            await _set_conversation_times(
                session,
                conversation_id,
                last_activity_at=newer,
            )
        await _set_conversation_times(session, owner_ids[3], last_activity_at=older)
        await _set_conversation_times(
            session,
            outsider_id,
            last_activity_at=newer + timedelta(days=1),
        )
        await session.commit()

    expected = [*sorted(owner_ids[:3], reverse=True), owner_ids[3]]
    async with session_factory() as session:
        repository = SqlAlchemyConversationRepository(session)
        first_page = await repository.list_for_owner(
            ListConversationsQuery(principal=owner, limit=2)
        )
        assert [item.conversation_id for item in first_page.items] == expected[:2]
        assert first_page.next_cursor == ConversationCursorPayload(
            version=1,
            last_activity_at=newer,
            conversation_id=expected[1],
        )

        second_page = await repository.list_for_owner(
            ListConversationsQuery(
                principal=owner,
                limit=2,
                cursor=first_page.next_cursor,
            )
        )
        assert [item.conversation_id for item in second_page.items] == expected[2:]
        assert second_page.next_cursor is None
        assert outsider_id not in {item.conversation_id for item in first_page.items}
        assert outsider_id not in {item.conversation_id for item in second_page.items}


async def test_conversation_row_lock_serializes_concurrent_ordinal_allocation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("concurrent-owner")
    async with session_factory() as setup_session:
        conversation_id = await _create_conversation(setup_session, owner)
        await setup_session.commit()

    query = _conversation_query(owner, conversation_id)
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()

    async def hold_parent_lock() -> None:
        async with session_factory() as session:
            repository = SqlAlchemyConversationRepository(session)
            assert await repository.get_for_owner_for_update(query) is not None
            lock_acquired.set()
            await release_lock.wait()
            await session.commit()

    async def allocate() -> object:
        await lock_acquired.wait()
        async with session_factory() as session:
            allocation = await SqlAlchemyConversationRepository(
                session
            ).allocate_turn_and_user_message_ordinals(query)
            await session.commit()
            return allocation

    locker = asyncio.create_task(hold_parent_lock())
    allocator = asyncio.create_task(allocate())
    await asyncio.wait_for(lock_acquired.wait(), timeout=LOCK_TIMEOUT_SECONDS)
    await asyncio.sleep(0.1)
    assert not allocator.done(), "ordinal allocation crossed an existing parent row lock"
    release_lock.set()
    allocation = await asyncio.wait_for(allocator, timeout=LOCK_TIMEOUT_SECONDS)
    await asyncio.wait_for(locker, timeout=LOCK_TIMEOUT_SECONDS)
    assert allocation is not None
    assert allocation.turn_ordinal == 1
    assert allocation.user_message_ordinal == 1
    assert allocation.row_version == 2


async def test_concurrent_turn_and_message_ordinals_are_unique_and_versioned_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("ordinal-owner")
    async with session_factory() as setup_session:
        conversation_id = await _create_conversation(setup_session, owner)
        await setup_session.commit()
    query = _conversation_query(owner, conversation_id)

    async def allocate_turn_and_user() -> object:
        async with session_factory() as session:
            allocation = await SqlAlchemyConversationRepository(
                session
            ).allocate_turn_and_user_message_ordinals(query)
            await session.commit()
            return allocation

    turn_allocations = await asyncio.wait_for(
        asyncio.gather(allocate_turn_and_user(), allocate_turn_and_user()),
        timeout=LOCK_TIMEOUT_SECONDS,
    )
    assert {item.turn_ordinal for item in turn_allocations if item is not None} == {1, 2}
    assert {item.user_message_ordinal for item in turn_allocations if item is not None} == {
        1,
        2,
    }
    assert {item.row_version for item in turn_allocations if item is not None} == {2, 3}

    async def allocate_assistant() -> object:
        async with session_factory() as session:
            allocation = await SqlAlchemyConversationRepository(session).allocate_message_ordinal(
                query
            )
            await session.commit()
            return allocation

    assistant_allocations = await asyncio.wait_for(
        asyncio.gather(allocate_assistant(), allocate_assistant()),
        timeout=LOCK_TIMEOUT_SECONDS,
    )
    assert {item.message_ordinal for item in assistant_allocations if item is not None} == {3, 4}
    assert {item.row_version for item in assistant_allocations if item is not None} == {4, 5}

    async with session_factory() as session:
        model = await session.get(ConversationModel, conversation_id)
        assert model is not None
        assert model.next_turn_ordinal == 3
        assert model.next_message_ordinal == 5
        assert model.row_version == 5


async def test_completed_matching_assistant_roundtrip_with_citations_diagnostics_and_keyset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("turn-owner")
    outsider = _principal("turn-outsider")
    idempotency_key = str(uuid4())
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner, title="roundtrip")
        turn, user_message = await _create_turn(
            session,
            owner,
            conversation_id,
            idempotency_key=idempotency_key,
            question="  original question  ",
        )
        await session.commit()

    assert turn.created_at is not None
    assert turn.updated_at is not None
    assert turn.started_at is None
    assert turn.completed_at is None
    assert user_message.created_at is None

    await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.WAITING_FOR_CAPACITY,
    )
    retrieving = await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.RETRIEVING,
    )
    assert retrieving.started_at is not None

    retrieval_diagnostics = RetrievalDiagnostics(
        candidate_count=7,
        selected_count=2,
        model_id="embedding-model",
        model_revision="revision-7",
    )
    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        loaded = await repository.get_for_owner_for_update(
            GetTurnQuery(owner, conversation_id, turn.id)
        )
        assert loaded is not None
        generating = loaded.result.turn
        generating.retrieval_status = RetrievalStatus.EVIDENCE_FOUND
        generating.retrieval_diagnostics = retrieval_diagnostics
        generating.transition_to(InternalTurnState.GENERATING)
        await repository.update(generating, owner)
        await session.commit()

    first_citation = Citation(
        citation_id="C1",
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        title="Remote Work Rules",
        section_path="Section 3",
        page_number=4,
    )
    second_citation = Citation(
        citation_id="C2",
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        title="Employee Handbook",
    )
    answer_parts = (
        AnswerPart(text="Two days.", citation_ids=("C1", "C2")),
        AnswerPart(text="Manager approval is required.", citation_ids=("C1",)),
    )
    generation_diagnostics = GenerationDiagnostics(
        provider="safe-provider",
        model="generation-model",
        prompt_version="v3",
        finish_reason="stop",
        prompt_tokens=123,
        completion_tokens=45,
    )

    async with session_factory() as session:
        turns = SqlAlchemyTurnRepository(session)
        conversations = SqlAlchemyConversationRepository(session)
        loaded = await turns.get_for_owner_for_update(GetTurnQuery(owner, conversation_id, turn.id))
        assert loaded is not None
        completed = loaded.result.turn
        completed.transition_to(InternalTurnState.COMPLETED)
        completed.answer_status = AnswerStatus.ANSWERED
        completed.outcome = ChatOutcome.ANSWERED
        completed.generation_diagnostics = generation_diagnostics
        await turns.update(completed, owner)
        allocation = await conversations.allocate_message_ordinal(
            _conversation_query(owner, conversation_id)
        )
        assert allocation is not None
        assistant_message = Message(
            id=uuid4(),
            conversation_id=conversation_id,
            turn_id=turn.id,
            ordinal=allocation.message_ordinal,
            role=MessageRole.ASSISTANT,
            content="Two days.\n\nManager approval is required.",
            response_kind=ChatOutcome.ANSWERED,
            answer_parts=answer_parts,
            citations=(first_citation, second_citation),
        )
        await turns.add_assistant_message(assistant_message, owner)
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        by_id = await repository.get_for_owner(GetTurnQuery(owner, conversation_id, turn.id))
        assert by_id is not None
        replay = await repository.find_by_idempotency(
            owner,
            conversation_id,
            idempotency_key,
        )
        assert replay == by_id
        assert by_id.error is None
        assert by_id.result.turn.state is InternalTurnState.COMPLETED
        assert by_id.result.turn.retrieval_status is RetrievalStatus.EVIDENCE_FOUND
        assert by_id.result.turn.answer_status is AnswerStatus.ANSWERED
        assert by_id.result.turn.outcome is ChatOutcome.ANSWERED
        assert by_id.result.turn.retrieval_diagnostics == retrieval_diagnostics
        assert by_id.result.turn.generation_diagnostics == generation_diagnostics
        assert by_id.result.turn.started_at is not None
        assert by_id.result.turn.completed_at is not None
        assert by_id.result.turn.updated_at == by_id.result.turn.completed_at
        assert by_id.result.user_message.content == "  original question  "
        assert by_id.result.user_message.created_at is not None
        assert by_id.result.assistant_message is not None
        assert by_id.result.assistant_message.answer_parts == answer_parts
        assert by_id.result.assistant_message.citations == (first_citation, second_citation)
        assert by_id.result.assistant_message.created_at is not None

        assert (
            await repository.get_for_owner(GetTurnQuery(outsider, conversation_id, turn.id)) is None
        )
        assert (
            await repository.find_by_idempotency(outsider, conversation_id, idempotency_key) is None
        )
        outsider_page = await repository.list_messages(
            ListMessagesQuery(principal=outsider, conversation_id=conversation_id, limit=10)
        )
        assert outsider_page.items == ()
        assert outsider_page.next_cursor is None
        await session.rollback()

    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        first_page = await repository.list_messages(
            ListMessagesQuery(principal=owner, conversation_id=conversation_id, limit=1)
        )
        assert [message.ordinal for message in first_page.items] == [1]
        assert first_page.next_cursor == MessageCursorPayload(version=1, ordinal=1)
        await session.rollback()

    async with session_factory() as session:
        second_page = await SqlAlchemyTurnRepository(session).list_messages(
            ListMessagesQuery(
                principal=owner,
                conversation_id=conversation_id,
                limit=1,
                cursor=MessageCursorPayload(version=1, ordinal=1),
            )
        )
        assert [message.ordinal for message in second_page.items] == [2]
        assert second_page.items[0].citations == (first_citation, second_citation)
        assert second_page.next_cursor is None

    async with session_factory() as session:
        persisted = await session.get(TurnModel, turn.id)
        assert persisted is not None
        assert persisted.retrieval_diagnostics == {
            "candidate_count": 7,
            "selected_count": 2,
            "model_id": "embedding-model",
            "model_revision": "revision-7",
        }
        assert persisted.generation_diagnostics == {
            "provider": "safe-provider",
            "model": "generation-model",
            "prompt_version": "v3",
            "finish_reason": "stop",
            "prompt_tokens": 123,
            "completion_tokens": 45,
        }
        serialized_diagnostics = repr(
            (persisted.retrieval_diagnostics, persisted.generation_diagnostics)
        )
        for forbidden in ("evidence", "attestation", "question", "answer", "secret"):
            assert forbidden not in serialized_diagnostics.lower()


async def test_safe_failure_roundtrip_and_idempotent_replay_has_no_assistant_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("failed-turn-owner")
    idempotency_key = str(uuid4())
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        turn, _ = await _create_turn(
            session,
            owner,
            conversation_id,
            idempotency_key=idempotency_key,
        )
        await session.commit()

    waiting = await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.WAITING_FOR_CAPACITY,
    )
    assert waiting.state is InternalTurnState.WAITING_FOR_CAPACITY

    failure = TurnFailure(
        response_http_status=429,
        safe_error_code="chat_capacity_exceeded",
        safe_error_message="The chat service is busy; retry later.",
        error_retryable=True,
        retry_after_seconds=MAX_RETRY_AFTER_SECONDS,
    )
    diagnostics = RetrievalDiagnostics(
        candidate_count=0,
        selected_count=0,
        model_id="not-called",
        model_revision="not-called",
    )
    async with session_factory() as session:
        turns = SqlAlchemyTurnRepository(session)
        loaded = await turns.get_for_owner_for_update(GetTurnQuery(owner, conversation_id, turn.id))
        assert loaded is not None
        failed = loaded.result.turn
        failed.retrieval_diagnostics = diagnostics
        failed.fail(failure)
        await turns.update(failed, owner)
        version = await SqlAlchemyConversationRepository(session).record_terminal_activity(
            _conversation_query(owner, conversation_id)
        )
        assert version == 3
        await session.commit()

    async with session_factory() as session:
        replay = await SqlAlchemyTurnRepository(session).find_by_idempotency(
            owner,
            conversation_id,
            idempotency_key,
        )
        assert replay is not None
        assert replay.result.turn.state is InternalTurnState.FAILED
        assert replay.result.turn.failure == failure
        assert replay.result.turn.retrieval_diagnostics == diagnostics
        assert replay.result.turn.completed_at is not None
        assert replay.result.assistant_message is None
        assert replay.error is not None
        assert replay.error.code == failure.safe_error_code
        assert replay.error.message == failure.safe_error_message
        assert replay.error.retryable is True
        assert replay.error.retry_after_seconds == MAX_RETRY_AFTER_SECONDS


async def test_update_rejects_mutated_failure_on_non_failed_turn_and_row_remains_readable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("mutable-failure-owner")
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        turn, _ = await _create_turn(session, owner, conversation_id)
        await session.commit()

    waiting = await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.WAITING_FOR_CAPACITY,
    )
    assert waiting.failure is None

    injected_failure = TurnFailure(
        response_http_status=503,
        safe_error_code="must_not_persist",
        safe_error_message="This mutable failure must be rejected.",
        error_retryable=False,
    )
    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        loaded = await repository.get_for_owner_for_update(
            GetTurnQuery(owner, conversation_id, turn.id)
        )
        assert loaded is not None
        corrupted = loaded.result.turn
        corrupted.transition_to(InternalTurnState.RETRIEVING)
        corrupted.failure = injected_failure
        with pytest.raises(DomainInvariantError):
            await repository.update(corrupted, owner)
        await session.rollback()

    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        readable = await repository.get_for_owner(GetTurnQuery(owner, conversation_id, turn.id))
        assert readable is not None
        assert readable.result.turn.state is InternalTurnState.WAITING_FOR_CAPACITY
        assert readable.result.turn.failure is None
        assert readable.error is None
        stored = await session.get(TurnModel, turn.id)
        assert stored is not None
        assert stored.response_http_status is None
        assert stored.safe_error_code is None
        assert stored.safe_error_message is None
        assert stored.error_retryable is False
        assert stored.retry_after_seconds is None


async def test_assistant_message_is_rejected_for_processing_turn_without_child_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("processing-assistant-owner")
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        turn, _ = await _create_turn(session, owner, conversation_id)
        await session.commit()

    citation = Citation(
        citation_id="C1",
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        title="Safe citation projection",
    )
    async with session_factory() as session:
        allocation = await SqlAlchemyConversationRepository(session).allocate_message_ordinal(
            _conversation_query(owner, conversation_id)
        )
        assert allocation is not None
        assistant = Message(
            id=uuid4(),
            conversation_id=conversation_id,
            turn_id=turn.id,
            ordinal=allocation.message_ordinal,
            role=MessageRole.ASSISTANT,
            content="Generation is blocked.",
            response_kind=ChatOutcome.EXTERNAL_GENERATION_BLOCKED,
            citations=(citation,),
        )
        with pytest.raises(DomainInvariantError):
            await SqlAlchemyTurnRepository(session).add_assistant_message(assistant, owner)
        await session.rollback()

    async with session_factory() as session:
        stored_turn = await SqlAlchemyTurnRepository(session).get_for_owner(
            GetTurnQuery(owner, conversation_id, turn.id)
        )
        assert stored_turn is not None
        assert stored_turn.result.turn.state is InternalTurnState.ACCEPTED
        assert stored_turn.result.assistant_message is None
        assert await _row_count(session, MessageModel) == 1
        assert await _row_count(session, CitationModel) == 0
        conversation = await session.get(ConversationModel, conversation_id)
        assert conversation is not None
        assert conversation.next_message_ordinal == 2
        assert conversation.row_version == 2


async def test_assistant_message_outcome_mismatch_is_atomic_without_child_writes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("mismatched-assistant-owner")
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        turn, _ = await _create_turn(session, owner, conversation_id)
        await session.commit()

    await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.WAITING_FOR_CAPACITY,
    )
    await _transition_turn(
        session_factory,
        owner,
        conversation_id,
        turn.id,
        InternalTurnState.RETRIEVING,
    )
    async with session_factory() as session:
        turns = SqlAlchemyTurnRepository(session)
        completed_result = await turns.get_for_owner_for_update(
            GetTurnQuery(owner, conversation_id, turn.id)
        )
        assert completed_result is not None
        completed = completed_result.result.turn
        completed.transition_to(InternalTurnState.COMPLETED)
        completed.outcome = ChatOutcome.INSUFFICIENT_EVIDENCE
        await turns.update(completed, owner)
        version = await SqlAlchemyConversationRepository(session).record_terminal_activity(
            _conversation_query(owner, conversation_id)
        )
        assert version == 3
        await session.commit()

    citation = Citation(
        citation_id="C1",
        chunk_id=uuid4(),
        document_id=uuid4(),
        version_id=uuid4(),
        title="Must not be inserted",
    )
    async with session_factory() as session:
        allocation = await SqlAlchemyConversationRepository(session).allocate_message_ordinal(
            _conversation_query(owner, conversation_id)
        )
        assert allocation is not None
        mismatched = Message(
            id=uuid4(),
            conversation_id=conversation_id,
            turn_id=turn.id,
            ordinal=allocation.message_ordinal,
            role=MessageRole.ASSISTANT,
            content="Generation is blocked.",
            response_kind=ChatOutcome.EXTERNAL_GENERATION_BLOCKED,
            citations=(citation,),
        )
        with pytest.raises(DomainInvariantError):
            await SqlAlchemyTurnRepository(session).add_assistant_message(mismatched, owner)
        await session.rollback()

    async with session_factory() as session:
        readable = await SqlAlchemyTurnRepository(session).get_for_owner(
            GetTurnQuery(owner, conversation_id, turn.id)
        )
        assert readable is not None
        assert readable.result.turn.state is InternalTurnState.COMPLETED
        assert readable.result.turn.outcome is ChatOutcome.INSUFFICIENT_EVIDENCE
        assert readable.result.assistant_message is None
        assert await _row_count(session, MessageModel) == 1
        assert await _row_count(session, CitationModel) == 0
        conversation = await session.get(ConversationModel, conversation_id)
        assert conversation is not None
        assert conversation.next_message_ordinal == 2
        assert conversation.row_version == 3


@pytest.mark.parametrize("same_fingerprint", [True, False], ids=["same-payload", "conflict"])
async def test_idempotency_race_transaction_has_no_ordinal_or_version_gap(
    session_factory: async_sessionmaker[AsyncSession],
    same_fingerprint: bool,
) -> None:
    owner = _principal("idempotency-race-owner")
    idempotency_key = str(uuid4())
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        await session.commit()

    start = asyncio.Event()
    both_initial_lookups_finished = asyncio.Event()
    initial_lookup_count = 0

    async def contender(index: int) -> tuple[str, UUID]:
        nonlocal initial_lookup_count
        question = "same race payload" if same_fingerprint else f"race payload {index}"
        fingerprint = "a" * 64 if same_fingerprint else str(index) * 64
        await start.wait()
        async with session_factory() as session:
            turns = SqlAlchemyTurnRepository(session)
            conversations = SqlAlchemyConversationRepository(session)
            existing = await turns.find_by_idempotency(
                owner,
                conversation_id,
                idempotency_key,
            )
            assert existing is None
            initial_lookup_count += 1
            if initial_lookup_count == 2:
                both_initial_lookups_finished.set()
            await both_initial_lookups_finished.wait()

            allocation = await conversations.allocate_turn_and_user_message_ordinals(
                _conversation_query(owner, conversation_id)
            )
            assert allocation is not None
            turn = ChatTurn(
                id=uuid4(),
                conversation_id=conversation_id,
                ordinal=allocation.turn_ordinal,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                original_question=question,
                processed_query=question,
                language="en",
                request_id=f"race-request-{index}",
            )
            message = Message(
                id=uuid4(),
                conversation_id=conversation_id,
                turn_id=turn.id,
                ordinal=allocation.user_message_ordinal,
                role=MessageRole.USER,
                content=question,
            )
            try:
                await turns.add(turn, message, owner)
                await session.commit()
                return "created", turn.id
            except IdempotencyConflictError:
                await session.rollback()

        async with session_factory() as replay_session:
            replay = await SqlAlchemyTurnRepository(replay_session).find_by_idempotency(
                owner,
                conversation_id,
                idempotency_key,
            )
            assert replay is not None
            outcome = (
                "replay"
                if replay.result.turn.request_fingerprint == fingerprint
                else "payload-conflict"
            )
            winner_id = replay.result.turn.id
            await replay_session.rollback()
            return outcome, winner_id

    tasks = [asyncio.create_task(contender(index)) for index in range(2)]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=LOCK_TIMEOUT_SECONDS)
    expected_loser_outcome = "replay" if same_fingerprint else "payload-conflict"
    assert sorted(outcome for outcome, _ in results) == sorted(("created", expected_loser_outcome))
    winner_ids = {turn_id for _, turn_id in results}
    assert len(winner_ids) == 1
    winner_id = winner_ids.pop()

    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        replay = await repository.find_by_idempotency(
            owner,
            conversation_id,
            idempotency_key,
        )
        assert replay is not None
        assert replay.result.turn.id == winner_id
        assert replay.result.turn.ordinal == 1
        assert replay.result.user_message.turn_id == winner_id
        assert replay.result.user_message.ordinal == 1
        assert await _row_count(session, TurnModel) == 1
        assert await _row_count(session, MessageModel) == 1
        conversation = await session.get(ConversationModel, conversation_id)
        assert conversation is not None
        assert conversation.next_turn_ordinal == 2
        assert conversation.next_message_ordinal == 2
        assert conversation.row_version == 2


async def test_turn_repository_acquires_conversation_lock_before_child_lookup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("turn-lock-owner")
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        turn, _ = await _create_turn(session, owner, conversation_id)
        await session.commit()

    query = GetTurnQuery(owner, conversation_id, turn.id)
    parent_locked = asyncio.Event()
    release_parent = asyncio.Event()

    async def locker() -> None:
        async with session_factory() as session:
            assert (
                await SqlAlchemyConversationRepository(session).get_for_owner_for_update(
                    _conversation_query(owner, conversation_id)
                )
                is not None
            )
            parent_locked.set()
            await release_parent.wait()
            await session.commit()

    async def lookup() -> object:
        await parent_locked.wait()
        async with session_factory() as session:
            result = await SqlAlchemyTurnRepository(session).get_for_owner_for_update(query)
            await session.rollback()
            return result

    locker_task = asyncio.create_task(locker())
    lookup_task = asyncio.create_task(lookup())
    await asyncio.wait_for(parent_locked.wait(), timeout=LOCK_TIMEOUT_SECONDS)
    await asyncio.sleep(0.1)
    assert not lookup_task.done(), "turn lookup did not wait for the parent conversation lock"
    release_parent.set()
    loaded = await asyncio.wait_for(lookup_task, timeout=LOCK_TIMEOUT_SECONDS)
    await asyncio.wait_for(locker_task, timeout=LOCK_TIMEOUT_SECONDS)
    assert loaded is not None
    assert loaded.result.turn.id == turn.id


async def test_recover_stale_uses_database_time_and_updates_each_aggregate_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner = _principal("recovery-owner")
    async with session_factory() as session:
        multi_stale_id = await _create_conversation(session, owner)
        single_stale_id = await _create_conversation(session, owner)
        fresh_id = await _create_conversation(session, owner)
        terminal_id = await _create_conversation(session, owner)
        stale_turn_ids = [
            await _insert_turn_with_messages(
                session,
                multi_stale_id,
                state="accepted",
                turn_ordinal=1,
                first_message_ordinal=1,
            ),
            await _insert_turn_with_messages(
                session,
                multi_stale_id,
                state="generating",
                turn_ordinal=2,
                first_message_ordinal=2,
            ),
            await _insert_turn_with_messages(
                session,
                single_stale_id,
                state="waiting_for_capacity",
            ),
        ]
        fresh_turn_id = await _insert_turn_with_messages(
            session,
            fresh_id,
            state="retrieving",
        )
        terminal_turn_id = await _insert_turn_with_messages(
            session,
            terminal_id,
            state="completed",
        )
        await session.execute(
            update(TurnModel)
            .where(TurnModel.id.in_((*stale_turn_ids, terminal_turn_id)))
            .values(updated_at=func.clock_timestamp() - timedelta(minutes=5))
        )
        await session.execute(
            update(TurnModel)
            .where(TurnModel.id == terminal_turn_id)
            .values(completed_at=func.clock_timestamp() - timedelta(minutes=5))
        )
        await session.execute(
            update(TurnModel)
            .where(TurnModel.id == fresh_turn_id)
            .values(updated_at=func.clock_timestamp())
        )
        await session.commit()

    async with session_factory() as session:
        before_versions = dict(
            (
                await session.execute(select(ConversationModel.id, ConversationModel.row_version))
            ).all()
        )

    async with session_factory() as session:
        repository = SqlAlchemyTurnRepository(session)
        recovered_count = await repository.recover_stale(60.0)
        database_now = await session.scalar(select(func.clock_timestamp()))
        assert database_now is not None
        await session.commit()

    assert recovered_count == 3
    async with session_factory() as session:
        recovered = list(
            (
                await session.scalars(
                    select(TurnModel).where(TurnModel.id.in_(stale_turn_ids)).order_by(TurnModel.id)
                )
            ).all()
        )
        assert len(recovered) == 3
        for model in recovered:
            assert model.state == "failed_unknown"
            assert model.response_http_status == 503
            assert model.safe_error_code == PROCESSING_INTERRUPTED_ERROR_CODE
            assert model.safe_error_message == PROCESSING_INTERRUPTED_ERROR_MESSAGE
            assert model.error_retryable is False
            assert model.retry_after_seconds is None
            assert model.completed_at == model.updated_at
            assert model.completed_at is not None
            assert abs((database_now - model.completed_at).total_seconds()) < 1

        fresh = await session.get(TurnModel, fresh_turn_id)
        terminal = await session.get(TurnModel, terminal_turn_id)
        assert fresh is not None and fresh.state == "retrieving"
        assert fresh.completed_at is None
        assert terminal is not None and terminal.state == "completed"
        assert terminal.completed_at is not None
        assert terminal.completed_at < database_now - timedelta(seconds=60)

        after_versions = dict(
            (
                await session.execute(select(ConversationModel.id, ConversationModel.row_version))
            ).all()
        )
        assert after_versions[multi_stale_id] == before_versions[multi_stale_id] + 1
        assert after_versions[single_stale_id] == before_versions[single_stale_id] + 1
        assert after_versions[fresh_id] == before_versions[fresh_id]
        assert after_versions[terminal_id] == before_versions[terminal_id]

        replay_model = await session.get(TurnModel, stale_turn_ids[0])
        assert replay_model is not None
        replay = await SqlAlchemyTurnRepository(session).find_by_idempotency(
            owner,
            multi_stale_id,
            replay_model.idempotency_key,
        )
        assert replay is not None
        assert replay.result.turn.state is InternalTurnState.FAILED_UNKNOWN
        assert replay.error is not None
        assert replay.error.code == PROCESSING_INTERRUPTED_ERROR_CODE
        assert replay.error.retryable is False


async def test_purge_preview_uses_and_selector_counts_busy_and_has_deterministic_digest(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = _principal("purge-admin", admin=True)
    selected_owner = _principal("selected-owner")
    other_owner = _principal("other-owner")
    cutoff = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    old = cutoff - timedelta(days=1)
    new = cutoff + timedelta(days=1)

    async with session_factory() as session:
        selected_id = await _create_conversation(session, selected_owner, title="secret title")
        wrong_owner_id = await _create_conversation(session, other_owner)
        too_new_id = await _create_conversation(session, selected_owner)
        await _set_conversation_times(session, selected_id, created_at=old)
        await _set_conversation_times(session, wrong_owner_id, created_at=old)
        await _set_conversation_times(session, too_new_id, created_at=new)
        await _insert_turn_with_messages(
            session,
            selected_id,
            state="accepted",
            message_count=1,
        )
        await session.commit()

    selector = PurgeSelector(
        conversation_ids=(selected_id, wrong_owner_id, too_new_id),
        owner_issuer=selected_owner.issuer,
        owner_subject=selected_owner.subject,
        created_before=cutoff,
    )
    async with session_factory() as session:
        repository = SqlAlchemyPurgeRepository(session, preview_ttl_seconds=600)
        first = await repository.create_preview(_preview_command(admin, selector))
        second = await repository.create_preview(_preview_command(admin, selector))
        await session.commit()

    assert first.preview.selected == second.preview.selected
    assert [item.conversation_id for item in first.preview.selected] == [selected_id]
    assert first.preview.selector_digest == second.preview.selector_digest
    assert len(first.preview.selector_digest) == 64
    assert first.preview.conversation_count == 1
    assert first.preview.message_count == 1
    assert first.busy_count == 1
    assert first.preview.expires_at - first.preview.created_at == timedelta(seconds=600)

    async with session_factory() as session:
        repository = SqlAlchemyPurgeRepository(session)
        loaded = await repository.get_preview_for_creator(first.preview.id, admin)
        assert loaded == first.preview
        assert (
            await repository.get_preview_for_creator(first.preview.id, _principal("intruder"))
            is None
        )
        stored = await session.get(PurgePreviewModel, first.preview.id)
        assert stored is not None
        serialized = f"{stored.selector!r}{stored.selected_conversations!r}"
        assert "secret title" not in serialized
        assert "sensitive conversation content" not in serialized


async def test_purge_preview_rejects_the_1001st_match_without_persisting_preview(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = _principal("bulk-admin", admin=True)
    owner = _principal("bulk-owner")
    rows = [
        {
            "id": uuid4(),
            "owner_issuer": owner.issuer,
            "owner_subject": owner.subject,
            "tenant_id": owner.tenant_id,
            "title": None,
            "status": "active",
        }
        for _ in range(1_001)
    ]
    async with session_factory() as session:
        await session.execute(insert(ConversationModel), rows)
        await session.commit()

    selector = PurgeSelector(owner_issuer=owner.issuer, owner_subject=owner.subject)
    async with session_factory() as session:
        repository = SqlAlchemyPurgeRepository(session)
        with pytest.raises(PurgeSelectionTooLargeError):
            await repository.create_preview(_preview_command(admin, selector))
        await session.rollback()

    async with session_factory() as session:
        assert await _row_count(session, ConversationModel) == 1_001


async def test_purge_execute_cascades_consumes_preview_and_writes_content_free_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = _principal("execute-admin", admin=True)
    owner = _principal("execute-owner")
    secret = "question and answer must never enter purge audit"
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner, title="confidential title")
        turn_id = await _insert_turn_with_messages(
            session,
            conversation_id,
            state="completed",
            message_count=2,
            secret=secret,
        )
        assistant_id = await session.scalar(
            select(MessageModel.id).where(
                MessageModel.turn_id == turn_id,
                MessageModel.role == "assistant",
            )
        )
        assert assistant_id is not None
        session.add(
            CitationModel(
                id=uuid4(),
                assistant_message_id=assistant_id,
                citation_id="C1",
                ordinal=1,
                chunk_id=uuid4(),
                document_id=uuid4(),
                version_id=uuid4(),
                title="private document title",
                section_path="private section",
                page_number=3,
            )
        )
        preview = await SqlAlchemyPurgeRepository(session).create_preview(
            _preview_command(admin, PurgeSelector(conversation_ids=(conversation_id,)))
        )
        await session.commit()

    async with session_factory() as session:
        result = await SqlAlchemyPurgeRepository(session).execute(
            _execute_command(admin, preview.preview.id)
        )
        await session.commit()

    assert result.conversation_count == 1
    assert result.message_count == 2
    assert result.purged_at.tzinfo is not None
    async with session_factory() as session:
        assert await _row_count(session, ConversationModel) == 0
        assert await _row_count(session, TurnModel) == 0
        assert await _row_count(session, MessageModel) == 0
        assert await _row_count(session, CitationModel) == 0
        assert await _row_count(session, PurgeAuditModel) == 1
        preview_model = await session.get(PurgePreviewModel, preview.preview.id)
        assert preview_model is not None
        assert preview_model.consumed_at is not None
        audit = await session.get(PurgeAuditModel, result.purge_id)
        assert audit is not None
        assert audit.actor_issuer == admin.issuer
        assert audit.actor_subject == admin.subject
        assert audit.selector_digest == preview.preview.selector_digest
        assert audit.conversation_count == 1
        assert audit.message_count == 2
        assert audit.reason == "approved retention request"
        assert audit.request_id == "purge-execute-request"
        assert set(PurgeAuditModel.__table__.columns.keys()) == {
            "id",
            "actor_issuer",
            "actor_subject",
            "selector_digest",
            "conversation_count",
            "message_count",
            "reason",
            "request_id",
            "purged_at",
        }
        serialized_audit = repr(
            {
                column.name: getattr(audit, column.name)
                for column in PurgeAuditModel.__table__.columns
            }
        )
        assert secret not in serialized_audit
        assert "confidential title" not in serialized_audit
        assert "private document title" not in serialized_audit


@pytest.mark.parametrize("mutation", ["row_version", "missing"])
async def test_stale_or_missing_selection_rolls_back_without_partial_delete(
    session_factory: async_sessionmaker[AsyncSession],
    mutation: str,
) -> None:
    admin = _principal(f"atomic-{mutation}-admin", admin=True)
    owner = _principal(f"atomic-{mutation}-owner")
    async with session_factory() as session:
        conversation_ids = [await _create_conversation(session, owner) for _ in range(2)]
        preview = await SqlAlchemyPurgeRepository(session).create_preview(
            _preview_command(
                admin,
                PurgeSelector(conversation_ids=tuple(conversation_ids)),
            )
        )
        await session.commit()

    async with session_factory() as session:
        if mutation == "row_version":
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation_ids[1])
                .values(row_version=ConversationModel.row_version + 1)
            )
        else:
            missing = await session.get(ConversationModel, conversation_ids[1])
            assert missing is not None
            await session.delete(missing)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(PurgePreviewStaleError):
            await SqlAlchemyPurgeRepository(session).execute(
                _execute_command(admin, preview.preview.id)
            )
        await session.rollback()

    async with session_factory() as session:
        remaining_ids = set((await session.scalars(select(ConversationModel.id))).all())
        expected = set(conversation_ids)
        if mutation == "missing":
            expected.remove(conversation_ids[1])
        assert remaining_ids == expected
        assert await _row_count(session, PurgeAuditModel) == 0
        stored_preview = await session.get(PurgePreviewModel, preview.preview.id)
        assert stored_preview is not None
        assert stored_preview.consumed_at is None


async def test_purge_execute_rejects_busy_expired_wrong_creator_and_consumed_previews(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = _principal("rejection-admin", admin=True)
    other_admin = _principal("other-admin", admin=True)
    owner = _principal("rejection-owner")

    async with session_factory() as session:
        busy_id = await _create_conversation(session, owner)
        await _insert_turn_with_messages(session, busy_id, state="retrieving")
        busy_preview = await SqlAlchemyPurgeRepository(session).create_preview(
            _preview_command(admin, PurgeSelector(conversation_ids=(busy_id,)))
        )
        expired_id = await _create_conversation(session, owner)
        expired_preview = await SqlAlchemyPurgeRepository(
            session,
            preview_ttl_seconds=1,
        ).create_preview(_preview_command(admin, PurgeSelector(conversation_ids=(expired_id,))))
        await session.execute(
            update(PurgePreviewModel)
            .where(PurgePreviewModel.id == expired_preview.preview.id)
            .values(expires_at=func.clock_timestamp() - timedelta(seconds=1))
        )
        consumed_id = await _create_conversation(session, owner)
        consumed_preview = await SqlAlchemyPurgeRepository(session).create_preview(
            _preview_command(admin, PurgeSelector(conversation_ids=(consumed_id,)))
        )
        await session.commit()

    async with session_factory() as session:
        repository = SqlAlchemyPurgeRepository(session)
        with pytest.raises(ConversationBusyError):
            await repository.execute(_execute_command(admin, busy_preview.preview.id))
        await session.rollback()

    async with session_factory() as session:
        with pytest.raises(PurgePreviewExpiredError):
            await SqlAlchemyPurgeRepository(session).execute(
                _execute_command(admin, expired_preview.preview.id)
            )
        await session.rollback()

    async with session_factory() as session:
        with pytest.raises(PurgePreviewNotFoundError):
            await SqlAlchemyPurgeRepository(session).execute(
                _execute_command(other_admin, consumed_preview.preview.id)
            )
        await session.rollback()

    async with session_factory() as session:
        await SqlAlchemyPurgeRepository(session).execute(
            _execute_command(admin, consumed_preview.preview.id)
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(PurgePreviewConsumedError):
            await SqlAlchemyPurgeRepository(session).execute(
                _execute_command(admin, consumed_preview.preview.id)
            )
        await session.rollback()

    async with session_factory() as session:
        assert await session.get(ConversationModel, busy_id) is not None
        assert await session.get(ConversationModel, expired_id) is not None
        assert await session.get(ConversationModel, consumed_id) is None
        assert await _row_count(session, PurgeAuditModel) == 1


async def test_concurrent_execute_serializes_and_second_observes_consumed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    admin = _principal("race-admin", admin=True)
    owner = _principal("race-owner")
    async with session_factory() as session:
        conversation_id = await _create_conversation(session, owner)
        preview = await SqlAlchemyPurgeRepository(session).create_preview(
            _preview_command(admin, PurgeSelector(conversation_ids=(conversation_id,)))
        )
        await session.commit()

    first_executed = asyncio.Event()
    allow_first_commit = asyncio.Event()

    async def first_execute() -> object:
        async with session_factory() as session:
            result = await SqlAlchemyPurgeRepository(session).execute(
                _execute_command(admin, preview.preview.id)
            )
            first_executed.set()
            await allow_first_commit.wait()
            await session.commit()
            return result

    async def second_execute() -> BaseException | None:
        await first_executed.wait()
        async with session_factory() as session:
            try:
                await SqlAlchemyPurgeRepository(session).execute(
                    _execute_command(admin, preview.preview.id)
                )
            except BaseException as error:
                await session.rollback()
                return error
            await session.commit()
            return None

    first_task = asyncio.create_task(first_execute())
    second_task = asyncio.create_task(second_execute())
    await asyncio.wait_for(first_executed.wait(), timeout=LOCK_TIMEOUT_SECONDS)
    await asyncio.sleep(0.1)
    assert not second_task.done(), "second execute crossed the preview FOR UPDATE lock"
    allow_first_commit.set()
    first_result, second_error = await asyncio.wait_for(
        asyncio.gather(first_task, second_task),
        timeout=LOCK_TIMEOUT_SECONDS,
    )
    assert first_result.conversation_count == 1
    assert isinstance(second_error, PurgePreviewConsumedError)

    async with session_factory() as session:
        assert await _row_count(session, ConversationModel) == 0
        assert await _row_count(session, PurgeAuditModel) == 1
