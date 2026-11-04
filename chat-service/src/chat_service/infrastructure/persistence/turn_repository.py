"""Turn 與 message aggregate 的 SQLAlchemy persistence adapter。

所有 child query 都在同一個 SQL join conversation，並同時帶 conversation ID、owner issuer
與 subject，避免先查 child 再於 Python 判斷 ownership。Mutation 與明確的 for-update query
則額外先鎖 parent conversation；這個固定順序同時防止 admin purge 的 TOCTOU，也讓並行狀態
轉移與 message ordinal allocation 遵守同一個鎖階層。Repository 只 flush、不 commit；
transaction boundary 仍由 Unit of Work 擁有。

JSONB mapping 採明確 allowlist，不使用通用 dataclass 序列化，因此 evidence、路徑、URL、
attestation 或 secret 沒有機會因物件日後增加欄位而被順帶持久化。
"""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from chat_service.application.dtos import (
    GetTurnQuery,
    GetTurnResult,
    ListMessagesQuery,
    ListMessagesResult,
    MessageCursorPayload,
    SafeError,
    TurnResult,
)
from chat_service.domain.entities import (
    PROCESSING_INTERRUPTED_ERROR_CODE,
    PROCESSING_INTERRUPTED_ERROR_MESSAGE,
    AnswerPart,
    ChatTurn,
    Citation,
    GenerationDiagnostics,
    Message,
    Principal,
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
    DomainInvariantError,
    DomainValidationError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    OwnershipError,
)
from chat_service.infrastructure.persistence.models import (
    CitationModel,
    ConversationModel,
    MessageModel,
    TurnModel,
)

IDEMPOTENCY_CONSTRAINT = "uq_turns_conversation_idempotency"
PROCESSING_STATE_VALUES = (
    InternalTurnState.ACCEPTED.value,
    InternalTurnState.WAITING_FOR_CAPACITY.value,
    InternalTurnState.RETRIEVING.value,
    InternalTurnState.GENERATING.value,
)
TERMINAL_STATES = frozenset(
    {
        InternalTurnState.COMPLETED,
        InternalTurnState.FAILED,
        InternalTurnState.FAILED_UNKNOWN,
    }
)

RETRIEVAL_DIAGNOSTIC_KEYS = frozenset(
    {"candidate_count", "selected_count", "model_id", "model_revision"}
)
GENERATION_DIAGNOSTIC_KEYS = frozenset(
    {
        "provider",
        "model",
        "prompt_version",
        "finish_reason",
        "prompt_tokens",
        "completion_tokens",
    }
)
ANSWER_PART_KEYS = frozenset({"text", "citation_ids"})


