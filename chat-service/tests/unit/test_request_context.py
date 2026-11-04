"""Request context 的最小 isolation contract。"""

from __future__ import annotations

from chat_service.infrastructure.request_context import request_id_context


def test_request_id_context_defaults_and_restores() -> None:
    assert request_id_context.get() is None

    token = request_id_context.set("request-123")
    try:
        assert request_id_context.get() == "request-123"
    finally:
        request_id_context.reset(token)

    assert request_id_context.get() is None
