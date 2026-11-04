"""Answer Generation FastAPI application factory。"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import RequestResponseEndpoint

from answer_generation_service.application.dtos import GenerateAnswerCommand
from answer_generation_service.bootstrap import Runtime, build_runtime
from answer_generation_service.domain.entities import (
    ApprovedContext,
    ApprovedEvidence,
    Governance,
    Question,
    SafeCitation,
)
from answer_generation_service.domain.exceptions import (
    DomainValidationError,
    OutboundPolicyBlockedError,
    ProviderContractError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    UnauthorizedContextError,
)
from answer_generation_service.infrastructure.config import Settings, get_settings
from answer_generation_service.infrastructure.logging import configure_logging
from answer_generation_service.presentation.middleware import BoundedRequestBodyMiddleware
from answer_generation_service.presentation.schemas import (
    AnswerPartResponse,
    CitationResponse,
    ErrorResponse,
    GenerateAnswerRequest,
    GenerateAnswerResponse,
    GenerationDiagnosticsResponse,
    HealthResponse,
)

logger = structlog.get_logger(__name__)


def create_app(*, settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    registry = CollectorRegistry()
    request_count = Counter(
        "answer_generation_http_requests_total",
        "HTTP requests handled by answer-generation-service.",
        ("method", "path", "status"),
        registry=registry,
    )
    request_duration = Histogram(
        "answer_generation_http_request_duration_seconds",
        "HTTP request latency.",
        ("method", "path"),
        registry=registry,
    )
    result_count = Counter(
        "answer_generation_results_total",
        "Grounded generation results.",
        ("status",),
        registry=registry,
    )
    policy_block_count = Counter(
        "answer_generation_policy_blocks_total",
        "Outbound policy blocks by non-sensitive category.",
        ("category",),
        registry=registry,
    )
    evidence_count = Histogram(
        "answer_generation_evidence_count",
        "Approved evidence items supplied for generation.",
        registry=registry,
    )
    active_generation = Gauge(
        "answer_generation_active_requests",
        "Requests currently executing the generation use case.",
        registry=registry,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        active_runtime = runtime or build_runtime(resolved_settings)
        application.state.runtime = active_runtime
        try:
            yield
        finally:
            if runtime is None:
                await active_runtime.close()

    application = FastAPI(
        title="Answer Generation Service",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            401: {"model": ErrorResponse},
            403: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            422: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
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
    async def authenticate_and_observe(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500
        response: Response
        try:
            if request.url.path.startswith("/api/"):
                configured_key = resolved_settings.api_internal_key
                supplied_key = request.headers.get("x-internal-api-key", "")
                if not configured_key or not hmac.compare_digest(configured_key, supplied_key):
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
            route = request.scope.get("route")
            path = str(getattr(route, "path", "__unmatched__"))
            request_count.labels(request.method, path, str(status_code)).inc()
            request_duration.labels(request.method, path).observe(time.perf_counter() - started)
            if "response" in locals():
                response.headers["x-request-id"] = request_id

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

    @application.exception_handler(UnauthorizedContextError)
    async def invalid_attestation(request: Request, exc: UnauthorizedContextError) -> JSONResponse:
        return _error_response(
            request,
            status_code=403,
            error="context_attestation_invalid",
            message="The approved retrieval context could not be verified",
        )

    @application.exception_handler(OutboundPolicyBlockedError)
    async def policy_blocked(request: Request, exc: OutboundPolicyBlockedError) -> JSONResponse:
        for category in exc.categories:
            policy_block_count.labels(category).inc()
        logger.warning(
            "outbound_policy_blocked",
            categories=exc.categories,
            rule_ids=exc.rule_ids,
            policy_version=exc.policy_version,
        )
        return _error_response(
            request,
            status_code=403,
            error="outbound_policy_blocked",
            message="Question or evidence cannot be sent to the configured external AI provider",
            details={
                "categories": list(exc.categories),
                "rule_ids": list(exc.rule_ids),
                "policy_version": exc.policy_version,
            },
        )

    @application.exception_handler(ProviderRateLimitError)
    async def provider_rate_limited(request: Request, exc: ProviderRateLimitError) -> JSONResponse:
        response = _error_response(
            request,
            status_code=429,
            error="provider_rate_limited",
            message="Generation capacity is temporarily limited",
        )
        if exc.retry_after_seconds is not None:
            response.headers["retry-after"] = str(exc.retry_after_seconds)
        return response

    @application.exception_handler(ProviderContractError)
    async def provider_contract(request: Request, exc: ProviderContractError) -> JSONResponse:
        return _error_response(
            request,
            status_code=502,
            error="provider_contract_error",
            message="Generation provider returned an invalid response",
        )

    @application.exception_handler(ProviderUnavailableError)
    async def provider_unavailable(request: Request, exc: ProviderUnavailableError) -> JSONResponse:
        return _error_response(
            request,
            status_code=503,
            error="provider_unavailable",
            message="Generation provider is unavailable",
        )

    @application.exception_handler(ProviderTimeoutError)
    async def provider_timeout(request: Request, exc: ProviderTimeoutError) -> JSONResponse:
        return _error_response(
            request,
            status_code=504,
            error="provider_timeout",
            message="Generation provider timed out",
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
        checks = _runtime(request).readiness_checks()
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content=HealthResponse(status="ok" if ready else "not_ready", checks=checks).model_dump(
                mode="json"
            ),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @application.post("/api/v1/answers", response_model=GenerateAnswerResponse)
    async def generate_answer(
        body: GenerateAnswerRequest,
        request: Request,
    ) -> GenerateAnswerResponse:
        command = _command(body)
        active_generation.inc()
        try:
            result = await _runtime(request).generate_answer.execute(command)
        finally:
            active_generation.dec()
        result_count.labels(result.answer.status.value).inc()
        evidence_count.observe(len(command.context.evidence))
        logger.info(
            "answer_generation_completed",
            status=result.answer.status.value,
            evidence_count=len(command.context.evidence),
            citation_count=len(result.citations),
            provider=result.diagnostics.provider,
            model=result.diagnostics.model,
            prompt_version=result.diagnostics.prompt_version,
            finish_reason=result.diagnostics.finish_reason,
            prompt_tokens=result.diagnostics.prompt_tokens,
            completion_tokens=result.diagnostics.completion_tokens,
        )
        return GenerateAnswerResponse(
            status=result.answer.status,
            answer_parts=[AnswerPartResponse.model_validate(item) for item in result.answer.parts],
            citations=[CitationResponse.model_validate(item) for item in result.citations],
            generation=GenerationDiagnosticsResponse.model_validate(result.diagnostics),
        )

    return application


def _runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


def _command(body: GenerateAnswerRequest) -> GenerateAnswerCommand:
    """投影成 Domain command；刻意捨棄 transport-only 的 Retrieval score。

    score 只讓 Answer API 與 Retrieval Evidence contract 相容。它不進入 ApprovedEvidence，
    因而不能改變驗簽 canonical digest、Prompt 或回答內容。
    """

    return GenerateAnswerCommand(
        question=Question(text=body.question, language=body.language),
        context=ApprovedContext(
            evidence=tuple(
                ApprovedEvidence(
                    rank=item.rank,
                    chunk_id=item.chunk_id,
                    document_id=item.document_id,
                    version_id=item.version_id,
                    content=item.content,
                    document_type=item.document_type,
                    token_count=item.token_count,
                    citation=SafeCitation(
                        title=item.citation.title,
                        section_path=item.citation.section_path,
                        page_number=item.citation.page_number,
                    ),
                    governance=Governance(
                        confidentiality=item.governance.confidentiality,
                        language=item.governance.language,
                        external_llm_allowed=item.governance.external_llm_allowed,
                    ),
                )
                for item in body.evidence
            )
        ),
        context_attestation=body.context_attestation,
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "").strip()
    if supplied and len(supplied) <= 128:
        return supplied
    return str(uuid4())


def _validation_details(exc: RequestValidationError) -> list[dict[str, object]]:
    return [
        {
            "type": str(error.get("type", "validation_error")),
            "location": [str(part) for part in error.get("loc", ())],
            "message": str(error.get("msg", "Invalid value")),
        }
        for error in exc.errors()
    ]


def _error_response(
    request: Request,
    *,
    status_code: int,
    error: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    content: dict[str, Any] = {
        "error": error,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if details is not None:
        content["details"] = details
    return JSONResponse(status_code=status_code, content=content)


app = create_app()
