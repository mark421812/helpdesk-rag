"""FastAPI composition root 與最小 Web Chat HTTP surface。

Route 只負責驗證 client schema、讀取 server-generated request ID 及映射安全結果；所有下游 URL、
identity header、timeout 與 response schema 都封裝在 infrastructure gateway。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from web_chat.application.dtos import GatewayFailure, GatewayResult
from web_chat.application.ports import ChatGatewayPort
from web_chat.infrastructure.chat_http import ChatHttpGateway
from web_chat.infrastructure.config import Settings, get_settings
from web_chat.presentation.middleware import RequestSecurityMiddleware
from web_chat.presentation.schemas import (
    ConversationResponse,
    CreateConversationRequest,
    ErrorResponse,
    SubmitTurnRequest,
    TurnResponse,
)

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _request_id(request: Request) -> str:
    """只取 middleware 產生的 UUID，不接受瀏覽器指定的 request ID。"""

    return str(request.scope.get("state", {}).get("request_id", "unknown"))


def _result_response(result: GatewayResult) -> JSONResponse:
    """回傳已由 gateway schema 驗證的 JSON；Retry-After 僅傳有界整數。"""

    headers = (
        {}
        if result.retry_after_seconds is None
        else {"Retry-After": str(result.retry_after_seconds)}
    )
    return JSONResponse(status_code=result.status_code, content=result.payload, headers=headers)


def _failure_response(failure: GatewayFailure, request: Request) -> JSONResponse:
    """把下游失敗轉為固定 envelope；絕不暴露 URL、raw body 或 exception。"""

    headers = (
        {}
        if failure.retry_after_seconds is None
        else {"Retry-After": str(failure.retry_after_seconds)}
    )
    payload = ErrorResponse(
        error=failure.error,
        message=failure.message,
        request_id=_request_id(request),
        details=None,
    ).model_dump(mode="json")
    return JSONResponse(status_code=failure.status_code, content=payload, headers=headers)


def _gateway(request: Request) -> ChatGatewayPort:
    """從 lifespan 注入 gateway，route 不直接建立 HTTP client。"""

    return cast(ChatGatewayPort, request.app.state.gateway)


def create_app(
    *, settings: Settings | None = None, gateway: ChatGatewayPort | None = None
) -> FastAPI:
    """建立可測試的 FastAPI application；production composition 只在 lifespan 組裝。"""

    resolved = settings or get_settings()
    injected_gateway = gateway

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client: httpx.AsyncClient | None = None
        active_gateway = injected_gateway
        if active_gateway is None:
            client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
            active_gateway = ChatHttpGateway(client=client, settings=resolved)
        app.state.gateway = active_gateway
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(
        title="Help Desk Demo Web Chat",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=resolved.host_allowlist)
    app.add_middleware(
        RequestSecurityMiddleware,
        max_body_bytes=resolved.api_max_request_body_bytes,
        allowed_origins={
            f"http://localhost:{resolved.web_chat_port}",
            f"http://127.0.0.1:{resolved.web_chat_port}",
        },
    )
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.exception_handler(GatewayFailure)
    async def gateway_failure(request: Request, exc: GatewayFailure) -> JSONResponse:
        return _failure_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        del exc
        return _failure_response(
            GatewayFailure(
                status_code=422,
                error="invalid_request",
                message="The request is invalid.",
            ),
            request,
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        """只回傳本專案的固定頁面，不接受任意檔案路徑。"""

        return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")

    @app.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, object]:
        """只表示 Web process 存活，不觸碰 Chat 或資料庫。"""

        return {"status": "ok", "checks": {"process": True}}

    @app.get("/health/ready", include_in_schema=False)
    async def readiness(request: Request) -> Response:
        """依 Chat readiness 結果 fail closed，回應不含內部 URL。"""

        ready = await _gateway(request).readiness(request_id=_request_id(request))
        status_code = 200 if ready else 503
        return JSONResponse(
            status_code=status_code,
            content={"status": "ok" if ready else "not_ready", "checks": {"chat_service": ready}},
        )

    @app.post("/api/v1/conversations", response_model=ConversationResponse, status_code=201)
    async def create_conversation(body: CreateConversationRequest, request: Request) -> Response:
        """建立對話；固定 title allowlist，demo identity 由 gateway server-side 注入。"""

        result = await _gateway(request).create_conversation(
            title=body.title,
            request_id=_request_id(request),
        )
        return _result_response(result)

    @app.post("/api/v1/conversations/{conversation_id}/turns", response_model=TurnResponse)
    async def submit_turn(
        conversation_id: UUID,
        body: SubmitTurnRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> Response:
        """送出問題；同一 logical retry 必須沿用相同的 idempotency key/body。"""

        if idempotency_key is None:
            raise GatewayFailure(
                status_code=422,
                error="invalid_idempotency_key",
                message="A valid Idempotency-Key UUID is required.",
            )
        key = idempotency_key.strip()
        if len(key) > 64:
            raise GatewayFailure(
                status_code=422,
                error="invalid_idempotency_key",
                message="A valid Idempotency-Key UUID is required.",
            )
        try:
            UUID(key)
        except ValueError:
            raise GatewayFailure(
                status_code=422,
                error="invalid_idempotency_key",
                message="A valid Idempotency-Key UUID is required.",
            ) from None
        result = await _gateway(request).submit_turn(
            conversation_id=conversation_id,
            idempotency_key=key,
            question=body.question,
            language=body.language,
            request_id=_request_id(request),
        )
        return _result_response(result)

    @app.get(
        "/api/v1/conversations/{conversation_id}/turns/{turn_id}",
        response_model=TurnResponse,
    )
    async def get_turn(conversation_id: UUID, turn_id: UUID, request: Request) -> Response:
        """只用 path UUID 查詢 turn，忽略 Chat response 的任意 status_url。"""

        result = await _gateway(request).get_turn(
            conversation_id=conversation_id,
            turn_id=turn_id,
            request_id=_request_id(request),
        )
        return _result_response(result)

    return app


app = create_app()
