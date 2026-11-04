"""Chat Service Phase 5 的 framework-free use cases。

本 module 是 orchestration 的 application layer：它只依賴 Domain、DTO 與 Protocol ports，
不認識 FastAPI、SQLAlchemy、HTTPX 或任何下游 URL。每一個 ``async with`` UoW 都是刻意
短小的 transaction boundary；等待 capacity、Retrieval HTTP 與 Answer HTTP 都在 UoW 外執行，
因此 row lock 不會跨越 queue 或網路呼叫。SubmitTurn 以「先保存 accepted/user message、
再進入 processing」的兩階段流程處理 idempotency race，並把所有 terminal result 重新讀成
可供後續 Presentation layer 安全投影的 ``GetTurnResult``。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
from uuid import UUID, uuid4

from chat_service.application.dtos import (
    AnswerGenerationRequest,
    AnswerGenerationResult,
    ConversationSummary,
    CreateConversationCommand,
    ExecuteConversationPurgeCommand,
    GetConversationQuery,
    GetTurnQuery,
    GetTurnResult,
    ListConversationsQuery,
    ListConversationsResult,
    ListMessagesQuery,
    ListMessagesResult,
    PreviewConversationPurgeCommand,
    PurgePreviewResult,
    PurgeResult,
    RetrievalGatewayResult,
    RetrievalRequest,
    SubmitTurnCommand,
    UpdateConversationCommand,
)
from chat_service.application.ports import (
    AnswerGenerationGatewayPort,
    CapacityGatePort,
    RetrievalGatewayPort,
    UnitOfWorkPort,
)
from chat_service.application.query_processor import QueryProcessor
from chat_service.domain.entities import (
    MAX_RETRY_AFTER_SECONDS,
    AllowedEvidence,
    AnswerPart,
    ChatTurn,
    Citation,
    Conversation,
    GenerationDiagnostics,
    Message,
    Principal,
    RestrictedEvidence,
    RetrievalDiagnostics,
    TurnFailure,
)
from chat_service.domain.enums import (
    AnswerStatus,
    ChatOutcome,
    InternalTurnState,
    MessageRole,
    RetrievalStatus,
)
from chat_service.domain.exceptions import (
    AdminRequiredError,
    DependencyError,
    DomainInvariantError,
    DomainValidationError,
    IdempotencyConflictError,
    InvalidIdempotencyKeyError,
    InvalidStateTransitionError,
    OwnershipError,
)

UnitOfWorkFactory = Callable[[], UnitOfWorkPort]

UNABLE_TO_ANSWER_MESSAGE: Final[str] = "目前無法根據已取得的公司文件可靠回答此問題。"
EXTERNAL_GENERATION_BLOCKED_MESSAGE: Final[str] = (
    "已找到相關文件，但依資料外送政策，這些內容不能送至外部 AI 產生答案。"  # noqa: RUF001
    "請參考文件引用或聯絡文件負責單位。"
)
INSUFFICIENT_EVIDENCE_MESSAGE: Final[str] = (
    "目前找不到足夠的公司文件依據，無法可靠回答。請換個方式提問或聯絡 Help Desk。"  # noqa: RUF001
)
OUTBOUND_POLICY_BLOCKED_MESSAGE: Final[str] = (
    "此問題或相關內容依資料外送政策無法送至目前的 AI 服務，因此未產生答案。"  # noqa: RUF001
)
CAPACITY_EXCEEDED_CODE: Final[str] = "chat_capacity_exceeded"
CAPACITY_EXCEEDED_MESSAGE: Final[str] = "系統目前處理量已滿，請稍後再試。"  # noqa: RUF001
GENERIC_INTERRUPTED_CODE: Final[str] = "chat_processing_interrupted"
GENERIC_INTERRUPTED_MESSAGE: Final[str] = (
    "先前的處理程序已中斷，無法確認是否完成；請使用新的請求重新送出。"  # noqa: RUF001
)


class CreateConversation:
    """建立一個由 verified Principal 擁有的 conversation。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        command: CreateConversationCommand,
        principal: Principal,
    ) -> ConversationSummary:
        """只在 repository transaction 內建立並 commit；owner 不來自 command body。"""

        async with self._uow_factory() as uow:
            conversation = await uow.conversations.add(command, principal)
            await uow.commit()
        return _to_conversation_summary(conversation)


