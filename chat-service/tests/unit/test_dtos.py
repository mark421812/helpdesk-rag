"""Phase 2 application DTO 與 strict cursor payload tests。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from chat_service.application.dtos import (
    ConversationCursorPayload,
    GetTurnResult,
    ListConversationsQuery,
    MessageCursorPayload,
    MessageOrdinalAllocation,
    PurgePreviewResult,
    SafeError,
    TurnAndUserMessageOrdinalAllocation,
    TurnResult,
    UpdateConversationCommand,
)
from chat_service.domain.entities import (
    ChatTurn,
    Message,
    Principal,
    PurgePreview,
    PurgeSelector,
    TurnFailure,
)
from chat_service.domain.enums import ConversationStatus, InternalTurnState, MessageRole
from chat_service.domain.exceptions import DomainValidationError, InvalidCursorError

UTC_NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


def owner() -> Principal:
    return Principal(issuer="urn:helpdesk:development", subject="employee-001")


def test_conversation_cursor_requires_version_aware_time_and_uuid() -> None:
    cursor = ConversationCursorPayload(
        version=1,
        last_activity_at=UTC_NOW,
        conversation_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    assert cursor.version == 1
    with pytest.raises(InvalidCursorError):
        ConversationCursorPayload(version=2, last_activity_at=UTC_NOW, conversation_id=uuid4())
    with pytest.raises(InvalidCursorError):
        ConversationCursorPayload(
            version=1,
            last_activity_at=datetime(2026, 8, 15, 8, 0),
            conversation_id=uuid4(),
        )


def test_message_cursor_requires_version_and_positive_ordinal() -> None:
    assert MessageCursorPayload(version=1, ordinal=1).ordinal == 1
    with pytest.raises(InvalidCursorError):
        MessageCursorPayload(version=1, ordinal=0)
    with pytest.raises(InvalidCursorError):
        MessageCursorPayload(version=2, ordinal=1)


def test_query_dto_bounds_and_patch_unset_semantics() -> None:
    query = ListConversationsQuery(principal=owner(), limit=20)
    assert query.cursor is None
    with pytest.raises(DomainValidationError):
        ListConversationsQuery(principal=owner(), limit=0)
    with pytest.raises(DomainValidationError):
        UpdateConversationCommand(
            principal=owner(),
            conversation_id=uuid4(),
        )
    clear_title = UpdateConversationCommand(
        principal=owner(),
        conversation_id=uuid4(),
        title=None,
    )
    assert clear_title.title is None
    archive = UpdateConversationCommand(
        principal=owner(),
        conversation_id=uuid4(),
        status=ConversationStatus.ARCHIVED,
    )
    assert archive.status is ConversationStatus.ARCHIVED


def test_safe_error_never_needs_downstream_payload() -> None:
    error = SafeError(
        code="retrieval_unavailable",
        message="Retrieval service is unavailable",
        retryable=True,
        retry_after_seconds=3,
    )
    assert error.retry_after_seconds == 3

    failure = TurnFailure(
        response_http_status=503,
        safe_error_code="retrieval_unavailable",
        safe_error_message="Retrieval service is unavailable",
        error_retryable=True,
        retry_after_seconds=3,
    )
    assert SafeError.from_turn_failure(failure) == error


def test_failed_turn_result_carries_exact_replay_safe_error() -> None:
    failure = TurnFailure(
        response_http_status=503,
        safe_error_code="chat_processing_interrupted",
        safe_error_message="先前的處理程序已中斷。",
        error_retryable=False,
    )
    conversation_id = uuid4()
    turn_id = uuid4()
    failed_turn = ChatTurn(
        id=turn_id,
        conversation_id=conversation_id,
        ordinal=1,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        request_fingerprint="f" * 64,
        original_question="question",
        processed_query="question",
        request_id="request-001",
        state=InternalTurnState.FAILED_UNKNOWN,
        failure=failure,
    )
    user_message = Message(
        id=uuid4(),
        conversation_id=conversation_id,
        turn_id=turn_id,
        ordinal=1,
        role=MessageRole.USER,
        content="question",
    )
    stored = TurnResult(
        turn=failed_turn,
        user_message=user_message,
        assistant_message=None,
    )
    replay = GetTurnResult(result=stored, error=SafeError.from_turn_failure(failure))
    assert replay.error is not None
    assert replay.error.code == "chat_processing_interrupted"

    with pytest.raises(DomainValidationError):
        GetTurnResult(result=stored)
    with pytest.raises(DomainValidationError):
        GetTurnResult(
            result=stored,
            error=SafeError(code="different", message="safe", retryable=False),
        )


def test_transactional_ordinal_allocation_dtos_reject_invalid_values() -> None:
    combined = TurnAndUserMessageOrdinalAllocation(
        turn_ordinal=1,
        user_message_ordinal=2,
        row_version=3,
    )
    assert combined.row_version == 3
    assert MessageOrdinalAllocation(message_ordinal=4, row_version=5).message_ordinal == 4
    with pytest.raises(DomainValidationError):
        TurnAndUserMessageOrdinalAllocation(
            turn_ordinal=0,
            user_message_ordinal=1,
            row_version=1,
        )
    with pytest.raises(DomainValidationError):
        MessageOrdinalAllocation(message_ordinal=True, row_version=1)


def test_purge_preview_result_requires_non_negative_busy_count() -> None:
    preview = PurgePreview(
        id=uuid4(),
        selector=PurgeSelector(conversation_ids=(uuid4(),)),
        selected=(),
        selector_digest="a" * 64,
        conversation_count=0,
        message_count=0,
        creator=owner(),
        expires_at=datetime(2026, 8, 15, 8, 10, tzinfo=UTC),
        created_at=UTC_NOW,
    )
    assert PurgePreviewResult(preview=preview, busy_count=0).busy_count == 0
    with pytest.raises(DomainValidationError):
        PurgePreviewResult(preview=preview, busy_count=-1)
