"""Phase 6 Presentation API contract tests。

這些測試使用 framework-free application fake，不建立另一套 persistence；真正 PostgreSQL
transaction 仍由 tests/integration 的 Testcontainers fixture 驗證。重點是 employee response
projection、identity boundary、strict body schema、cursor codec 與 health/metrics transport。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from chat_service.application.dtos import ConversationSummary, ListConversationsResult
from chat_service.domain.enums import ConversationStatus
from chat_service.infrastructure.config import Settings
from chat_service.infrastructure.identity.development import DevelopmentHeaderIdentityProvider
from chat_service.presentation.app import create_app

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _CreateConversation:
    async def execute(self, _command: object, _principal: object) -> ConversationSummary:
        return ConversationSummary(uuid4(), None, ConversationStatus.ACTIVE, NOW, NOW, NOW)


class _ListConversations:
    async def execute(self, _query: object) -> ListConversationsResult:
        return ListConversationsResult((), None)


class _Runtime:
    identity_provider = DevelopmentHeaderIdentityProvider()
    create_conversation = _CreateConversation()
    list_conversations = _ListConversations()

    async def readiness_checks(self) -> dict[str, bool]:
        return {"process": True, "postgres": True, "retrieval": True, "answer_generation": True}


def _app(*, body_limit: int = 65_536):
    settings = Settings(_env_file=None, api_max_request_body_bytes=body_limit)
    runtime = _Runtime()
    for name in (
        "get_conversation",
        "update_conversation",
        "submit_turn",
        "get_turn",
        "list_messages",
        "preview_conversation_purge",
        "execute_conversation_purge",
    ):
        setattr(runtime, name, SimpleNamespace())
    return create_app(settings=settings, runtime=runtime)


def test_employee_routes_require_identity_and_reject_extra_body_fields() -> None:
    with TestClient(_app()) as client:
        missing = client.get("/api/v1/conversations")
        assert missing.status_code == 401
        assert missing.json()["error"] == "unauthorized_principal"

        extra = client.post(
            "/api/v1/conversations",
            json={"title": None, "user_id": "forged"},
            headers={"x-dev-user-id": "alice"},
        )
        assert extra.status_code == 422
        assert "forged" not in extra.text

        created = client.post(
            "/api/v1/conversations",
            json={"title": None},
            headers={"x-dev-user-id": "alice"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "active"
        assert "owner_subject" not in created.json()


def test_body_limit_request_id_and_security_headers() -> None:
    with TestClient(_app(body_limit=8)) as client:
        response = client.post(
            "/api/v1/conversations",
            content=b"123456789",
            headers={"x-dev-user-id": "alice", "x-request-id": "x" * 129},
        )
        assert response.status_code == 413
        assert response.json()["error"] == "request_too_large"
        assert len(response.headers["x-request-id"]) == 36
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"


def test_health_and_metrics_are_safe() -> None:
    with TestClient(_app()) as client:
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "ok", "checks": {"process": True}}

        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["process"] is True

        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "chat_http_requests_total" in metrics.text
        assert "alice" not in metrics.text


def test_unknown_http_route_uses_the_safe_error_envelope() -> None:
    with TestClient(_app()) as client:
        response = client.get("/not-a-chat-route")

    assert response.status_code == 404
    assert response.json()["error"] == "resource_not_found"
    assert response.json()["details"] is None
    assert "detail" not in response.json()


def test_security_headers_cannot_be_overridden_by_a_route() -> None:
    application = _app()

    @application.get("/header-overrides")
    async def header_overrides() -> PlainTextResponse:
        return PlainTextResponse(
            "ok",
            headers={
                "x-request-id": "forged",
                "x-content-type-options": "sniff",
                "x-frame-options": "ALLOWALL",
                "referrer-policy": "unsafe-url",
                "cache-control": "public",
            },
        )

    with TestClient(application) as client:
        response = client.get("/header-overrides")

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "forged"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