class ListConversations:
    """列出 owner 自己的 conversation，cursor/keyset 由 repository 執行。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, query: ListConversationsQuery) -> ListConversationsResult:
        async with self._uow_factory() as uow:
            return await uow.conversations.list_for_owner(query)


class GetConversation:
    """取得單一 owner-scoped conversation；不存在與非 owner 共用 OwnershipError。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, query: GetConversationQuery) -> ConversationSummary:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.get_for_owner(query)
        if conversation is None:
            raise OwnershipError("conversation is unavailable to this principal")
        return _to_conversation_summary(conversation)


class UpdateConversation:
    """在 repository 的 owner row lock 內更新 title/status。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, command: UpdateConversationCommand) -> ConversationSummary:
        async with self._uow_factory() as uow:
            conversation = await uow.conversations.update(command)
            await uow.commit()
        return _to_conversation_summary(conversation)


class GetTurn:
    """取得 owner-scoped turn，包含已保存的 replay-safe error。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, query: GetTurnQuery) -> GetTurnResult:
        async with self._uow_factory() as uow:
            result = await uow.turns.get_for_owner(query)
        if result is None:
            raise OwnershipError("turn is unavailable to this principal")
        return result


class ListMessages:
    """列出 owner conversation 的安全 messages 與 keyset cursor。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, query: ListMessagesQuery) -> ListMessagesResult:
        async with self._uow_factory() as uow:
            return await uow.turns.list_messages(query)


class PreviewConversationPurge:
    """建立 admin hard-purge preview；selector 與 snapshot 由 persistence adapter 驗證。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        admin_role: str = "helpdesk-admin",
    ) -> None:
        self._uow_factory = uow_factory
        self._admin_role = admin_role

    async def execute(self, command: PreviewConversationPurgeCommand) -> PurgePreviewResult:
        """先檢查固定 admin role，再在單一 UoW 中建立 preview 並明確 commit。"""

        _require_admin(command.principal, self._admin_role)
        async with self._uow_factory() as uow:
            result = await uow.purges.create_preview(command)
            await uow.commit()
        return result