class SqlAlchemyTurnRepository:
    """以呼叫端提供的 ``AsyncSession`` 實作 TurnRepositoryPort。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_idempotency(
        self,
        principal: Principal,
        conversation_id: UUID,
        idempotency_key: str,
    ) -> GetTurnResult | None:
        """以單一 owner-filtered turn query 載入可供冪等重播的完整結果。"""

        model = await self._find_turn_model(
            principal=principal,
            conversation_id=conversation_id,
            idempotency_key=idempotency_key,
            for_update=False,
        )
        return (
            None if model is None else await self._hydrate_turn_result(model, principal=principal)
        )

    async def get_for_owner(self, query: GetTurnQuery) -> GetTurnResult | None:
        """以 owner join 讀取 turn、messages、citations 與 safe error，不配置 read lock。"""

        model = await self._find_turn_model(
            principal=query.principal,
            conversation_id=query.conversation_id,
            turn_id=query.turn_id,
            for_update=False,
        )
        return (
            None
            if model is None
            else await self._hydrate_turn_result(model, principal=query.principal)
        )

    async def get_for_owner_for_update(self, query: GetTurnQuery) -> GetTurnResult | None:
        """先鎖 owner conversation，再鎖 turn，供 transition 的 CAS 流程使用。"""

        if not await self._lock_conversation_for_owner(
            query.principal,
            query.conversation_id,
        ):
            return None
        model = await self._find_turn_model(
            principal=query.principal,
            conversation_id=query.conversation_id,
            turn_id=query.turn_id,
            for_update=True,
        )
        return (
            None
            if model is None
            else await self._hydrate_turn_result(model, principal=query.principal)
        )

    async def add(
        self,
        turn: ChatTurn,
        user_message: Message,
        principal: Principal,
    ) -> None:
        """在 owner parent lock 下新增 accepted turn 與唯一的 user message。

        PostgreSQL named unique constraint 是跨 worker 冪等競態的最終仲裁。這裡只把該特定
        constraint 轉成 typed conflict；其他 integrity error 保留原型，且絕不自行 commit。
        """

        await self._require_owned_conversation(principal, turn.conversation_id)
        self._validate_new_turn_and_user_message(turn, user_message)

        turn_model = TurnModel(
            id=turn.id,
            conversation_id=turn.conversation_id,
            ordinal=turn.ordinal,
            idempotency_key=turn.idempotency_key,
            request_fingerprint=turn.request_fingerprint,
            original_question=turn.original_question,
            processed_query=turn.processed_query,
            language=turn.language,
            state=turn.state.value,
            retrieval_status=None,
            answer_status=None,
            outcome=None,
            response_http_status=None,
            safe_error_code=None,
            safe_error_message=None,
            error_retryable=False,
            retry_after_seconds=None,
            request_id=turn.request_id,
            retrieval_diagnostics=None,
            generation_diagnostics=None,
            started_at=None,
            completed_at=None,
        )
        # ORM models 沒有 relationship 可讓 SQLAlchemy 推導 unit-of-work dependency，故必須
        # 明確先 flush parent turn，避免 PostgreSQL 在 user message INSERT 時看不到 FK target。
        self._session.add(turn_model)
        try:
            await self._session.flush()
        except IntegrityError as error:
            if self._integrity_constraint_name(error) == IDEMPOTENCY_CONSTRAINT:
                raise IdempotencyConflictError(
                    "idempotency key is already in use for this conversation"
                ) from None
            raise

        # INSERT server defaults 必須回到 domain turn，讓後續 transition 可用 updated_at 作
        # 額外的 stale-object 防線；這仍只是在目前 transaction 內 refresh。
        await self._session.refresh(turn_model)

        message_model = MessageModel(
            id=user_message.id,
            conversation_id=user_message.conversation_id,
            turn_id=user_message.turn_id,
            ordinal=user_message.ordinal,
            role=MessageRole.USER.value,
            response_kind=None,
            content=user_message.content,
            answer_parts=None,
            payload_schema_version=user_message.payload_schema_version,
        )
        self._session.add(message_model)
        await self._session.flush()

        turn.created_at = turn_model.created_at
        turn.updated_at = turn_model.updated_at
        turn.started_at = None
        turn.completed_at = None

    async def add_assistant_message(self, message: Message, principal: Principal) -> None:
        """只為 outcome 相符的 completed turn 寫入安全 answer parts 與 citations。"""

        if message.role is not MessageRole.ASSISTANT:
            raise DomainInvariantError("assistant message repository requires the assistant role")
        await self._require_owned_conversation(principal, message.conversation_id)

        # Child row 也要在 parent lock 後確認隸屬關係；populate_existing 讓同一 transaction
        # 先完成 turn update、再新增 assistant 的合法順序可立即看見最新 terminal state。
        turn_model = await self._find_turn_model(
            principal=principal,
            conversation_id=message.conversation_id,
            turn_id=message.turn_id,
            for_update=True,
        )
        if turn_model is None:
            raise OwnershipError("turn is unavailable to this principal")
        if (
            self._internal_state(turn_model.state) is not InternalTurnState.COMPLETED
            or turn_model.completed_at is None
            or turn_model.outcome is None
            or message.response_kind is None
            or turn_model.outcome != message.response_kind.value
        ):
            raise DomainInvariantError(
                "assistant message requires a completed turn with the same outcome"
            )

        model = MessageModel(
            id=message.id,
            conversation_id=message.conversation_id,
            turn_id=message.turn_id,
            ordinal=message.ordinal,
            role=MessageRole.ASSISTANT.value,
            response_kind=None if message.response_kind is None else message.response_kind.value,
            content=message.content,
            answer_parts=self._serialize_answer_parts(message.answer_parts),
            payload_schema_version=message.payload_schema_version,
        )
        self._session.add(model)
        await self._session.flush()

        citation_models = [
            CitationModel(
                id=uuid4(),
                assistant_message_id=message.id,
                citation_id=citation.citation_id,
                ordinal=ordinal,
                chunk_id=citation.chunk_id,
                document_id=citation.document_id,
                version_id=citation.version_id,
                title=citation.title,
                section_path=citation.section_path,
                page_number=citation.page_number,
            )
            for ordinal, citation in enumerate(message.citations, start=1)
        ]
        self._session.add_all(citation_models)
        await self._session.flush()

    async def update(self, turn: ChatTurn, principal: Principal) -> None:
        """以 conversation-first lock、immutable check 與 state CAS 保存 transition。

        ``updated_at``、首次進入 retrieving 的 ``started_at``，以及首次 terminal 的
        ``completed_at`` 都取 lock 後的 PostgreSQL clock。此方法刻意不修改 conversation；
        assistant 成功路徑由 message ordinal allocation bump aggregate，無 assistant 的 terminal
        路徑則由 ``record_terminal_activity`` bump。
        """

        await self._require_owned_conversation(principal, turn.conversation_id)
        model = await self._find_turn_model(
            principal=principal,
            conversation_id=turn.conversation_id,
            turn_id=turn.id,
            for_update=True,
        )
        if model is None:
            raise OwnershipError("turn is unavailable to this principal")

        self._validate_immutable_turn_fields(model, turn)
        current_state = self._internal_state(model.state)
        self._validate_target_state_payload(turn)
        self._validate_transition(current_state, turn)
        database_now = await self._database_now()

        values: dict[str, Any] = {
            "state": turn.state.value,
            "retrieval_status": self._enum_value(turn.retrieval_status),
            "answer_status": self._enum_value(turn.answer_status),
            "outcome": self._enum_value(turn.outcome),
            "response_http_status": turn.response_http_status,
            "safe_error_code": turn.safe_error_code,
            "safe_error_message": turn.safe_error_message,
            "error_retryable": turn.error_retryable,
            "retry_after_seconds": turn.retry_after_seconds,
            "retrieval_diagnostics": self._serialize_retrieval_diagnostics(
                turn.retrieval_diagnostics
            ),
            "generation_diagnostics": self._serialize_generation_diagnostics(
                turn.generation_diagnostics
            ),
            "updated_at": database_now,
        }
        if turn.state is InternalTurnState.RETRIEVING and model.started_at is None:
            values["started_at"] = database_now
        if turn.state in TERMINAL_STATES and model.completed_at is None:
            values["completed_at"] = database_now

        statement = (
            update(TurnModel)
            .where(
                TurnModel.id == turn.id,
                TurnModel.conversation_id == turn.conversation_id,
                # Row lock 已串行化 writers；state 仍放入 predicate，避免日後重構時
                # 不慎移除 lock 後變成無條件 lost update。
                TurnModel.state == current_state.value,
            )
            .values(**values)
            .returning(
                TurnModel.updated_at,
                TurnModel.started_at,
                TurnModel.completed_at,
            )
            .execution_options(synchronize_session=False)
        )
        result = (await self._session.execute(statement)).one_or_none()
        if result is None:
            raise InvalidStateTransitionError("turn state changed before the transition was saved")

        # Core UPDATE 停用 session synchronization 以保留明確 CAS predicate；refresh 防止
        # identity map 在同一 UoW 內留下舊 state/timestamps。
        await self._session.refresh(model)
        turn.updated_at = model.updated_at
        turn.started_at = model.started_at
        turn.completed_at = model.completed_at

    async def list_messages(self, query: ListMessagesQuery) -> ListMessagesResult:
        """依 ordinal ASC 執行 ``limit + 1`` keyset pagination 並 hydrate citations。"""

        statement = (
            select(MessageModel)
            .join(ConversationModel, ConversationModel.id == MessageModel.conversation_id)
            .where(
                MessageModel.conversation_id == query.conversation_id,
                ConversationModel.id == query.conversation_id,
                ConversationModel.owner_issuer == query.principal.issuer,
                ConversationModel.owner_subject == query.principal.subject,
            )
        )
        if query.cursor is not None:
            statement = statement.where(MessageModel.ordinal > query.cursor.ordinal)
        statement = statement.order_by(MessageModel.ordinal.asc()).limit(query.limit + 1)

        models = list((await self._session.scalars(statement)).all())
        has_next_page = len(models) > query.limit
        page = models[: query.limit]
        citations = await self._load_citations_for_messages(
            principal=query.principal,
            conversation_id=query.conversation_id,
            message_ids=tuple(model.id for model in page),
        )
        items = tuple(self._hydrate_message(model, citations.get(model.id, ())) for model in page)

        next_cursor: MessageCursorPayload | None = None
        if has_next_page:
            next_cursor = MessageCursorPayload(version=1, ordinal=page[-1].ordinal)
        return ListMessagesResult(items=items, next_cursor=next_cursor)

    async def recover_stale(self, processing_stale_seconds: float) -> int:
        """以 DB clock 原子把 stale processing turns 復原為 ``failed_unknown``。

        先讀候選 parent IDs，再依 UUID 升冪鎖 conversation；鎖後重新套用 state/cutoff
        predicate，避免把等待鎖期間已完成的 turn 錯殺。同一 conversation 無論含幾個 stale
        turns，row version 與 activity 都只增加一次。
        """

        if (
            isinstance(processing_stale_seconds, bool)
            or not isinstance(processing_stale_seconds, (int, float))
            or not isfinite(float(processing_stale_seconds))
            or processing_stale_seconds <= 0
        ):
            raise DomainValidationError("processing_stale_seconds must be a positive finite number")

        _, candidate_cutoff = await self._database_recovery_window(float(processing_stale_seconds))
        candidate_statement = (
            select(TurnModel.conversation_id)
            .where(
                TurnModel.state.in_(PROCESSING_STATE_VALUES),
                TurnModel.updated_at < candidate_cutoff,
            )
            .distinct()
            .order_by(TurnModel.conversation_id.asc())
        )
        candidate_ids = tuple((await self._session.scalars(candidate_statement)).all())
        if not candidate_ids:
            return 0

        locked_ids = tuple(
            (
                await self._session.scalars(
                    select(ConversationModel.id)
                    .where(ConversationModel.id.in_(candidate_ids))
                    .order_by(ConversationModel.id.asc())
                    .with_for_update()
                )
            ).all()
        )
        if not locked_ids:
            return 0

        # Conversation locks 可能等待其他 transaction；鎖到後必須重新取 wall clock/cutoff，
        # 才不會以 lock wait 前的舊時間覆寫 activity，並再次判斷 turn 是否仍 stale。
        database_now, cutoff = await self._database_recovery_window(float(processing_stale_seconds))

        recovery_statement = (
            update(TurnModel)
            .where(
                TurnModel.conversation_id.in_(locked_ids),
                TurnModel.state.in_(PROCESSING_STATE_VALUES),
                TurnModel.updated_at < cutoff,
            )
            .values(
                state=InternalTurnState.FAILED_UNKNOWN.value,
                response_http_status=503,
                safe_error_code=PROCESSING_INTERRUPTED_ERROR_CODE,
                safe_error_message=PROCESSING_INTERRUPTED_ERROR_MESSAGE,
                error_retryable=False,
                retry_after_seconds=None,
                updated_at=database_now,
                completed_at=database_now,
            )
            .returning(TurnModel.conversation_id)
            .execution_options(synchronize_session=False)
        )
        recovered_conversation_ids = tuple((await self._session.scalars(recovery_statement)).all())
        if not recovered_conversation_ids:
            return 0

        affected_ids = tuple(sorted(set(recovered_conversation_ids), key=lambda value: value.int))
        await self._session.execute(
            update(ConversationModel)
            .where(ConversationModel.id.in_(affected_ids))
            .values(
                row_version=ConversationModel.row_version + 1,
                updated_at=database_now,
                last_activity_at=database_now,
            )
            .execution_options(synchronize_session=False)
        )
        # Bulk UPDATE 不經 ORM attribute instrumentation；清掉可能由同一 UoW 先前載入的
        # turn/conversation snapshot，避免後續讀到 recovery 前的 state 或 row_version。
        self._session.expire_all()
        return len(recovered_conversation_ids)

    async def _lock_conversation_for_owner(
        self,
        principal: Principal,
        conversation_id: UUID,
    ) -> bool:
        """以完整 owner predicate 鎖 parent；mismatch 與不存在不作區分。"""

        owned_id = await self._session.scalar(
            select(ConversationModel.id)
            .where(
                ConversationModel.id == conversation_id,
                ConversationModel.owner_issuer == principal.issuer,
                ConversationModel.owner_subject == principal.subject,
            )
            .with_for_update()
        )
        return owned_id is not None

    async def _require_owned_conversation(
        self,
        principal: Principal,
        conversation_id: UUID,
    ) -> None:
        """Mutation 找不到 owner parent 時使用不洩漏存在性的 typed error。"""

        if not await self._lock_conversation_for_owner(principal, conversation_id):
            raise OwnershipError("conversation is unavailable to this principal")

    async def _find_turn_model(
        self,
        *,
        principal: Principal,
        conversation_id: UUID,
        turn_id: UUID | None = None,
        idempotency_key: str | None = None,
        for_update: bool,
    ) -> TurnModel | None:
        """以 owner join 限定 child；mutation caller 會先鎖 parent，read caller 不加鎖。"""

        if (turn_id is None) == (idempotency_key is None):
            raise RuntimeError("turn lookup requires exactly one selector")
        statement = (
            select(TurnModel)
            .join(ConversationModel, ConversationModel.id == TurnModel.conversation_id)
            .where(
                TurnModel.conversation_id == conversation_id,
                ConversationModel.id == conversation_id,
                ConversationModel.owner_issuer == principal.issuer,
                ConversationModel.owner_subject == principal.subject,
            )
            .execution_options(populate_existing=True)
        )
        if turn_id is not None:
            statement = statement.where(TurnModel.id == turn_id)
        else:
            statement = statement.where(TurnModel.idempotency_key == idempotency_key)
        if for_update:
            statement = statement.with_for_update(of=TurnModel)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _hydrate_turn_result(
        self,
        model: TurnModel,
        *,
        principal: Principal,
    ) -> GetTurnResult:
        """完整載入單一 turn 的 user、optional assistant、citations 與 SafeError。"""

        message_models = list(
            (
                await self._session.scalars(
                    select(MessageModel)
                    .join(
                        ConversationModel,
                        ConversationModel.id == MessageModel.conversation_id,
                    )
                    .where(
                        MessageModel.conversation_id == model.conversation_id,
                        MessageModel.turn_id == model.id,
                        ConversationModel.id == model.conversation_id,
                        ConversationModel.owner_issuer == principal.issuer,
                        ConversationModel.owner_subject == principal.subject,
                    )
                    .order_by(MessageModel.ordinal.asc())
                )
            ).all()
        )
        user_models = [item for item in message_models if item.role == MessageRole.USER.value]
        assistant_models = [
            item for item in message_models if item.role == MessageRole.ASSISTANT.value
        ]
        if len(user_models) != 1 or len(assistant_models) > 1:
            raise DomainInvariantError("persisted turn has an invalid message set")
        if len(user_models) + len(assistant_models) != len(message_models):
            raise DomainInvariantError("persisted turn contains an unknown message role")

        assistant_model = assistant_models[0] if assistant_models else None
        citations: tuple[Citation, ...] = ()
        if assistant_model is not None:
            by_message = await self._load_citations_for_messages(
                principal=principal,
                conversation_id=model.conversation_id,
                message_ids=(assistant_model.id,),
            )
            citations = by_message.get(assistant_model.id, ())

        turn = self._hydrate_turn(model)
        user_message = self._hydrate_message(user_models[0], ())
        assistant_message = (
            None if assistant_model is None else self._hydrate_message(assistant_model, citations)
        )
        error = None if turn.failure is None else SafeError.from_turn_failure(turn.failure)
        return GetTurnResult(
            result=TurnResult(
                turn=turn,
                user_message=user_message,
                assistant_message=assistant_message,
            ),
            error=error,
        )

    async def _load_citations_for_messages(
        self,
        *,
        principal: Principal,
        conversation_id: UUID,
        message_ids: tuple[UUID, ...],
    ) -> dict[UUID, tuple[Citation, ...]]:
        """批次讀取 page/turn 所需 citations，並以 owning conversation 再次限縮。"""

        if not message_ids:
            return {}
        statement = (
            select(CitationModel)
            .join(MessageModel, MessageModel.id == CitationModel.assistant_message_id)
            .join(ConversationModel, ConversationModel.id == MessageModel.conversation_id)
            .where(
                CitationModel.assistant_message_id.in_(message_ids),
                MessageModel.conversation_id == conversation_id,
                ConversationModel.id == conversation_id,
                ConversationModel.owner_issuer == principal.issuer,
                ConversationModel.owner_subject == principal.subject,
            )
            .order_by(
                CitationModel.assistant_message_id.asc(),
                CitationModel.ordinal.asc(),
            )
        )
        rows = tuple((await self._session.scalars(statement)).all())
        grouped: dict[UUID, list[Citation]] = {}
        for row in rows:
            grouped.setdefault(row.assistant_message_id, []).append(self._hydrate_citation(row))
        return {message_id: tuple(items) for message_id, items in grouped.items()}

    @staticmethod
    def _validate_new_turn_and_user_message(turn: ChatTurn, message: Message) -> None:
        """拒絕跨 aggregate 關聯、非 accepted insert 或非 user payload。"""

        if turn.state is not InternalTurnState.ACCEPTED:
            raise InvalidStateTransitionError("new turns must start in accepted state")
        if any(
            value is not None
            for value in (
                turn.retrieval_status,
                turn.answer_status,
                turn.outcome,
                turn.failure,
                turn.retrieval_diagnostics,
                turn.generation_diagnostics,
                turn.started_at,
                turn.completed_at,
            )
        ):
            raise DomainInvariantError("new accepted turn contains processing or terminal data")
        if message.role is not MessageRole.USER:
            raise DomainInvariantError("new turn requires a user message")
        if (
            message.conversation_id != turn.conversation_id
            or message.turn_id != turn.id
            or message.content != turn.original_question
        ):
            raise DomainInvariantError("user message does not belong to the new turn")

    @staticmethod
    def _validate_immutable_turn_fields(model: TurnModel, turn: ChatTurn) -> None:
        """State transition 不可改寫 idempotency identity 或原始 request payload。"""

        immutable_fields_match = (
            model.id == turn.id
            and model.conversation_id == turn.conversation_id
            and model.ordinal == turn.ordinal
            and model.idempotency_key == turn.idempotency_key
            and model.request_fingerprint == turn.request_fingerprint
            and model.original_question == turn.original_question
            and model.processed_query == turn.processed_query
            and model.language == turn.language
            and model.request_id == turn.request_id
        )
        if not immutable_fields_match:
            raise DomainInvariantError("immutable turn fields differ from the persisted turn")
        if turn.created_at is not None and turn.created_at != model.created_at:
            raise DomainInvariantError("turn created_at is immutable and database-owned")
        if turn.updated_at is not None and turn.updated_at != model.updated_at:
            raise InvalidStateTransitionError("turn was updated by another transaction")

    @classmethod
    def _validate_transition(cls, current_state: InternalTurnState, turn: ChatTurn) -> None:
        """重用 Domain transition methods 驗證 DB current state 到 requested target。"""

        if current_state in TERMINAL_STATES:
            raise InvalidStateTransitionError("terminal turns cannot transition again")
        probe = ChatTurn(
            id=turn.id,
            conversation_id=turn.conversation_id,
            ordinal=turn.ordinal,
            idempotency_key=turn.idempotency_key,
            request_fingerprint=turn.request_fingerprint,
            original_question=turn.original_question,
            processed_query=turn.processed_query,
            request_id=turn.request_id,
            language=turn.language,
            state=current_state,
        )
        if turn.state is InternalTurnState.FAILED:
            if turn.failure is None:
                raise DomainInvariantError("failed transition requires a safe failure")
            probe.fail(turn.failure)
        elif turn.state is InternalTurnState.FAILED_UNKNOWN:
            if turn.failure is None:
                raise DomainInvariantError("failed_unknown transition requires a safe failure")
            probe.fail(turn.failure, result_unknown=True)
        else:
            probe.transition_to(turn.state)

    @staticmethod
    def _validate_target_state_payload(turn: ChatTurn) -> None:
        """補足 entity constructor 未強制的 processing/terminal persistence invariant。"""

        is_failed = turn.state in {
            InternalTurnState.FAILED,
            InternalTurnState.FAILED_UNKNOWN,
        }
        if is_failed:
            if not isinstance(turn.failure, TurnFailure):
                raise DomainInvariantError("failed turn requires a typed safe failure")
        elif turn.failure is not None:
            raise DomainInvariantError("non-failed turn cannot contain failure data")

        if turn.state in TERMINAL_STATES:
            if turn.state is InternalTurnState.COMPLETED:
                if turn.outcome is None:
                    raise DomainInvariantError("completed turn requires a business outcome")
            elif turn.outcome is not None:
                raise DomainInvariantError("failed turn cannot contain a business outcome")
        elif turn.outcome is not None:
            raise DomainInvariantError("processing turn cannot contain a business outcome")

    @classmethod
    def _hydrate_turn(cls, model: TurnModel) -> ChatTurn:
        """把 flattened ORM failure/diagnostics 還原成 typed Domain aggregate。"""

        state = cls._internal_state(model.state)
        failure = cls._hydrate_failure(model, state)
        try:
            retrieval_status = (
                None if model.retrieval_status is None else RetrievalStatus(model.retrieval_status)
            )
            answer_status = (
                None if model.answer_status is None else AnswerStatus(model.answer_status)
            )
            outcome = None if model.outcome is None else ChatOutcome(model.outcome)
        except ValueError as error:
            raise DomainInvariantError("persisted turn contains an unknown status") from error

        return ChatTurn(
            id=model.id,
            conversation_id=model.conversation_id,
            ordinal=model.ordinal,
            idempotency_key=model.idempotency_key,
            request_fingerprint=model.request_fingerprint,
            original_question=model.original_question,
            processed_query=model.processed_query,
            request_id=model.request_id,
            language=model.language,
            state=state,
            retrieval_status=retrieval_status,
            answer_status=answer_status,
            outcome=outcome,
            failure=failure,
            retrieval_diagnostics=cls._hydrate_retrieval_diagnostics(model.retrieval_diagnostics),
            generation_diagnostics=cls._hydrate_generation_diagnostics(
                model.generation_diagnostics
            ),
            created_at=model.created_at,
            updated_at=model.updated_at,
            started_at=model.started_at,
            completed_at=model.completed_at,
        )

    @staticmethod
    def _hydrate_failure(
        model: TurnModel,
        state: InternalTurnState,
    ) -> TurnFailure | None:
        """只從規格允許的五個 safe 欄位建立 replay error。"""

        is_failed = state in {
            InternalTurnState.FAILED,
            InternalTurnState.FAILED_UNKNOWN,
        }
        if not is_failed:
            if (
                model.response_http_status is not None
                or model.safe_error_code is not None
                or model.safe_error_message is not None
                or model.error_retryable
                or model.retry_after_seconds is not None
            ):
                raise DomainInvariantError("non-failed persisted turn contains failure data")
            return None
        if (
            model.response_http_status is None
            or model.safe_error_code is None
            or model.safe_error_message is None
        ):
            raise DomainInvariantError("failed persisted turn is missing safe failure data")
        return TurnFailure(
            response_http_status=model.response_http_status,
            safe_error_code=model.safe_error_code,
            safe_error_message=model.safe_error_message,
            error_retryable=model.error_retryable,
            retry_after_seconds=model.retry_after_seconds,
        )

    @classmethod
    def _hydrate_message(
        cls,
        model: MessageModel,
        citations: tuple[Citation, ...],
    ) -> Message:
        """還原 message；user JSONB 必須為 null，assistant JSONB 僅准 answer parts。"""

        try:
            role = MessageRole(model.role)
            response_kind = (
                None if model.response_kind is None else ChatOutcome(model.response_kind)
            )
        except ValueError as error:
            raise DomainInvariantError("persisted message contains an unknown enum") from error
        if role is MessageRole.USER and model.answer_parts is not None:
            raise DomainInvariantError("persisted user message contains assistant JSON")
        if role is MessageRole.USER and citations:
            raise DomainInvariantError("persisted user message contains citations")

        return Message(
            id=model.id,
            conversation_id=model.conversation_id,
            turn_id=model.turn_id,
            ordinal=model.ordinal,
            role=role,
            content=model.content,
            response_kind=response_kind,
            answer_parts=cls._hydrate_answer_parts(model.answer_parts),
            citations=citations,
            payload_schema_version=model.payload_schema_version,
            created_at=model.created_at,
        )

    @staticmethod
    def _hydrate_citation(model: CitationModel) -> Citation:
        """Citation mapping 刻意沒有 source path、URL 或 evidence content。"""

        return Citation(
            citation_id=model.citation_id,
            chunk_id=model.chunk_id,
            document_id=model.document_id,
            version_id=model.version_id,
            title=model.title,
            section_path=model.section_path,
            page_number=model.page_number,
        )

    @staticmethod
    def _serialize_answer_parts(
        parts: tuple[AnswerPart, ...],
    ) -> list[dict[str, Any]] | None:
        """只保存 text 與 citation IDs，不序列化任意 object state。"""

        if not parts:
            return None
        return [{"text": part.text, "citation_ids": list(part.citation_ids)} for part in parts]

    @staticmethod
    def _hydrate_answer_parts(payload: list[dict[str, Any]] | None) -> tuple[AnswerPart, ...]:
        """拒絕 JSONB 中任何非 answer-part allowlist 的欄位。"""

        if payload is None:
            return ()
        parts: list[AnswerPart] = []
        for item in payload:
            if not isinstance(item, dict) or frozenset(item) != ANSWER_PART_KEYS:
                raise DomainInvariantError("persisted answer part has an invalid shape")
            text_value = item.get("text")
            citation_ids_value = item.get("citation_ids")
            if not isinstance(text_value, str) or not isinstance(citation_ids_value, list):
                raise DomainInvariantError("persisted answer part has invalid field types")
            if any(not isinstance(value, str) for value in citation_ids_value):
                raise DomainInvariantError("persisted answer part has invalid citation IDs")
            parts.append(
                AnswerPart(
                    text=text_value,
                    citation_ids=tuple(cast(list[str], citation_ids_value)),
                )
            )
        return tuple(parts)

    @staticmethod
    def _serialize_retrieval_diagnostics(
        diagnostics: RetrievalDiagnostics | None,
    ) -> dict[str, Any] | None:
        """明確 allowlist Retrieval 計數/model revision，絕不保存 evidence。"""

        if diagnostics is None:
            return None
        return {
            "candidate_count": diagnostics.candidate_count,
            "selected_count": diagnostics.selected_count,
            "model_id": diagnostics.model_id,
            "model_revision": diagnostics.model_revision,
        }

    @staticmethod
    def _serialize_generation_diagnostics(
        diagnostics: GenerationDiagnostics | None,
    ) -> dict[str, Any] | None:
        """明確 allowlist provider/model/version/reason/token counts。"""

        if diagnostics is None:
            return None
        return {
            "provider": diagnostics.provider,
            "model": diagnostics.model,
            "prompt_version": diagnostics.prompt_version,
            "finish_reason": diagnostics.finish_reason,
            "prompt_tokens": diagnostics.prompt_tokens,
            "completion_tokens": diagnostics.completion_tokens,
        }

    @staticmethod
    def _hydrate_retrieval_diagnostics(
        payload: dict[str, Any] | None,
    ) -> RetrievalDiagnostics | None:
        """只接受 writer 定義的 Retrieval diagnostics canonical JSON shape。"""

        if payload is None:
            return None
        if not isinstance(payload, dict) or frozenset(payload) != RETRIEVAL_DIAGNOSTIC_KEYS:
            raise DomainInvariantError("persisted retrieval diagnostics have an invalid shape")
        return RetrievalDiagnostics(
            candidate_count=payload["candidate_count"],
            selected_count=payload["selected_count"],
            model_id=payload["model_id"],
            model_revision=payload["model_revision"],
        )

    @staticmethod
    def _hydrate_generation_diagnostics(
        payload: dict[str, Any] | None,
    ) -> GenerationDiagnostics | None:
        """只接受 writer 定義的 Generation diagnostics canonical JSON shape。"""

        if payload is None:
            return None
        if not isinstance(payload, dict) or frozenset(payload) != GENERATION_DIAGNOSTIC_KEYS:
            raise DomainInvariantError("persisted generation diagnostics have an invalid shape")
        return GenerationDiagnostics(
            provider=payload["provider"],
            model=payload["model"],
            prompt_version=payload["prompt_version"],
            finish_reason=payload["finish_reason"],
            prompt_tokens=payload["prompt_tokens"],
            completion_tokens=payload["completion_tokens"],
        )

    async def _database_now(self) -> datetime:
        """在 parent lock 後讀 PostgreSQL wall clock，避免 lock wait 前的 transaction time。"""

        value = await self._session.scalar(select(func.clock_timestamp()))
        if value is None:
            raise RuntimeError("database did not return its current time")
        return cast(datetime, value)

    async def _database_recovery_window(self, stale_seconds: float) -> tuple[datetime, datetime]:
        """讓 PostgreSQL 用同一個 wall-clock value 計算 recovery now 與 cutoff。"""

        statement = text(
            """
            WITH database_clock AS MATERIALIZED (
                SELECT clock_timestamp() AS database_now
            )
            SELECT
                database_now,
                database_now - make_interval(
                    secs => CAST(:stale_seconds AS double precision)
                ) AS cutoff
            FROM database_clock
            """
        )
        row = (
            (await self._session.execute(statement, {"stale_seconds": stale_seconds}))
            .mappings()
            .one()
        )
        database_now = cast(datetime, row["database_now"])
        cutoff = cast(datetime, row["cutoff"])
        if (
            database_now.tzinfo is None
            or database_now.utcoffset() is None
            or cutoff.tzinfo is None
            or cutoff.utcoffset() is None
        ):
            raise RuntimeError("database returned a timezone-naive recovery clock")
        return database_now, cutoff

    @staticmethod
    def _internal_state(value: str) -> InternalTurnState:
        """把不可信 DB string 轉成有限 state，不把值寫入錯誤訊息。"""

        try:
            return InternalTurnState(value)
        except ValueError as error:
            raise DomainInvariantError("persisted turn contains an unknown state") from error

    @staticmethod
    def _enum_value(value: RetrievalStatus | AnswerStatus | ChatOutcome | None) -> str | None:
        """集中 nullable StrEnum persistence mapping。"""

        return None if value is None else value.value

    @staticmethod
    def _integrity_constraint_name(error: IntegrityError) -> str | None:
        """跨 asyncpg/DBAPI wrapper 取得 named constraint，不解析含 payload 的 detail。"""

        current: BaseException | None = error.orig
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            name = getattr(current, "constraint_name", None)
            if isinstance(name, str):
                return name
            diagnostic = getattr(current, "diag", None)
            diagnostic_name = getattr(diagnostic, "constraint_name", None)
            if isinstance(diagnostic_name, str):
                return diagnostic_name
            cause = current.__cause__
            current = cause if cause is not None else current.__context__

        # SQLAlchemy's asyncpg wrapper versions do not all expose constraint_name.
        # The fallback only recognizes the static migration name and never propagates detail.
        if IDEMPOTENCY_CONSTRAINT in str(error.orig):
            return IDEMPOTENCY_CONSTRAINT
        return None
