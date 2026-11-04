"""KIS FastAPI application factory、middleware、health checks 與 error mapping。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hmac import compare_digest
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

from knowledge_index_service.bootstrap import Container, create_container
from knowledge_index_service.domain.exceptions import (
    CapacityExceededError,
    ConflictError,
    DomainError,
    EntityNotFoundError,
    ServiceNotReadyError,
    ValidationError,
)
from knowledge_index_service.infrastructure.config import Settings, get_settings
from knowledge_index_service.infrastructure.logging import configure_logging
from knowledge_index_service.presentation.routes import router
from knowledge_index_service.presentation.schemas import ErrorResponse, HealthResponse

REQUESTS = Counter(
    "knowledge_index_http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)
LATENCY = Histogram(
    "knowledge_index_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """建立 KIS FastAPI app；Settings 可注入以便測試不同環境。"""

    resolved = settings or get_settings()
    configure_logging(resolved.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """startup 建 Container，shutdown 關閉 DB/Qdrant resources。"""

        container = create_container(resolved)
        app.state.container = container
        yield
        await container.close()

    app = FastAPI(
        title="Knowledge Index Service",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            409: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    if resolved.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """集中處理 request ID、body 上限、內部 API key 與 metrics。"""

        started = time.perf_counter()
        request_id = request.headers.get("x-request-id") or str(uuid4())
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        content_length = request.headers.get("content-length")
        # JSONResponse 與 call_next 回傳的型別不同，但兩者皆符合 Starlette
        # Response；先標註共同基底，讓各分支維持一致的靜態型別。
        response: Response
        if (
            content_length is not None
            and content_length.isdigit()
            and int(content_length) > resolved.max_request_body_bytes
        ):
            response = JSONResponse(
                status_code=413,
                content={
                    "code": "request_too_large",
                    "message": (
                        "Request body exceeds the configured "
                        f"{resolved.max_request_body_bytes}-byte limit"
                    ),
                    "request_id": request_id,
                },
            )
        elif (
            resolved.api_internal_key
            and request.url.path.startswith("/api/")
            and not compare_digest(
                request.headers.get("x-internal-api-key", ""),
                resolved.api_internal_key,
            )
        ):
            response = JSONResponse(
                status_code=401,
                content={
                    "code": "unauthorized",
                    "message": "Invalid or missing internal API key",
                    "request_id": request_id,
                },
            )
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, path, response.status_code).inc()
        LATENCY.labels(request.method, path).observe(time.perf_counter() - started)
        return response

    register_error_handlers(app)
    app.include_router(router)

    @app.get("/health/live", response_model=HealthResponse)
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", checks={"process": True})

    @app.get("/health/ready", response_model=HealthResponse)
    async def ready(request: Request) -> JSONResponse:
        container: Container = request.app.state.container
        checks = {
            "postgres": await _database_ready(container),
            "qdrant": await container.vector_store.is_ready(),
            "embedding_model": await container.embedding_model.is_ready(),
        }
        is_ready = all(checks.values())
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={"status": "ok" if is_ready else "not_ready", "checks": checks},
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def register_error_handlers(app: FastAPI) -> None:
    """把可預期 Domain exception 型別映射成穩定 HTTP status。"""

    statuses: dict[type[DomainError], int] = {
        ValidationError: 400,
        EntityNotFoundError: 404,
        ConflictError: 409,
        CapacityExceededError: 413,
        ServiceNotReadyError: 503,
    }

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status_code = next(
            (code for error_type, code in statuses.items() if isinstance(exc, error_type)),
            400,
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "code": type(exc).__name__,
                "message": str(exc),
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        structlog.get_logger().exception("unhandled_exception", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": "Unexpected server error",
                "request_id": getattr(request.state, "request_id", None),
            },
        )


async def _database_ready(container: Container) -> bool:
    """以廉價 ``SELECT 1`` 驗證 connection pool 能連到 PostgreSQL。"""

    try:
        async with container.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


# Uvicorn 以 ``module:app`` 載入這個 module-level application instance。
app = create_app()