class ExecuteConversationPurge:
    """執行 preview-first hard purge；repository 負責 row lock/TOCTOU/cascade。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        admin_role: str = "helpdesk-admin",
    ) -> None:
        self._uow_factory = uow_factory
        self._admin_role = admin_role

    async def execute(self, command: ExecuteConversationPurgeCommand) -> PurgeResult:
        """先檢查固定 admin role，再以同一 transaction 執行 purge 並 commit。"""

        _require_admin(command.principal, self._admin_role)
        async with self._uow_factory() as uow:
            result = await uow.purges.execute(command)
            await uow.commit()
        return result


class SubmitTurn:
    """執行單輪 RAG orchestration、idempotency replay 與 bounded capacity。

    新 turn 先在獨立 transaction 保存 ``accepted`` 與 user message，接著每次 state transition
    使用另一個短 transaction。這個切分是必要的：capacity queue、Retrieval 與 Answer 都可能
    等待很久，不能持有 conversation/turn row lock。Answer POST 沒有 idempotency contract，
    所以任何無法確認 provider 結果的 timeout/例外都保存為 ``failed_unknown``，絕不自動重送。
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        retrieval_gateway: RetrievalGatewayPort,
        answer_gateway: AnswerGenerationGatewayPort,
        capacity_gate: CapacityGatePort,
        *,
        query_processor: QueryProcessor | None = None,
        queue_wait_timeout_seconds: float = 120.0,
        retrieval_limit: int = 8,
    ) -> None:
        self._uow_factory = uow_factory
        self._retrieval_gateway = retrieval_gateway
        self._answer_gateway = answer_gateway
        self._capacity_gate = capacity_gate
        self._query_processor = query_processor or QueryProcessor()
        if (
            isinstance(queue_wait_timeout_seconds, bool)
            or not isinstance(queue_wait_timeout_seconds, (int, float))
            or not math.isfinite(float(queue_wait_timeout_seconds))
            or queue_wait_timeout_seconds <= 0
            or queue_wait_timeout_seconds > MAX_RETRY_AFTER_SECONDS
        ):
            raise ValueError("queue_wait_timeout_seconds must be positive, finite, and bounded")
        self._queue_wait_timeout_seconds = float(queue_wait_timeout_seconds)
        if (
            isinstance(retrieval_limit, bool)
            or not isinstance(retrieval_limit, int)
            or not 1 <= retrieval_limit <= 20
        ):
            raise ValueError("retrieval_limit must be between 1 and 20")
        self._retrieval_limit = retrieval_limit

    async def execute(self, command: SubmitTurnCommand) -> GetTurnResult:
        """依規格完成新 turn 或回傳既有 turn 的安全 replay。"""

        processed = self._query_processor.process(command.question, command.language)
        canonical_key = canonicalize_idempotency_key(command.idempotency_key)
        fingerprint = request_fingerprint(processed.processed_query, processed.language)

        created = await self._create_or_replay(
            command,
            canonical_key=canonical_key,
            fingerprint=fingerprint,
            original_question=processed.original_question,
            processed_query=processed.processed_query,
            language=processed.language,
        )
        if created.replay is not None:
            return created.replay

        turn_id = created.turn_id
        if turn_id is None:
            raise RuntimeError("new turn creation returned neither turn ID nor replay")
        return await self._run_new_turn(
            command,
            turn_id=turn_id,
            processed_query=processed.processed_query,
            language=processed.language,
        )

    async def _create_or_replay(
        self,
        command: SubmitTurnCommand,
        *,
        canonical_key: str,
        fingerprint: str,
        original_question: str,
        processed_query: str,
        language: str | None,
    ) -> _CreatedTurn:
        """先查 replay，再以 conversation lock/unique constraint 建立 accepted turn。"""

        try:
            async with self._uow_factory() as uow:
                existing = await uow.turns.find_by_idempotency(
                    command.principal,
                    command.conversation_id,
                    canonical_key,
                )
                if existing is not None:
                    _ensure_same_fingerprint(existing, fingerprint)
                    return _CreatedTurn(turn_id=None, replay=existing)

                allocation = await uow.conversations.allocate_turn_and_user_message_ordinals(
                    GetConversationQuery(
                        principal=command.principal,
                        conversation_id=command.conversation_id,
                    )
                )
                if allocation is None:
                    raise OwnershipError("conversation is unavailable to this principal")

                turn_id = uuid4()
                turn = ChatTurn(
                    id=turn_id,
                    conversation_id=command.conversation_id,
                    ordinal=allocation.turn_ordinal,
                    idempotency_key=canonical_key,
                    request_fingerprint=fingerprint,
                    original_question=original_question,
                    processed_query=processed_query,
                    request_id=command.request_id,
                    language=language,
                )
                user_message = Message(
                    id=uuid4(),
                    conversation_id=command.conversation_id,
                    turn_id=turn_id,
                    ordinal=allocation.user_message_ordinal,
                    role=MessageRole.USER,
                    content=original_question,
                )
                await uow.turns.add(turn, user_message, command.principal)
                await uow.commit()
                return _CreatedTurn(turn_id=turn_id, replay=None)
        except IdempotencyConflictError:
            # Flush 的 unique conflict 會讓原 transaction 進入 failed 狀態；先離開 UoW
            # 讓它 rollback，再以全新 session 查詢 winner，絕不沿用 failed transaction。
            async with self._uow_factory() as uow:
                existing = await uow.turns.find_by_idempotency(
                    command.principal,
                    command.conversation_id,
                    canonical_key,
                )
            if existing is None:
                raise IdempotencyConflictError(
                    "idempotency race winner could not be loaded"
                ) from None
            _ensure_same_fingerprint(existing, fingerprint)
            return _CreatedTurn(turn_id=None, replay=existing)

    async def _run_new_turn(
        self,
        command: SubmitTurnCommand,
        *,
        turn_id: UUID,
        processed_query: str,
        language: str | None,
    ) -> GetTurnResult:
        """執行等待 capacity、Retrieval 與必要的 Answer；每一步都 fail closed。"""

        waiting_result = await self._transition_to_waiting(command, turn_id)
        if waiting_result is not None:
            return waiting_result

        lease = await self._capacity_gate.acquire(self._queue_wait_timeout_seconds)
        if lease is None:
            return await self._persist_failure(
                command,
                turn_id,
                expected_state=InternalTurnState.WAITING_FOR_CAPACITY,
                failure=_capacity_failure(self._queue_wait_timeout_seconds),
            )

        try:
            retrieving_result = await self._transition_to_retrieving(command, turn_id)
            if retrieving_result is not None:
                return retrieving_result

            try:
                retrieval = await self._retrieval_gateway.retrieve(
                    RetrievalRequest(
                        query=processed_query,
                        limit=self._retrieval_limit,
                        language=language,
                        principal=command.principal,
                        request_id=command.request_id,
                    )
                )
                _validate_retrieval_result(retrieval)
            except DependencyError as error:
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.RETRIEVING,
                    failure=_failure_from_dependency(error, dependency="retrieval"),
                )
            except (DomainValidationError, DomainInvariantError, TypeError, ValueError):
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.RETRIEVING,
                    failure=_retrieval_contract_failure(),
                )
            except Exception:
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.RETRIEVING,
                    failure=_retrieval_service_failure(),
                )

            if retrieval.status is not RetrievalStatus.EVIDENCE_FOUND:
                return await self._complete_retrieval_business_result(
                    command,
                    turn_id,
                    retrieval,
                )

            generating_result = await self._transition_to_generating(
                command,
                turn_id,
                retrieval,
            )
            if generating_result is not None:
                return generating_result

            try:
                answer = await self._answer_gateway.generate(
                    AnswerGenerationRequest(
                        question=processed_query,
                        language=language,
                        evidence=retrieval.llm_context,
                        context_attestation=_required_attestation(retrieval),
                        request_id=command.request_id,
                    )
                )
                _validate_answer_result(answer)
            except DependencyError as error:
                if error.code == "outbound_policy_blocked":
                    return await self._complete_terminal(
                        command,
                        turn_id,
                        expected_state=InternalTurnState.GENERATING,
                        outcome=ChatOutcome.OUTBOUND_POLICY_BLOCKED,
                        retrieval=retrieval,
                        answer=None,
                        content=OUTBOUND_POLICY_BLOCKED_MESSAGE,
                    )
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.GENERATING,
                    failure=_failure_from_dependency(error, dependency="answer"),
                    result_unknown=error.code == "provider_timeout",
                )
            except (DomainValidationError, DomainInvariantError, TypeError, ValueError):
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.GENERATING,
                    failure=_answer_contract_failure(),
                )
            except Exception:
                return await self._persist_failure(
                    command,
                    turn_id,
                    expected_state=InternalTurnState.GENERATING,
                    failure=_interrupted_failure(),
                    result_unknown=True,
                )

            return await self._complete_answer_result(command, turn_id, retrieval, answer)
        finally:
            await self._capacity_gate.release(lease)

    async def _transition_to_waiting(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
    ) -> GetTurnResult | None:
        return await self._transition(
            command,
            turn_id,
            expected_state=InternalTurnState.ACCEPTED,
            target_state=InternalTurnState.WAITING_FOR_CAPACITY,
        )

    async def _transition_to_retrieving(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
    ) -> GetTurnResult | None:
        return await self._transition(
            command,
            turn_id,
            expected_state=InternalTurnState.WAITING_FOR_CAPACITY,
            target_state=InternalTurnState.RETRIEVING,
        )

    async def _transition_to_generating(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        retrieval: RetrievalGatewayResult,
    ) -> GetTurnResult | None:
        diagnostics = RetrievalDiagnostics(
            candidate_count=retrieval.candidate_count,
            selected_count=retrieval.selected_count,
            model_id=retrieval.model_id,
            model_revision=retrieval.model_revision,
        )
        return await self._transition(
            command,
            turn_id,
            expected_state=InternalTurnState.RETRIEVING,
            target_state=InternalTurnState.GENERATING,
            retrieval_status=retrieval.status,
            retrieval_diagnostics=diagnostics,
        )

    async def _transition(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        *,
        expected_state: InternalTurnState,
        target_state: InternalTurnState,
        retrieval_status: RetrievalStatus | None = None,
        retrieval_diagnostics: RetrievalDiagnostics | None = None,
    ) -> GetTurnResult | None:
        async with self._uow_factory() as uow:
            result = await uow.turns.get_for_owner_for_update(
                GetTurnQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                    turn_id=turn_id,
                )
            )
            if result is None:
                raise OwnershipError("turn is unavailable to this principal")
            if result.result.turn.state is not expected_state:
                return result
            turn = result.result.turn
            if retrieval_status is not None:
                turn.retrieval_status = retrieval_status
            if retrieval_diagnostics is not None:
                turn.retrieval_diagnostics = retrieval_diagnostics
            turn.transition_to(target_state)
            await uow.turns.update(turn, command.principal)
            await uow.commit()
        return None

    async def _persist_failure(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        *,
        expected_state: InternalTurnState,
        failure: TurnFailure,
        result_unknown: bool = False,
    ) -> GetTurnResult:
        """把安全 failure 寫成 terminal state；若競爭者已 terminal，保留先完成者結果。"""

        async with self._uow_factory() as uow:
            result = await uow.turns.get_for_owner_for_update(
                GetTurnQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                    turn_id=turn_id,
                )
            )
            if result is None:
                raise OwnershipError("turn is unavailable to this principal")
            if result.result.turn.state is not expected_state:
                if result.result.turn.state in {
                    InternalTurnState.COMPLETED,
                    InternalTurnState.FAILED,
                    InternalTurnState.FAILED_UNKNOWN,
                }:
                    return result
                raise InvalidStateTransitionError(
                    "turn is no longer in the expected processing state"
                )
            turn = result.result.turn
            turn.fail(failure, result_unknown=result_unknown)
            await uow.turns.update(turn, command.principal)
            await uow.conversations.record_terminal_activity(
                GetConversationQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                )
            )
            await uow.commit()
        return await self._reload_turn(command, turn_id)

    async def _complete_retrieval_business_result(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        retrieval: RetrievalGatewayResult,
    ) -> GetTurnResult:
        if retrieval.status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED:
            citations = _blocked_citations(retrieval.restricted_evidence)
            outcome = ChatOutcome.EXTERNAL_GENERATION_BLOCKED
            content = EXTERNAL_GENERATION_BLOCKED_MESSAGE
        elif retrieval.status is RetrievalStatus.INSUFFICIENT_EVIDENCE:
            citations = ()
            outcome = ChatOutcome.INSUFFICIENT_EVIDENCE
            content = INSUFFICIENT_EVIDENCE_MESSAGE
        else:
            raise DomainInvariantError("unexpected retrieval business status")
        return await self._complete_terminal(
            command,
            turn_id,
            expected_state=InternalTurnState.RETRIEVING,
            outcome=outcome,
            retrieval=retrieval,
            answer=None,
            content=content,
            citations=citations,
        )

    async def _complete_answer_result(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        retrieval: RetrievalGatewayResult,
        answer: AnswerGenerationResult,
    ) -> GetTurnResult:
        if answer.status is AnswerStatus.ANSWERED:
            content = "\n\n".join(part.text for part in answer.answer_parts)
            outcome = ChatOutcome.ANSWERED
        elif answer.status is AnswerStatus.UNABLE_TO_ANSWER:
            content = UNABLE_TO_ANSWER_MESSAGE
            outcome = ChatOutcome.UNABLE_TO_ANSWER
        else:
            raise DomainInvariantError("unexpected answer business status")
        return await self._complete_terminal(
            command,
            turn_id,
            expected_state=InternalTurnState.GENERATING,
            outcome=outcome,
            retrieval=retrieval,
            answer=answer,
            content=content,
            citations=answer.citations,
        )

    async def _complete_terminal(
        self,
        command: SubmitTurnCommand,
        turn_id: UUID,
        *,
        expected_state: InternalTurnState,
        outcome: ChatOutcome,
        retrieval: RetrievalGatewayResult,
        answer: AnswerGenerationResult | None,
        content: str,
        citations: tuple[Citation, ...] = (),
    ) -> GetTurnResult:
        """同一 transaction 寫 terminal turn、assistant message/citations 與 activity。"""

        retrieval_diagnostics = RetrievalDiagnostics(
            candidate_count=retrieval.candidate_count,
            selected_count=retrieval.selected_count,
            model_id=retrieval.model_id,
            model_revision=retrieval.model_revision,
        )
        async with self._uow_factory() as uow:
            result = await uow.turns.get_for_owner_for_update(
                GetTurnQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                    turn_id=turn_id,
                )
            )
            if result is None:
                raise OwnershipError("turn is unavailable to this principal")
            if result.result.turn.state is not expected_state:
                if result.result.turn.state in {
                    InternalTurnState.COMPLETED,
                    InternalTurnState.FAILED,
                    InternalTurnState.FAILED_UNKNOWN,
                }:
                    return result
                raise InvalidStateTransitionError(
                    "turn is no longer in the expected processing state"
                )
            turn = result.result.turn
            turn.retrieval_status = retrieval.status
            turn.retrieval_diagnostics = retrieval_diagnostics
            if answer is not None:
                turn.answer_status = answer.status
                turn.generation_diagnostics = answer.diagnostics
            turn.transition_to(InternalTurnState.COMPLETED)
            turn.set_outcome(outcome)
            await uow.turns.update(turn, command.principal)

            allocation = await uow.conversations.allocate_message_ordinal(
                GetConversationQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                )
            )
            if allocation is None:
                raise OwnershipError("conversation is unavailable to this principal")
            assistant = Message(
                id=uuid4(),
                conversation_id=command.conversation_id,
                turn_id=turn_id,
                ordinal=allocation.message_ordinal,
                role=MessageRole.ASSISTANT,
                content=content,
                response_kind=outcome,
                answer_parts=()
                if answer is None or outcome is not ChatOutcome.ANSWERED
                else answer.answer_parts,
                citations=citations,
            )
            await uow.turns.add_assistant_message(assistant, command.principal)
            await uow.commit()
        return await self._reload_turn(command, turn_id)

    async def _reload_turn(self, command: SubmitTurnCommand, turn_id: UUID) -> GetTurnResult:
        async with self._uow_factory() as uow:
            result = await uow.turns.get_for_owner(
                GetTurnQuery(
                    principal=command.principal,
                    conversation_id=command.conversation_id,
                    turn_id=turn_id,
                )
            )
        if result is None:
            raise OwnershipError("turn is unavailable to this principal")
        return result


