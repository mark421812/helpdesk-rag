"""Ingestion FastAPI application factory、middleware、routes 與 error mapping。"""

from __future__ import annotations

import hmac
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID, uuid4

import structlog
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from starlette.middleware.base import RequestResponseEndpoint

from ingestion_service.bootstrap import Runtime, build_runtime, ensure_default_source
from ingestion_service.domain.enums import SourceItemStatus, SyncTrigger
from ingestion_service.domain.exceptions import (
    ConflictError,
    DomainError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from ingestion_service.infrastructure.config import Settings, get_settings
from ingestion_service.infrastructure.logging import configure_logging
from ingestion_service.presentation.schemas import (
    ErrorResponse,
    HealthResponse,
    IngestFileRequest,
    IngestFileResponse,
    IngestionJobResponse,
    ScanSourceResponse,
    SourceItemResponse,
    SourceResponse,
    SyncRunResponse,
)

logger = structlog.get_logger(__name__)
_PUBLIC_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


def create_app(
    *,
    settings: Settings | None = None,
    runtime: Runtime | None = None,
) -> FastAPI:
    """建立 FastAPI app；測試可傳入 fake Runtime，正式環境則自行組裝依賴。"""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)
    registry = CollectorRegistry()
    request_count = Counter(
        "ingestion_http_requests_total",
        "Number of HTTP requests handled by ingestion-service.",
        ("method", "path", "status"),
        registry=registry,
    )
    request_duration = Histogram(
        "ingestion_http_request_duration_seconds",
        "Time spent handling ingestion-service HTTP requests.",
        ("method", "path"),
        registry=registry,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # lifespan 的 yield 前是 startup，yield 後（finally）是 shutdown。
        if runtime is not None:
            application.state.runtime = runtime
            yield
            return
        owned_runtime = build_runtime(resolved_settings)
        try:
            await ensure_default_source(owned_runtime)
            application.state.runtime = owned_runtime
            yield
        finally:
            await owned_runtime.close()

    application = FastAPI(
        title="Ingestion Service",
        version="0.1.0",
        lifespan=lifespan,
    )
    if resolved_settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["content-type", "x-internal-api-key", "x-request-id"],
        )

    @application.middleware("http")
    async def limit_request_body(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # decorator 將內部函式註冊成 middleware；request 會依註冊順序流經它們。
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)

        limit = resolved_settings.api_max_request_body_bytes
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    return _request_body_too_large(request, limit)
            except ValueError:
                # Uvicorn normally rejects malformed Content-Length first. If a
                # different ASGI server forwards it, the streaming limit below
                # remains authoritative.
                pass

        body_parts: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                return _request_body_too_large(request, limit)
            body_parts.append(chunk)

        # BaseHTTPMiddleware replays this cached body to FastAPI's request parser.
        request._body = b"".join(body_parts)
        return await call_next(request)

    @application.middleware("http")
    async def internal_api_key(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # compare_digest 避免一般字串比較可能洩漏的 timing 差異。
        configured_key = resolved_settings.api_internal_key
        if configured_key and request.url.path not in _PUBLIC_PATHS:
            supplied_key = request.headers.get("x-internal-api-key", "")
            if not hmac.compare_digest(configured_key, supplied_key):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "unauthorized",
                        "message": "A valid internal API key is required",
                        "request_id": getattr(request.state, "request_id", None),
                    },
                )
        return await call_next(request)

    @application.middleware("http")
    async def observe_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        # finally 無論 handler 成功或拋錯都會記錄 latency/HTTP status metrics。
        request_id = _request_id(request)
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["x-request-id"] = request_id
            return response
        finally:
            route = request.scope.get("route")
            path = str(getattr(route, "path", "__unmatched__"))
            request_count.labels(request.method, path, str(status_code)).inc()
            request_duration.labels(request.method, path).observe(time.perf_counter() - started)

    @application.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error_response(request, exc, 404)

    @application.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _error_response(request, exc, 409)

    @application.exception_handler(ValidationError)
    async def invalid(request: Request, exc: ValidationError) -> JSONResponse:
        return _error_response(request, exc, 422)

    @application.exception_handler(ExternalServiceError)
    async def unavailable(request: Request, exc: ExternalServiceError) -> JSONResponse:
        return _error_response(request, exc, 503 if exc.retryable else 502)

    @application.exception_handler(DomainError)
    async def domain_failure(request: Request, exc: DomainError) -> JSONResponse:
        return _error_response(request, exc, 400)

    @application.get("/health/live", response_model=HealthResponse)
    async def liveness() -> HealthResponse:
        return HealthResponse(status="ok", checks={"process": True})

    @application.get("/health/ready", response_model=HealthResponse)
    async def readiness(request: Request) -> JSONResponse:
        checks = await _runtime(request).readiness_checks()
        ready = all(checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content=HealthResponse(
                status="ready" if ready else "not_ready",
                checks=checks,
            ).model_dump(mode="json"),
        )

    @application.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @application.get("/api/v1/sources", response_model=list[SourceResponse])
    async def list_sources(request: Request) -> list[SourceResponse]:
        sources = await _runtime(request).list_enabled_sources()
        return [SourceResponse.model_validate(source) for source in sources]

    @application.post(
        "/api/v1/sources/{source_id}/sync",
        response_model=ScanSourceResponse,
        status_code=202,
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def sync_source(
        source_id: UUID,
        request: Request,
        allow_mass_deletion: bool = False,
    ) -> ScanSourceResponse:
        result = await _runtime(request).scan_source.execute(
            source_id,
            SyncTrigger.MANUAL,
            allow_mass_deletion=allow_mass_deletion,
        )
        logger.info(
            "source_scan_accepted",
            source_id=str(source_id),
            run_id=str(result.run_id),
            allow_mass_deletion=allow_mass_deletion,
        )
        return ScanSourceResponse.model_validate(result)

    @application.post(
        "/api/v1/sources/{source_id}/files/ingest",
        response_model=IngestFileResponse,
        status_code=202,
    )
    async def ingest_file(
        source_id: UUID,
        body: IngestFileRequest,
        request: Request,
    ) -> IngestFileResponse:
        result = await _runtime(request).ingest_file.execute(
            source_id,
            body.source_item_id,
            SyncTrigger.MANUAL,
        )
        logger.info(
            "file_ingestion_accepted",
            source_id=str(source_id),
            source_item_id=body.source_item_id,
            run_id=str(result.run_id),
            job_id=str(result.job_id) if result.job_id else None,
        )
        return IngestFileResponse.model_validate(result)

    @application.get("/api/v1/runs/{run_id}", response_model=SyncRunResponse)
    async def get_run(run_id: UUID, request: Request) -> SyncRunResponse:
        result = await _runtime(request).get_run.execute(run_id)
        return SyncRunResponse.model_validate(result)

    @application.get("/api/v1/items", response_model=list[SourceItemResponse])
    async def list_items(
        request: Request,
        source_id: UUID | None = None,
        status: SourceItemStatus | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[SourceItemResponse]:
        results = await _runtime(request).list_items.execute(
            source_id=source_id,
            status=status,
            offset=offset,
            limit=limit,
        )
        return [SourceItemResponse.model_validate(result) for result in results]

    @application.get("/api/v1/items/{item_id}", response_model=SourceItemResponse)
    async def get_item(item_id: UUID, request: Request) -> SourceItemResponse:
        result = await _runtime(request).get_item.execute(item_id)
        return SourceItemResponse.model_validate(result)

    @application.get("/api/v1/jobs/{job_id}", response_model=IngestionJobResponse)
    async def get_job(job_id: UUID, request: Request) -> IngestionJobResponse:
        result = await _runtime(request).get_job.execute(job_id)
        return IngestionJobResponse.model_validate(result)

    @application.post(
        "/api/v1/jobs/{job_id}/retry",
        response_model=IngestionJobResponse,
        status_code=202,
    )
    async def retry_job(job_id: UUID, request: Request) -> IngestionJobResponse:
        result = await _runtime(request).retry_job.execute(job_id)
        return IngestionJobResponse.model_validate(result)

    return application


def _runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


def _request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id", "").strip()
    if supplied and len(supplied) <= 128 and all(character.isprintable() for character in supplied):
        return supplied
    return str(uuid4())


def _error_response(request: Request, exc: DomainError, status_code: int) -> JSONResponse:
    payload = ErrorResponse(
        error=type(exc).__name__,
        message=str(exc),
        request_id=getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump(mode="json"))


def _request_body_too_large(request: Request, limit: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": "request_body_too_large",
            "message": f"Request body exceeds the {limit}-byte limit",
            "request_id": getattr(request.state, "request_id", None),
        },
    )


app = create_app()
