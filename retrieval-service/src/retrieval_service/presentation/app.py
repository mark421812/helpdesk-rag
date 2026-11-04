"""Retrieval Service 的 FastAPI application factory。

Presentation layer 負責 HTTP concerns：middleware、API key、request ID、CORS、metrics、
exception-to-status mapping 與 Pydantic schema。真正的檢索政策仍由 Application use case
執行，route 只做 transport mapping。
"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.middleware.base import RequestResponseEndpoint

from retrieval_service.application.dtos import IdentityInput
from retrieval_service.bootstrap import Runtime, build_runtime
from retrieval_service.domain.entities import RetrievalQuery, RetrievalResult
from retrieval_service.domain.exceptions import (
    DependencyContractError,
    DependencyTimeoutError,
    DependencyUnavailableError,
    DomainValidationError,
    RetrievalError,
    UnauthorizedPrincipalError,
)
from retrieval_service.infrastructure.config import Settings, get_settings
from retrieval_service.infrastructure.logging import configure_logging
from retrieval_service.infrastructure.request_context import request_id_context
from retrieval_service.presentation.middleware import BoundedRequestBodyMiddleware
from retrieval_service.presentation.schemas import (
    ErrorResponse,
    EvidenceResponse,
    HealthResponse,
    RetrievalDiagnosticsResponse,
    RetrieveRequest,
    RetrieveResponse,
)

logger = structlog.get_logger(__name__)


def create_app(
    *,
    settings: Settings | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """建立 FastAPI app；可注入 fake Runtime，讓 API tests 不需要真的啟動 KIS。"""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    registry = CollectorRegistry()
    request_count = Counter(
        "retrieval_http_requests_total",
        "Number of HTTP requests handled by retrieval-service.",
        ("method", "path", "status"),
        registry=registry,
    )
    request_duration = Histogram(
        "retrieval_http_request_duration_seconds",
        "Time spent handling retrieval-service HTTP requests.",
        ("method", "path"),
        registry=registry,
    )
    result_count = Counter(
        "retrieval_results_total",
        "Retrieval results by evidence status.",
        ("status",),
        registry=registry,
    )
    candidate_count = Histogram(
        "retrieval_candidate_count",
        "Candidate chunks returned by Knowledge Index Service.",
        registry=registry,
    )
    selected_count = Histogram(
        "retrieval_selected_count",
        "Evidence chunks selected for a retrieval response.",
        registry=registry,
    )

    # asynccontextmanager 把 yield 前後分成 startup 與 shutdown 兩個生命週期階段。
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """啟動時建立 Runtime，關閉時釋放由 app 擁有的 resources。"""

        active_runtime = runtime or build_runtime(resolved_settings)
        application.state.runtime = active_runtime
        try:
            yield
        finally:
            if runtime is None:
                await active_runtime.close()

    application = FastAPI(
        title="Retrieval Service",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            401: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    application.add_middleware(
        BoundedRequestBodyMiddleware,
        max_bytes=resolved_settings.api_max_request_body_bytes,
    )

    @application.middleware("http")
    async def guard_and_observe(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """驗證 service key、建立 request context，並記錄低 cardinality metrics。"""

        request_id = _request_id(request)
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request_id_token = request_id_context.set(request_id)
        started = time.perf_counter()
        status_code = 500
        response: Response
        try:
            # health/metrics 保持可供監控探測；商業 API 才要求內部服務金鑰。
            if request.url.path.startswith("/api/"):
                configured_key = resolved_settings.api_internal_key
                if configured_key and not hmac.compare_digest(
                    configured_key,
                    request.headers.get("x-internal-api-key", ""),
                ):
                    response = _error_response(
                        request,
                        status_code=401,
                        error="unauthorized",
                        message="A valid internal API key is required",
                    )
                    status_code = response.status_code
                    return response

            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            logger.exception("middleware_unhandled_exception", error_type=type(exc).__name__)
            response = _error_response(
                request,
                status_code=500,
                error="internal_error",
                message="Unexpected server error",
            )
            status_code = response.status_code
            return response
        finally:
            # finally 無論成功或例外都執行，確保 metrics 與 ContextVar 一定被清理。
            route = request.scope.get("route")
            path = str(getattr(route, "path", "__unmatched__"))
            request_count.labels(request.method, path, str(status_code)).inc()
            request_duration.labels(request.method, path).observe(time.perf_counter() - started)
            if "response" in locals():
                response.headers["x-request-id"] = request_id
            request_id_context.reset(request_id_token)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            error="invalid_request",
            message="Request validation failed",
            details=_validation_details(exc),
        )

    @application.exception_handler(DomainValidationError)
    async def invalid_domain(request: Request, exc: DomainValidationError) -> JSONResponse:
        return _error_response(
            request,
            status_code=422,
            error="invalid_request",
            message=str(exc),
        )

    @application.exception_handler(UnauthorizedPrincipalError)
    async def unauthorized_principal(
        request: Request,
        exc: UnauthorizedPrincipalError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=401,
            error="unauthorized_principal",
            message=str(exc),
        )

    @application.exception_handler(DependencyContractError)
    async def dependency_contract(
        request: Request,
        exc: DependencyContractError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=502,
            error="dependency_contract_error",
            message=str(exc),
        )

    @application.exception_handler(DependencyUnavailableError)
    async def dependency_unavailable(
        request: Request,
        exc: DependencyUnavailableError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            error="dependency_unavailable",
            message=str(exc),
        )

    @application.exception_handler(DependencyTimeoutError)
    async def dependency_timeout(
        request: Request,
        exc: DependencyTimeoutError,
    ) -> JSONResponse:
        return _error_response(
            request,
            status_code=504,
            error="dependency_timeout",
            message=str(exc),
        )

    @application.exception_handler(RetrievalError)
    async def expected_failure(request: Request, exc: RetrievalError) -> JSONResponse:
        return _error_response(
            request,
            status_code=400,
            error="retrieval_error",
            message=str(exc),
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
        return HealthResponse(status="ok", checks={"process": True})

    @application.get("/health/ready", response_model=HealthResponse)
    async def readiness(request: Request) -> JSONResponse:
        """Readiness 檢查 KIS 連線與 KIS service-key，不只是本 process 存活。"""

        checks = await _runtime(request).readiness_checks()
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content=HealthResponse(
                status="ok" if ready else "not_ready",
                checks=checks,
            ).model_dump(mode="json"),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @application.post("/api/v1/retrieve", response_model=RetrieveResponse)
    async def retrieve(body: RetrieveRequest, request: Request) -> RetrieveResponse:
        """把 HTTP request 轉成 Domain query/principal，再執行 RetrieveEvidence。"""

        limit = body.limit or resolved_settings.default_retrieval_limit
        if limit > resolved_settings.max_retrieval_limit:
            raise DomainValidationError(
                f"limit must not exceed {resolved_settings.max_retrieval_limit}"
            )
        principal = await _runtime(request).identity_provider.resolve(
            IdentityInput(
                bearer_token=_bearer_token(request),
                development_user_id=request.headers.get("x-dev-user-id"),
                development_group_ids=_development_groups(request),
            )
        )
        result = await _runtime(request).retrieve_evidence.execute(
            RetrievalQuery(
                text=body.query,
                limit=limit,
                language=body.language,
            ),
            principal,
        )
        # 只有允許外送的 llm_context 才能取得封條。若簽章失敗會 fail closed，
        # 不會把 unsigned context 回給未來的 Chat/Answer Service。
        context_attestation = None
        if result.llm_context:
            context_attestation = _runtime(request).context_attestor.issue(
                RetrievalQuery(text=body.query, limit=limit, language=body.language),
                result.llm_context,
            )
        result_count.labels(result.status.value).inc()
        candidate_count.observe(result.candidate_count)
        selected_count.observe(result.selected_count)
        logger.info(
            "retrieval_completed",
            status=result.status.value,
            candidate_count=result.candidate_count,
            selected_count=result.selected_count,
            llm_context_count=len(result.llm_context),
            restricted_evidence_count=len(result.restricted_evidence),
        )
        return _retrieve_response(result, context_attestation=context_attestation)

    # Add CORS last so Starlette places it outside authentication/body guards.
    # That lets it answer OPTIONS preflights and attach CORS headers to guarded
    # error responses without weakening the actual API-key check.
    if resolved_settings.cors_origins:
        allowed_headers = [
            "authorization",
            "content-type",
            "x-internal-api-key",
            "x-request-id",
        ]
        if resolved_settings.app_env != "production":
            allowed_headers.extend(["x-dev-user-id", "x-dev-group-ids"])
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=allowed_headers,
        )

    return application


def _runtime(request: Request) -> Runtime:
    """從 FastAPI app.state 取出 lifespan 建立的強型別 Runtime。"""

    return cast(Runtime, request.app.state.runtime)


def _request_id(request: Request) -> str:
    """接受合理長度的上游 request ID，否則產生新的 UUID。"""

    supplied = request.headers.get("x-request-id", "").strip()
    if supplied and len(supplied) <= 128:
        return supplied
    return str(uuid4())


def _bearer_token(request: Request) -> str | None:
    """解析標準 ``Authorization: Bearer ...``，格式錯誤時立即拒絕。"""

    authorization = request.headers.get("authorization")
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token.strip():
        raise UnauthorizedPrincipalError("Authorization must use a Bearer token")
    return token.strip()


def _development_groups(request: Request) -> tuple[str, ...]:
    """將逗號分隔的開發群組 header 轉成不可變 tuple。"""

    raw_groups = request.headers.get("x-dev-group-ids", "")
    if not raw_groups.strip():
        return ()
    return tuple(group.strip() for group in raw_groups.split(","))


def _validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    """只挑選安全且穩定的 Pydantic validation 欄位回傳給 Client。"""

    return [
        {
            "type": str(error.get("type", "validation_error")),
            "location": [str(part) for part in error.get("loc", ())],
            "message": str(error.get("msg", "Invalid value")),
        }
        for error in exc.errors()
    ]


def _retrieve_response(
    result: RetrievalResult,
    *,
    context_attestation: str | None,
) -> RetrieveResponse:
    """把 Domain result 映射成對外 schema，不直接序列化 Domain 內部實作。"""

    return RetrieveResponse(
        status=result.status,
        llm_context=[EvidenceResponse.model_validate(item) for item in result.llm_context],
        restricted_evidence=[
            EvidenceResponse.model_validate(item) for item in result.restricted_evidence
        ],
        external_generation_blocked=result.external_generation_blocked,
        context_attestation=context_attestation,
        retrieval=RetrievalDiagnosticsResponse(
            candidate_count=result.candidate_count,
            selected_count=result.selected_count,
            model_id=result.model_id,
            model_revision=result.model_revision,
        ),
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    error: str,
    message: str,
    details: list[dict[str, object]] | None = None,
) -> JSONResponse:
    """建立所有錯誤共用的 JSON envelope 與 request ID。"""

    content: dict[str, object] = {
        "error": error,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if details is not None:
        content["details"] = details
    return JSONResponse(status_code=status_code, content=content)


app = create_app()
