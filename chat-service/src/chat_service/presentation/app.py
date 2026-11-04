"""Chat Service FastAPI application factory。

Presentation 只負責 HTTP schema、identity input、cursor/error mapping 與安全 response projection；
所有商業流程透過 ``bootstrap.Runtime`` 的 use case 執行。Route 不建立 SQLAlchemy session、
不直接呼叫 HTTP adapter，也不把 owner/body identity 交給 application 當成可信資料。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID, uuid4

import structlog
from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from chat_service.application.dtos import (
    UNSET,
    CreateConversationCommand,
    ExecuteConversationPurgeCommand,
    GetConversationQuery,
    GetTurnQuery,
    IdentityInput,
    ListConversationsQuery,
    ListMessagesQuery,
    PreviewConversationPurgeCommand,
    SubmitTurnCommand,
    UpdateConversationCommand,
)
from chat_service.bootstrap import Runtime, build_runtime
from chat_service.domain.entities import Message, Principal, PurgeSelector
from chat_service.domain.exceptions import (
    AdminRequiredError,
    ArchivedConversationError,
    ConversationBusyError,
    DependencyError,
    DomainValidationError,
    IdempotencyConflictError,
    InvalidCursorError,
    InvalidIdempotencyKeyError,
    OwnershipError,
    PurgePreviewConsumedError,
    PurgePreviewExpiredError,
    PurgePreviewNotFoundError,
    PurgePreviewStaleError,
    PurgeSelectionTooLargeError,
    UnauthorizedPrincipalError,
)
from chat_service.infrastructure.config import Settings, get_settings
from chat_service.infrastructure.logging import configure_logging
from chat_service.infrastructure.observability import METRICS_CONTENT_TYPE, Metrics
from chat_service.presentation.cursors import (
    decode_conversation_cursor,
    decode_message_cursor,
    encode_conversation_cursor,
    encode_message_cursor,
)
from chat_service.presentation.middleware import (
    BoundedRequestBodyMiddleware,
    RequestContextMiddleware,
)
from chat_service.presentation.schemas import (
    AnswerPartResponse,
    CitationResponse,
    ConversationResponse,
    CreateConversationRequest,
    ErrorResponse,
    ExecutePurgeRequest,
    HealthResponse,
    ListConversationsResponse,
    ListMessagesResponse,
    MessageResponse,
    PurgePreviewRequest,
    PurgePreviewResponse,
    PurgeResponse,
    SafeErrorResponse,
    SubmitTurnRequest,
    TurnResponse,
    UpdateConversationRequest,
)

logger = structlog.get_logger(__name__)


def create_app(*, settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """建立 FastAPI app；可注入 Runtime 供 API/security tests 使用。"""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    metrics = Metrics.create()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """啟動時 recovery stale turns，關閉時釋放 production-owned resources。"""

        active_runtime = runtime or build_runtime(resolved_settings, metrics=metrics)
        application.state.runtime = active_runtime
        application.state.metrics = metrics
        try:
            startup = getattr(active_runtime, "startup", None)
            if startup is not None:
                await startup()
            yield
        finally:
            if runtime is None:
                await active_runtime.close()

    application = FastAPI(
        title="Chat Service",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            401: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    # Register CORS first, then body and request-context middleware.  Starlette inserts new
    # middleware at the front, so RequestContext ends up outermost and also covers CORS preflight
    # and early 413 responses with the same request ID/security headers.
    if resolved_settings.cors_origins:
        allowed_headers = ["authorization", "content-type", "idempotency-key", "x-request-id"]
        if resolved_settings.app_env != "production":
            allowed_headers.extend(["x-dev-user-id", "x-dev-group-ids", "x-dev-roles"])
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=allowed_headers,
        )

    application.add_middleware(
        BoundedRequestBodyMiddleware,
        max_bytes=resolved_settings.api_max_request_body_bytes,
    )
    application.add_middleware(RequestContextMiddleware, metrics=metrics)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            error="invalid_request",
            message="Request validation failed",
            details=_validation_details(exc),
        )

    @application.exception_handler(StarletteHTTPException)
    async def http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """把 routing/HTTPException 也轉成固定 envelope，不回傳 raw ``detail``。"""

        safe_mapping: dict[int, tuple[str, str]] = {
            400: ("invalid_request", "The request could not be accepted"),
            401: ("unauthorized_principal", "A valid principal is required"),
            403: ("forbidden", "The request is not allowed"),
            404: ("resource_not_found", "The requested resource was not found"),
            405: ("method_not_allowed", "The HTTP method is not allowed"),
            413: ("request_too_large", "Request body exceeds the configured size limit"),
            422: ("invalid_request", "Request validation failed"),
            429: ("chat_capacity_exceeded", "The service is temporarily busy"),
            500: ("internal_error", "Unexpected server error"),
            502: ("dependency_error", "A downstream dependency could not complete the request"),
            503: ("service_unavailable", "The service is temporarily unavailable"),
            504: ("service_timeout", "The service did not complete the request in time"),
        }
        status_code = exc.status_code if 400 <= exc.status_code <= 599 else 500
        error, message = safe_mapping.get(status_code, ("http_error", "The request failed"))
        return _error_response(request, status_code=status_code, error=error, message=message)

    @application.exception_handler(InvalidCursorError)
    async def invalid_cursor(request: Request, exc: InvalidCursorError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=422,
            error="invalid_cursor",
            message="The pagination cursor is invalid",
        )

    @application.exception_handler(InvalidIdempotencyKeyError)
    async def invalid_idempotency_key(
        request: Request,
        exc: InvalidIdempotencyKeyError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=422,
            error="invalid_idempotency_key",
            message="Idempotency-Key must be a UUID",
        )

    @application.exception_handler(DomainValidationError)
    async def invalid_domain(request: Request, exc: DomainValidationError) -> JSONResponse:
        return _error_response(request, status_code=422, error="invalid_request", message=str(exc))

    @application.exception_handler(UnauthorizedPrincipalError)
    async def unauthorized_principal(
        request: Request,
        exc: UnauthorizedPrincipalError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=401,
            error="unauthorized_principal",
            message="A valid principal is required",
        )

    @application.exception_handler(AdminRequiredError)
    async def admin_required(request: Request, exc: AdminRequiredError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=403,
            error="admin_required",
            message="Administrator role is required",
        )

    @application.exception_handler(OwnershipError)
    async def ownership_error(request: Request, exc: OwnershipError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=404,
            error="resource_not_found",
            message="The requested resource was not found",
        )

    @application.exception_handler(ArchivedConversationError)
    async def archived_conversation(
        request: Request,
        exc: ArchivedConversationError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="conversation_archived",
            message="Archived conversations cannot accept new turns",
        )

    @application.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(
        request: Request,
        exc: IdempotencyConflictError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="idempotency_conflict",
            message="Idempotency-Key was already used for another request",
        )

    @application.exception_handler(PurgeSelectionTooLargeError)
    async def purge_selection_too_large(
        request: Request,
        exc: PurgeSelectionTooLargeError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=422,
            error="purge_selection_too_large",
            message="Purge selection exceeds the maximum size",
        )

    @application.exception_handler(PurgePreviewNotFoundError)
    async def purge_preview_not_found(
        request: Request,
        exc: PurgePreviewNotFoundError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=404,
            error="purge_preview_not_found",
            message="The purge preview was not found",
        )

    @application.exception_handler(PurgePreviewExpiredError)
    async def purge_preview_expired(
        request: Request,
        exc: PurgePreviewExpiredError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="purge_preview_expired",
            message="The purge preview has expired",
        )

    @application.exception_handler(PurgePreviewConsumedError)
    async def purge_preview_consumed(
        request: Request,
        exc: PurgePreviewConsumedError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="purge_preview_consumed",
            message="The purge preview was already consumed",
        )

    @application.exception_handler(PurgePreviewStaleError)
    async def purge_preview_stale(
        request: Request,
        exc: PurgePreviewStaleError,
    ) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="purge_preview_stale",
            message="The purge preview no longer matches current data",
        )

    @application.exception_handler(ConversationBusyError)
    async def conversation_busy(request: Request, exc: ConversationBusyError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=409,
            error="conversation_busy",
            message="A selected conversation is still processing",
        )

    @application.exception_handler(SQLAlchemyError)
    async def persistence_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        del exc
        metrics.persistence_failures.labels("request").inc()
        return _error_response(
            request,
            status_code=503,
            error="persistence_unavailable",
            message="Chat persistence is temporarily unavailable",
        )

    @application.exception_handler(DependencyError)
    async def dependency_error(request: Request, exc: DependencyError) -> JSONResponse:
        del exc
        return _error_response(
            request,
            status_code=502,
            error="dependency_error",
            message="A downstream dependency could not complete the request",
        )

    @application.exception_handler(Exception)
    async def unexpected_failure(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error_type=type(exc).__name__)
        return _error_response(
            request,
            status_code=500,
            error="internal_error",
            message="Unexpected server error",
        )

    @application.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        """只確認 process 還能回應，不觸碰 DB 或下游。"""

        return HealthResponse(status="ok", checks={"process": True})

    @application.get("/health/ready", response_model=HealthResponse)
    async def readiness(request: Request) -> JSONResponse:
        """檢查 DB/Retrieval/Answer readiness，絕不呼叫 generation。"""

        try:
            checks = await _runtime(request).readiness_checks()
        except Exception:
            checks = {
                "process": True,
                "postgres": False,
                "retrieval": False,
                "answer_generation": False,
                "identity_provider": False,
            }
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content=HealthResponse(
                status="ok" if ready else "not_ready",
                checks=checks,
            ).model_dump(mode="json"),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        """輸出 isolated registry；不帶任何 user/resource label。"""

        return Response(metrics.exposition(), media_type=METRICS_CONTENT_TYPE)

    @application.post(
        "/api/v1/conversations",
        response_model=ConversationResponse,
        status_code=201,
    )
    async def create_conversation(
        body: CreateConversationRequest,
        request: Request,
    ) -> ConversationResponse:
        principal = await _principal(request)
        result = await _runtime(request).create_conversation.execute(
            CreateConversationCommand(title=body.title),
            principal,
        )
        return _conversation_response(result)

    @application.get("/api/v1/conversations", response_model=ListConversationsResponse)
    async def list_conversations(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = Query(default=None),
    ) -> ListConversationsResponse:
        principal = await _principal(request)
        decoded = None if cursor is None else decode_conversation_cursor(cursor)
        result = await _runtime(request).list_conversations.execute(
            ListConversationsQuery(principal=principal, limit=limit, cursor=decoded)
        )
        return ListConversationsResponse(
            items=[_conversation_response(item) for item in result.items],
            next_cursor=(
                None
                if result.next_cursor is None
                else encode_conversation_cursor(result.next_cursor)
            ),
        )

    @application.get(
        "/api/v1/conversations/{conversation_id}",
        response_model=ConversationResponse,
    )
    async def get_conversation(conversation_id: UUID, request: Request) -> ConversationResponse:
        principal = await _principal(request)
        result = await _runtime(request).get_conversation.execute(
            GetConversationQuery(principal=principal, conversation_id=conversation_id)
        )
        return _conversation_response(result)

    @application.patch(
        "/api/v1/conversations/{conversation_id}",
        response_model=ConversationResponse,
    )
    async def update_conversation(
        conversation_id: UUID,
        body: UpdateConversationRequest,
        request: Request,
    ) -> ConversationResponse:
        principal = await _principal(request)
        title = body.title if "title" in body.model_fields_set else UNSET
        status = body.status
        result = await _runtime(request).update_conversation.execute(
            UpdateConversationCommand(
                principal=principal,
                conversation_id=conversation_id,
                title=title,
                status=status,
            )
        )
        return _conversation_response(result)

    @application.post(
        "/api/v1/conversations/{conversation_id}/turns",
        response_model=TurnResponse,
    )
    async def submit_turn(
        conversation_id: UUID,
        body: SubmitTurnRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        if idempotency_key is None:
            raise InvalidIdempotencyKeyError("idempotency key must be a UUID")
        principal = await _principal(request)
        active_runtime = _runtime(request)
        _set_active_turn_metric(metrics, active_runtime)
        try:
            result = await active_runtime.submit_turn.execute(
                SubmitTurnCommand(
                    principal=principal,
                    conversation_id=conversation_id,
                    idempotency_key=idempotency_key,
                    question=body.question,
                    language=body.language,
                    request_id=_request_id(request),
                )
            )
        finally:
            _set_active_turn_metric(metrics, active_runtime)
        turn = result.result.turn
        if turn.outcome is not None:
            metrics.record_outcome(turn.outcome.value)
        if result.error is not None and result.error.code == "chat_capacity_exceeded":
            metrics.capacity_rejections.labels("queue_timeout").inc()
        if result.error is not None:
            _record_dependency_metric(metrics, result.error.code)
        status = 202 if turn.is_processing else (turn.response_http_status or 200)
        if result.error is not None and not turn.is_processing:
            # Dependency/capacity failures use the common safe error envelope.  The persisted
            # turn remains queryable through GET status, while POST never exposes internal IDs or
            # downstream details in an error body.
            return _error_response(
                request,
                status_code=status,
                error=result.error.code,
                message=result.error.message,
                headers=(
                    {}
                    if result.error.retry_after_seconds is None
                    else {"Retry-After": str(result.error.retry_after_seconds)}
                ),
            )
        response_payload = _turn_response(
            result,
            status_url=_turn_status_url(conversation_id, turn.id),
        ).model_dump(mode="json")
        if turn.is_processing:
            # POST processing contract deliberately exposes only the polling handle; the user
            # message is available from GET status and is not duplicated in the 202 payload.
            response_payload = {
                "conversation_id": str(turn.conversation_id),
                "turn_id": str(turn.id),
                "state": turn.public_state.value,
                "outcome": None,
                "status_url": _turn_status_url(conversation_id, turn.id),
                "request_id": turn.request_id,
            }
        else:
            response_payload.pop("status_url", None)
            response_payload.pop("created_at", None)
            response_payload.pop("updated_at", None)
            if result.error is None:
                # The terminal success contract has no error member; failed terminal turns keep
                # the replay-safe error projection so clients can inspect the persisted failure.
                response_payload.pop("error", None)
        return JSONResponse(
            status_code=status,
            content=response_payload,
            headers=(
                {}
                if result.error is None or result.error.retry_after_seconds is None
                else {"Retry-After": str(result.error.retry_after_seconds)}
            ),
        )

    @application.get(
        "/api/v1/conversations/{conversation_id}/turns/{turn_id}",
        response_model=TurnResponse,
    )
    async def get_turn(
        conversation_id: UUID,
        turn_id: UUID,
        request: Request,
    ) -> TurnResponse:
        principal = await _principal(request)
        result = await _runtime(request).get_turn.execute(
            GetTurnQuery(
                principal=principal,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
        )
        return _turn_response(result, status_url=_turn_status_url(conversation_id, turn_id))

    @application.get(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=ListMessagesResponse,
    )
    async def list_messages(
        conversation_id: UUID,
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None),
    ) -> ListMessagesResponse:
        principal = await _principal(request)
        # list_messages repository 可合法回傳空頁；先驗證 parent owner，避免 non-owner
        # 取得「200 empty」而非規格要求的 indistinguishable 404。
        await _runtime(request).get_conversation.execute(
            GetConversationQuery(principal=principal, conversation_id=conversation_id)
        )
        decoded = None if cursor is None else decode_message_cursor(cursor)
        result = await _runtime(request).list_messages.execute(
            ListMessagesQuery(
                principal=principal,
                conversation_id=conversation_id,
                limit=limit,
                cursor=decoded,
            )
        )
        return ListMessagesResponse(
            items=[_message_response(item) for item in result.items],
            next_cursor=(
                None if result.next_cursor is None else encode_message_cursor(result.next_cursor)
            ),
        )

    @application.post(
        "/api/v1/admin/conversation-purge-previews",
        response_model=PurgePreviewResponse,
        status_code=201,
    )
    async def create_purge_preview(
        body: PurgePreviewRequest,
        request: Request,
    ) -> PurgePreviewResponse:
        principal = await _principal(request)
        result = await _runtime(request).preview_conversation_purge.execute(
            PreviewConversationPurgeCommand(
                principal=principal,
                selector=_purge_selector(body),
                request_id=_request_id(request),
            )
        )
        return PurgePreviewResponse(
            preview_id=result.preview.id,
            conversation_count=result.preview.conversation_count,
            message_count=result.preview.message_count,
            busy_count=result.busy_count,
            expires_at=result.preview.expires_at,
        )

    @application.post(
        "/api/v1/admin/conversation-purges",
        response_model=PurgeResponse,
    )
    async def execute_purge(
        body: ExecutePurgeRequest,
        request: Request,
    ) -> PurgeResponse:
        principal = await _principal(request)
        result = await _runtime(request).execute_conversation_purge.execute(
            ExecuteConversationPurgeCommand(
                principal=principal,
                preview_id=body.preview_id,
                reason=body.reason,
                request_id=_request_id(request),
            )
        )
        return PurgeResponse(
            purge_id=result.purge_id,
            conversation_count=result.conversation_count,
            message_count=result.message_count,
            purged_at=result.purged_at,
        )

    return application


def _runtime(request: Request) -> Runtime:
    """取得 lifespan 建立的 Runtime；route 不直接操作其 infrastructure。"""

    return cast(Runtime, request.app.state.runtime)


async def _principal(request: Request) -> Principal:
    """把 HTTP headers 映射成 untrusted IdentityInput，再交給 identity port 驗證。"""

    authorization = request.headers.get("authorization")
    bearer_token: str | None = None
    if authorization is not None:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token.strip():
            raise UnauthorizedPrincipalError("Authorization must use a Bearer token")
        bearer_token = token.strip()
    return await _runtime(request).identity_provider.resolve(
        IdentityInput(
            bearer_token=bearer_token,
            development_user_id=request.headers.get("x-dev-user-id"),
            development_group_ids=_csv_header(request.headers.get("x-dev-group-ids")),
            development_roles=_csv_header(request.headers.get("x-dev-roles")),
        )
    )


def _csv_header(value: str | None) -> tuple[str, ...]:
    """逗號分隔 header 保留空項，讓 identity adapter 明確拒絕 malformed input。"""

    if value is None or not value.strip():
        return ()
    return tuple(item.strip() for item in value.split(","))


def _request_id(request: Request) -> str:
    """取得 RequestContextMiddleware 已驗證的 request ID。"""

    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else str(uuid4())


def _set_active_turn_metric(metrics: Metrics, runtime: Runtime) -> None:
    """從 in-process capacity adapter 更新 active gauge；fake runtime 沒有該欄位時視為零。"""

    active_count = getattr(getattr(runtime, "capacity_gate", None), "active_count", 0)
    if isinstance(active_count, int) and active_count >= 0:
        metrics.active_turns.set(active_count)


def _record_dependency_metric(metrics: Metrics, error_code: str) -> None:
    """只把 application 已知的 safe error code 映射成固定 dependency label。"""

    retrieval_codes = {
        "retrieval_auth_misconfigured",
        "retrieval_identity_rejected",
        "retrieval_contract_error",
        "retrieval_service_error",
        "retrieval_unavailable",
        "retrieval_timeout",
    }
    answer_codes = {
        "answer_auth_misconfigured",
        "answer_context_rejected",
        "answer_contract_error",
        "provider_contract_error",
        "answer_service_error",
        "provider_unavailable",
        "provider_timeout",
        "generation_busy",
    }
    if error_code in retrieval_codes:
        metrics.record_dependency_error("retrieval", error_code)
    elif error_code in answer_codes:
        metrics.record_dependency_error("answer", error_code)


def _conversation_response(summary: Any) -> ConversationResponse:
    """將 application summary/domain object 投影為 public response。"""

    return ConversationResponse(
        conversation_id=summary.conversation_id,
        title=summary.title,
        status=summary.status,
        created_at=summary.created_at,
        updated_at=summary.updated_at,
        last_activity_at=summary.last_activity_at,
    )


def _message_response(message: Message) -> MessageResponse:
    """只回 message content 與安全 citation metadata；不回 machine evidence fields。"""

    if message.created_at is None:
        raise DomainValidationError("message timestamp is incomplete")
    return MessageResponse(
        message_id=message.id,
        role=message.role,
        content=message.content,
        response_kind=message.response_kind,
        answer_parts=(
            None
            if not message.answer_parts
            else [
                AnswerPartResponse(text=part.text, citation_ids=list(part.citation_ids))
                for part in message.answer_parts
            ]
        ),
        citations=[
            CitationResponse(
                citation_id=citation.citation_id,
                title=citation.title,
                section_path=citation.section_path,
                page_number=citation.page_number,
            )
            for citation in message.citations
        ],
        created_at=message.created_at,
    )


def _turn_response(result: Any, *, status_url: str) -> TurnResponse:
    """把 GetTurnResult 投影為 processing/terminal safe response。"""

    turn = result.result.turn
    return TurnResponse(
        conversation_id=turn.conversation_id,
        turn_id=turn.id,
        state=turn.public_state,
        outcome=turn.outcome,
        user_message=_message_response(result.result.user_message),
        assistant_message=(
            None
            if result.result.assistant_message is None
            else _message_response(result.result.assistant_message)
        ),
        error=(
            None
            if result.error is None
            else SafeErrorResponse(
                code=result.error.code,
                message=result.error.message,
                retryable=result.error.retryable,
                retry_after_seconds=result.error.retry_after_seconds,
            )
        ),
        request_id=turn.request_id,
        status_url=status_url if turn.is_processing else None,
        created_at=turn.created_at,
        updated_at=turn.updated_at,
    )


def _turn_status_url(conversation_id: UUID, turn_id: UUID) -> str:
    """產生不含 host/query 的 relative status URL。"""

    return f"/api/v1/conversations/{conversation_id}/turns/{turn_id}"


def _purge_selector(body: PurgePreviewRequest) -> PurgeSelector:
    """將 strict request model 轉成 framework-free PurgeSelector。"""

    return PurgeSelector(
        conversation_ids=tuple(body.conversation_ids),
        owner_issuer=body.owner_issuer,
        owner_subject=body.owner_subject,
        created_before=body.created_before,
    )


def _validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    """只保留 type/location，不把 Pydantic input/value 或 body 放進 response。"""

    details: list[dict[str, object]] = []
    for error in exc.errors():
        details.append(
            {
                "type": str(error.get("type", "validation_error")),
                "location": [str(part) for part in error.get("loc", ())],
            }
        )
    return details


def _error_response(
    request: Request,
    *,
    status_code: int,
    error: str,
    message: str,
    details: list[dict[str, object]] | dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """建立固定 safe error envelope，永不帶 traceback/downstream/raw body。"""

    content: dict[str, object] = {
        "error": error,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
        "details": details,
    }
    return JSONResponse(status_code=status_code, content=content, headers=headers)


app = create_app()