@dataclass(frozen=True, slots=True)
class _CreatedTurn:
    """新 turn ID 或既有 replay result 的 private discriminated result。"""

    turn_id: UUID | None
    replay: GetTurnResult | None


def canonicalize_idempotency_key(value: str) -> str:
    """把 raw header 的 UUID 表示立即 canonicalize 成 36 字元 lowercase UUID。"""

    if not isinstance(value, str) or len(value) > 64:
        raise InvalidIdempotencyKeyError("idempotency key must be a UUID")
    trimmed = value.strip()
    if not trimmed:
        raise InvalidIdempotencyKeyError("idempotency key must be a UUID")
    try:
        return str(UUID(trimmed))
    except (ValueError, AttributeError, TypeError):
        raise InvalidIdempotencyKeyError("idempotency key must be a UUID") from None


def request_fingerprint(processed_query: str, language: str | None) -> str:
    """依規格以 processed query/language 的 canonical JSON 計算 SHA-256。"""

    payload = json.dumps(
        {"language": language, "question": processed_query},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _to_conversation_summary(conversation: Conversation) -> ConversationSummary:
    """把 Domain aggregate 轉成 employee-safe summary，拒絕遺漏 DB timestamps。"""

    if (
        conversation.created_at is None
        or conversation.updated_at is None
        or conversation.last_activity_at is None
    ):
        raise DomainInvariantError("conversation timestamps are incomplete")
    return ConversationSummary(
        conversation_id=conversation.id,
        title=conversation.title,
        status=conversation.status,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        last_activity_at=conversation.last_activity_at,
    )


def _require_admin(principal: Principal, admin_role: str) -> None:
    """管理 use case 只接受 verified Principal 內的固定 admin role。"""

    if not isinstance(admin_role, str) or not admin_role.strip():
        raise ValueError("admin_role must be non-blank")
    if admin_role not in principal.roles:
        raise AdminRequiredError("administrator role is required")


def _ensure_same_fingerprint(result: GetTurnResult, fingerprint: str) -> None:
    """同 key 必須同 payload；不同 payload 不把原值放進 conflict message。"""

    if result.result.turn.request_fingerprint != fingerprint:
        raise IdempotencyConflictError("idempotency key was already used for another request")


def _required_attestation(result: RetrievalGatewayResult) -> str:
    """evidence_found 沒有 attestation 時 fail closed，不把 token 寫入錯誤。"""

    if result.context_attestation is None or not result.context_attestation.strip():
        raise DomainInvariantError("evidence_found requires a context attestation")
    return result.context_attestation


def _validate_retrieval_result(result: RetrievalGatewayResult) -> None:
    """在 application boundary 再驗一次 fake/adapter result 的安全與跨欄位 invariant。"""

    if not isinstance(result, RetrievalGatewayResult):
        raise DomainInvariantError("retrieval result has an invalid type")
    if any(not isinstance(item, AllowedEvidence) for item in result.llm_context):
        raise DomainInvariantError("retrieval allowed context contains an invalid item")
    if any(not isinstance(item, RestrictedEvidence) for item in result.restricted_evidence):
        raise DomainInvariantError("retrieval restricted context contains an invalid item")
    if result.selected_count != len(result.llm_context) + len(result.restricted_evidence):
        raise DomainInvariantError("retrieval selected count does not match evidence lists")
    if result.candidate_count < result.selected_count:
        raise DomainInvariantError("retrieval candidate count is smaller than selected count")
    allowed_ranks = tuple(item.rank for item in result.llm_context)
    restricted_ranks = tuple(item.rank for item in result.restricted_evidence)
    if allowed_ranks != tuple(sorted(allowed_ranks)):
        raise DomainInvariantError("retrieval allowed ranks are not ordered")
    if restricted_ranks != tuple(sorted(restricted_ranks)):
        raise DomainInvariantError("retrieval restricted ranks are not ordered")
    all_ranks = allowed_ranks + restricted_ranks
    if len(set(all_ranks)) != len(all_ranks) or tuple(sorted(all_ranks)) != tuple(
        range(1, result.selected_count + 1)
    ):
        raise DomainInvariantError("retrieval ranks are not a unique global sequence")
    if any(not item.governance.external_llm_allowed for item in result.llm_context):
        raise DomainInvariantError("retrieval allowed context contains restricted evidence")

    if result.status is RetrievalStatus.EVIDENCE_FOUND:
        if not result.llm_context or result.external_generation_blocked:
            raise DomainInvariantError("evidence_found has an invalid policy projection")
        _required_attestation(result)
    elif result.status is RetrievalStatus.EXTERNAL_GENERATION_BLOCKED:
        if (
            result.llm_context
            or not result.restricted_evidence
            or result.context_attestation is not None
            or not result.external_generation_blocked
        ):
            raise DomainInvariantError("external_generation_blocked has an invalid projection")
    elif result.status is RetrievalStatus.INSUFFICIENT_EVIDENCE:
        if (
            result.llm_context
            or result.restricted_evidence
            or result.context_attestation is not None
            or result.external_generation_blocked
        ):
            raise DomainInvariantError("insufficient_evidence has an invalid projection")
    else:
        raise DomainInvariantError("retrieval status is invalid")


def _validate_answer_result(result: AnswerGenerationResult) -> None:
    """驗證 fake answer port 也遵守 Answer cross-field invariants。"""

    if not isinstance(result, AnswerGenerationResult):
        raise DomainInvariantError("answer result has an invalid type")
    if not isinstance(result.diagnostics, GenerationDiagnostics):
        raise DomainInvariantError("answer diagnostics have an invalid type")
    if any(not isinstance(part, AnswerPart) for part in result.answer_parts):
        raise DomainInvariantError("answer parts contain an invalid item")
    if any(not isinstance(citation, Citation) for citation in result.citations):
        raise DomainInvariantError("answer citations contain an invalid item")
    citation_ids = tuple(citation.citation_id for citation in result.citations)
    if len(set(citation_ids)) != len(citation_ids):
        raise DomainInvariantError("answer citation IDs are not unique")
    referenced_ids = tuple(
        citation_id for part in result.answer_parts for citation_id in part.citation_ids
    )
    if any(citation_id not in set(citation_ids) for citation_id in referenced_ids):
        raise DomainInvariantError("answer references an unknown citation")
    if result.status is AnswerStatus.ANSWERED:
        if not result.answer_parts or not result.citations:
            raise DomainInvariantError("answered result is missing parts or citations")
        if set(referenced_ids) != set(citation_ids):
            raise DomainInvariantError("answered result contains an unused citation")
    elif result.status is AnswerStatus.UNABLE_TO_ANSWER:
        if result.answer_parts or result.citations:
            raise DomainInvariantError("unable_to_answer result contains answer payload")
    else:
        raise DomainInvariantError("answer status is invalid")


def _blocked_citations(evidence: tuple[RestrictedEvidence, ...]) -> tuple[Citation, ...]:
    """以全域 rank 排序 restricted safe citation，重新指定 public C1/C2 IDs。"""

    return tuple(
        Citation(
            citation_id=f"C{index}",
            chunk_id=item.chunk_id,
            document_id=item.document_id,
            version_id=item.version_id,
            title=item.citation.title,
            section_path=item.citation.section_path,
            page_number=item.citation.page_number,
        )
        for index, item in enumerate(sorted(evidence, key=lambda item: item.rank), start=1)
    )


def _capacity_failure(wait_timeout_seconds: float) -> TurnFailure:
    """Capacity reject 使用安全固定訊息，Retry-After 取 bounded queue timeout。"""

    try:
        bounded_wait_timeout = float(wait_timeout_seconds)
    except (TypeError, ValueError, OverflowError):
        bounded_wait_timeout = 0.0
    if not math.isfinite(bounded_wait_timeout) or bounded_wait_timeout <= 0:
        retry_after_seconds = 1
    else:
        retry_after_seconds = min(
            MAX_RETRY_AFTER_SECONDS,
            max(1, math.ceil(bounded_wait_timeout)),
        )
    return TurnFailure(
        response_http_status=429,
        safe_error_code=CAPACITY_EXCEEDED_CODE,
        safe_error_message=CAPACITY_EXCEEDED_MESSAGE,
        error_retryable=True,
        retry_after_seconds=retry_after_seconds,
    )


def _retrieval_contract_failure() -> TurnFailure:
    return TurnFailure(
        response_http_status=502,
        safe_error_code="retrieval_contract_error",
        safe_error_message="檢索服務回傳的資料格式無法驗證。",
        error_retryable=False,
    )


def _retrieval_service_failure() -> TurnFailure:
    return TurnFailure(
        response_http_status=502,
        safe_error_code="retrieval_service_error",
        safe_error_message="檢索服務目前無法處理請求。",
        error_retryable=False,
    )


def _answer_contract_failure() -> TurnFailure:
    return TurnFailure(
        response_http_status=502,
        safe_error_code="answer_contract_error",
        safe_error_message="回答服務回傳的資料格式無法驗證。",
        error_retryable=False,
    )


def _interrupted_failure() -> TurnFailure:
    return TurnFailure(
        response_http_status=503,
        safe_error_code=GENERIC_INTERRUPTED_CODE,
        safe_error_message=GENERIC_INTERRUPTED_MESSAGE,
        error_retryable=False,
    )


def _failure_from_dependency(error: DependencyError, *, dependency: str) -> TurnFailure:
    """將 adapter safe code 映射為永久保存的 terminal failure，不採用 exception raw text。"""

    if dependency == "retrieval":
        mapping: dict[str, tuple[int, str, bool]] = {
            "retrieval_auth_misconfigured": (502, "檢索服務驗證設定錯誤。", False),
            "retrieval_identity_rejected": (502, "檢索服務拒絕已驗證的使用者身分。", False),
            "retrieval_contract_error": (502, "檢索服務回傳的資料格式無法驗證。", False),
            "retrieval_service_error": (502, "檢索服務目前無法處理請求。", False),
            "retrieval_unavailable": (503, "檢索服務目前無法使用。", True),
            "retrieval_timeout": (504, "檢索服務處理逾時。", True),
        }
    else:
        mapping = {
            "answer_auth_misconfigured": (502, "回答服務驗證設定錯誤。", False),
            "answer_context_rejected": (502, "回答服務拒絕檢索內容驗證。", False),
            "answer_contract_error": (502, "回答服務回傳的資料格式無法驗證。", False),
            "provider_contract_error": (502, "目前的回答模型回傳格式無法驗證。", False),
            "answer_service_error": (502, "回答服務目前無法處理請求。", False),
            "provider_unavailable": (503, "目前的回答模型暫時無法使用。", True),
            "provider_timeout": (504, "目前的回答模型處理逾時。", False),
            "generation_busy": (503, "回答服務目前處理量已滿，請稍後再試。", True),  # noqa: RUF001
        }
    raw_code = error.code if isinstance(error.code, str) else ""
    default_code = (
        "retrieval_service_error" if dependency == "retrieval" else "answer_service_error"
    )
    code = raw_code if raw_code in mapping else default_code
    status, message, retryable = mapping.get(
        code,
        (503 if dependency == "answer" else 502, "下游服務目前無法完成請求。", False),
    )
    retry_after = _safe_retry_after_seconds(error.retry_after_seconds) if retryable else None
    return TurnFailure(
        response_http_status=status,
        safe_error_code=code,
        safe_error_message=message,
        error_retryable=retryable,
        retry_after_seconds=retry_after,
    )


def _safe_retry_after_seconds(value: object) -> int | None:
    """限制下游提供的 Retry-After，避免 malformed adapter/fake 造成不安全回應。"""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_RETRY_AFTER_SECONDS
    ):
        return None
    return value
