"""Phase 2 Domain entities、enums 與不可變 invariants 的 unit tests。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from chat_service.domain.entities import (
    MAX_RETRY_AFTER_SECONDS,
    AllowedEvidence,
    AnswerPart,
    ChatTurn,
    Citation,
    Conversation,
    GenerationDiagnostics,
    Governance,
    Principal,
    PurgeSelector,
    RestrictedEvidence,
    RetrievalDiagnostics,
    TurnFailure,
)
from chat_service.domain.enums import (
    AnswerStatus,
    ChatOutcome,
    Confidentiality,
    ConversationStatus,
    InternalTurnState,
    MessageRole,
    PublicTurnState,
    RetrievalStatus,
    project_public_turn_state,
)
from chat_service.domain.exceptions import (
    ArchivedConversationError,
    ConflictError,
    ConversationBusyError,
    DomainValidationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    OwnershipError,
    PurgeError,
    PurgePreviewConsumedError,
    PurgePreviewExpiredError,
    PurgePreviewNotFoundError,
    PurgePreviewStaleError,
    PurgeSelectionTooLargeError,
)

UTC_NOW = datetime(2026, 8, 15, 8, 0, tzinfo=UTC)


def principal(subject: str = "employee-001") -> Principal:
    return Principal(
        issuer="urn:helpdesk:development",
        subject=subject,
        group_ids=("all-employees",),
        roles=("employee",),
    )


def conversation(status: ConversationStatus = ConversationStatus.ACTIVE) -> Conversation:
    return Conversation(
        id=uuid4(),
        owner=principal(),
        status=status,
        created_at=UTC_NOW,
        updated_at=UTC_NOW,
        last_activity_at=UTC_NOW,
    )


def citation(citation_id: str = "C1") -> Citation:
    return Citation(
        citation_id=citation_id,
        chunk_id=UUID("00000000-0000-0000-0000-000000000001"),
        document_id=UUID("00000000-0000-0000-0000-000000000002"),
        version_id=UUID("00000000-0000-0000-0000-000000000003"),
        title="遠端工作規章",
        section_path="第三條",
        page_number=3,
    )


def turn() -> ChatTurn:
    return ChatTurn(
        id=uuid4(),
        conversation_id=uuid4(),
        ordinal=1,
        idempotency_key="00000000-0000-0000-0000-000000000001",
        request_fingerprint="fingerprint",
        original_question="員工每週可以遠端工作幾天？",
        processed_query="員工每週可以遠端工作幾天？",
        request_id="request-001",
    )


def test_enum_values_are_exact_contract_values() -> None:
    assert [item.value for item in ConversationStatus] == ["active", "archived"]
    assert [item.value for item in InternalTurnState] == [
        "accepted",
        "waiting_for_capacity",
        "retrieving",
        "generating",
        "completed",
        "failed",
        "failed_unknown",
    ]
    assert [item.value for item in PublicTurnState] == [
        "processing",
        "completed",
        "failed",
        "failed_unknown",
    ]
    assert [item.value for item in MessageRole] == ["user", "assistant"]
    assert [item.value for item in ChatOutcome] == [
        "answered",
        "unable_to_answer",
        "external_generation_blocked",
        "insufficient_evidence",
        "outbound_policy_blocked",
    ]
    assert [item.value for item in RetrievalStatus] == [
        "evidence_found",
        "insufficient_evidence",
        "external_generation_blocked",
    ]
    assert [item.value for item in AnswerStatus] == ["answered", "unable_to_answer"]
    assert [item.value for item in Confidentiality] == [
        "public",
        "internal",
        "confidential",
        "restricted",
    ]


def test_public_state_projection_hides_internal_processing_phases() -> None:
    for state in (
        InternalTurnState.ACCEPTED,
        InternalTurnState.WAITING_FOR_CAPACITY,
        InternalTurnState.RETRIEVING,
        InternalTurnState.GENERATING,
    ):
        assert project_public_turn_state(state) is PublicTurnState.PROCESSING
    assert project_public_turn_state(InternalTurnState.COMPLETED) is PublicTurnState.COMPLETED
    assert project_public_turn_state(InternalTurnState.FAILED) is PublicTurnState.FAILED
    assert (
        project_public_turn_state(InternalTurnState.FAILED_UNKNOWN)
        is PublicTurnState.FAILED_UNKNOWN
    )


def test_principal_ownership_requires_issuer_and_subject() -> None:
    owner = principal()
    assert owner.owns("urn:helpdesk:development", "employee-001")
    assert not owner.owns("different-issuer", "employee-001")
    assert not owner.owns("urn:helpdesk:development", "other-employee")
    with pytest.raises(DomainValidationError):
        Principal(issuer=" ", subject="employee-001")
    with pytest.raises(OwnershipError):
        conversation().ensure_owner(principal("other-employee"))


def test_conversation_allocates_monotonic_ordinals_and_row_versions() -> None:
    item = conversation()
    initial_version = item.row_version
    assert item.allocate_turn_ordinal() == 1
    assert item.next_turn_ordinal == 2
    assert item.allocate_message_ordinal() == 1
    assert item.next_message_ordinal == 2
    assert item.row_version == initial_version + 2


def test_combined_turn_and_user_message_allocation_increments_row_version_once() -> None:
    item = conversation()
    initial_version = item.row_version

    turn_ordinal, message_ordinal = item.allocate_turn_and_user_message_ordinals()

    assert (turn_ordinal, message_ordinal) == (1, 1)
    assert (item.next_turn_ordinal, item.next_message_ordinal) == (2, 2)
    assert item.row_version == initial_version + 1

    archived = conversation(ConversationStatus.ARCHIVED)
    with pytest.raises(ArchivedConversationError):
        archived.allocate_turn_and_user_message_ordinals()


def test_archived_conversation_rejects_new_turn() -> None:
    with pytest.raises(ArchivedConversationError):
        conversation(ConversationStatus.ARCHIVED).allocate_turn_ordinal()


def test_turn_transition_and_stale_recovery_are_bounded() -> None:
    item = turn()
    item.transition_to(InternalTurnState.WAITING_FOR_CAPACITY)
    item.transition_to(InternalTurnState.RETRIEVING)
    item.transition_to(InternalTurnState.GENERATING)
    item.transition_to(InternalTurnState.COMPLETED)
    assert item.public_state is PublicTurnState.COMPLETED
    with pytest.raises(InvalidStateTransitionError):
        item.transition_to(InternalTurnState.FAILED)

    recoverable = turn()
    recoverable.transition_to(InternalTurnState.WAITING_FOR_CAPACITY)
    recoverable.recover_as_unknown()
    assert recoverable.state is InternalTurnState.FAILED_UNKNOWN
    assert recoverable.public_state is PublicTurnState.FAILED_UNKNOWN
    assert recoverable.response_http_status == 503
    assert recoverable.safe_error_code == "chat_processing_interrupted"
    assert recoverable.error_retryable is False


def test_failed_turn_carries_only_typed_replay_safe_error() -> None:
    failure = TurnFailure(
        response_http_status=429,
        safe_error_code="chat_capacity_exceeded",
        safe_error_message="系統目前處理量已滿，請稍後再試。",
        error_retryable=True,
        retry_after_seconds=5,
    )

    accepted = turn()
    with pytest.raises(InvalidStateTransitionError):
        accepted.fail(failure)
    assert accepted.state is InternalTurnState.ACCEPTED
    assert accepted.failure is None

    item = turn()
    item.transition_to(InternalTurnState.WAITING_FOR_CAPACITY)
    item.fail(failure)

    assert item.state is InternalTurnState.FAILED
    assert item.failure is failure
    assert item.response_http_status == 429
    assert item.retry_after_seconds == 5
    with pytest.raises(InvalidStateTransitionError):
        item.transition_to(InternalTurnState.FAILED_UNKNOWN)
    with pytest.raises(DomainValidationError):
        TurnFailure(
            response_http_status=200,
            safe_error_code="unsafe_status",
            safe_error_message="safe",
            error_retryable=False,
        )
    with pytest.raises(DomainValidationError):
        TurnFailure(
            response_http_status=503,
            safe_error_code="not_retryable",
            safe_error_message="safe",
            error_retryable=False,
            retry_after_seconds=1,
        )
    maximum = TurnFailure(
        response_http_status=429,
        safe_error_code="chat_capacity_exceeded",
        safe_error_message="系統目前處理量已滿，請稍後再試。",
        error_retryable=True,
        retry_after_seconds=MAX_RETRY_AFTER_SECONDS,
    )
    assert maximum.retry_after_seconds == MAX_RETRY_AFTER_SECONDS
    with pytest.raises(DomainValidationError):
        TurnFailure(
            response_http_status=429,
            safe_error_code="chat_capacity_exceeded",
            safe_error_message="系統目前處理量已滿，請稍後再試。",
            error_retryable=True,
            retry_after_seconds=MAX_RETRY_AFTER_SECONDS + 1,
        )


def test_failed_turn_mapping_requires_failure_and_diagnostics_are_content_free() -> None:
    with pytest.raises(DomainValidationError):
        ChatTurn(
            id=uuid4(),
            conversation_id=uuid4(),
            ordinal=1,
            idempotency_key="00000000-0000-0000-0000-000000000001",
            request_fingerprint="fingerprint",
            original_question="question",
            processed_query="question",
            request_id="request-001",
            state=InternalTurnState.FAILED,
        )

    retrieval = RetrievalDiagnostics(
        candidate_count=8,
        selected_count=2,
        model_id="embedding-model",
        model_revision="revision-1",
    )
    generation = GenerationDiagnostics(
        provider="fake",
        model="model",
        prompt_version="v1",
        finish_reason="stop",
        prompt_tokens=10,
        completion_tokens=5,
    )
    item = turn()
    item.retrieval_diagnostics = retrieval
    item.generation_diagnostics = generation
    assert item.retrieval_diagnostics.selected_count == 2
    assert item.generation_diagnostics.prompt_tokens == 10
    with pytest.raises(DomainValidationError):
        RetrievalDiagnostics(
            candidate_count=1,
            selected_count=2,
            model_id="model",
            model_revision="revision",
        )


def test_answer_part_and_citation_invariants() -> None:
    item = AnswerPart(text="答案", citation_ids=("C1",))
    assert item.citation_ids == ("C1",)
    with pytest.raises(DomainValidationError):
        AnswerPart(text="答案", citation_ids=())
    with pytest.raises(DomainValidationError):
        Citation(
            citation_id="C1",
            chunk_id=uuid4(),
            document_id=uuid4(),
            version_id=uuid4(),
            title="文件",
            page_number=0,
        )


def test_allowed_evidence_requires_external_llm_permission_and_restricted_has_no_content() -> None:
    safe_citation = citation()
    governance = Governance(
        confidentiality=Confidentiality.INTERNAL,
        language="zh-TW",
        external_llm_allowed=True,
    )
    allowed = AllowedEvidence(
        rank=1,
        chunk_id=safe_citation.chunk_id,
        document_id=safe_citation.document_id,
        version_id=safe_citation.version_id,
        score=0.9,
        content="員工每週可以遠端工作兩天。",
        document_type="policy",
        token_count=10,
        citation=safe_citation,
        governance=governance,
    )
    assert allowed.content
    restricted = RestrictedEvidence(
        rank=1,
        chunk_id=safe_citation.chunk_id,
        document_id=safe_citation.document_id,
        version_id=safe_citation.version_id,
        citation=safe_citation,
    )
    assert not hasattr(restricted, "content")
    with pytest.raises(DomainValidationError):
        AllowedEvidence(
            rank=1,
            chunk_id=safe_citation.chunk_id,
            document_id=safe_citation.document_id,
            version_id=safe_citation.version_id,
            score=0.9,
            content="不可外送",
            document_type="policy",
            token_count=10,
            citation=safe_citation,
            governance=Governance(
                confidentiality=Confidentiality.RESTRICTED,
                language="zh-TW",
                external_llm_allowed=False,
            ),
        )


def test_purge_selector_requires_bounded_explicit_conditions() -> None:
    with pytest.raises(DomainValidationError):
        PurgeSelector()
    with pytest.raises(DomainValidationError):
        PurgeSelector(owner_issuer="only-issuer")
    with pytest.raises(DomainValidationError):
        PurgeSelector(conversation_ids=tuple(uuid4() for _ in range(101)))
    selector = PurgeSelector(owner_issuer="urn:helpdesk:development", owner_subject="employee-001")
    assert selector.owner_subject == "employee-001"


def test_idempotency_and_purge_failures_have_typed_framework_free_errors() -> None:
    assert issubclass(IdempotencyConflictError, ConflictError)
    assert issubclass(PurgeSelectionTooLargeError, DomainValidationError)
    assert issubclass(PurgePreviewNotFoundError, PurgeError)
    assert issubclass(PurgePreviewExpiredError, PurgeError)
    assert issubclass(PurgePreviewConsumedError, (ConflictError, PurgeError))
    assert issubclass(PurgePreviewStaleError, (ConflictError, PurgeError))
    assert issubclass(ConversationBusyError, (ConflictError, PurgeError))
