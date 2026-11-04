"""Shared test helpers; all fake downstream responses live under tests only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest

from web_chat.application.dtos import GatewayResult
from web_chat.infrastructure.config import Settings


@pytest.fixture
def settings() -> Settings:
    """測試使用 test environment，仍通過 production-like URL/id 驗證。"""

    return Settings(app_env="test", chat_service_url="http://chat-api:8120")


def conversation_payload() -> dict[str, object]:
    """產生符合 Chat public contract 的 deterministic fixture。"""

    return {
        "conversation_id": str(uuid4()),
        "title": None,
        "status": "active",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "last_activity_at": "2026-01-01T00:00:00Z",
    }


def turn_payload(
    *, state: str = "completed", outcome: str | None = "answered"
) -> dict[str, object]:
    """建立 200/202 共用的安全 turn fixture。"""

    conversation_id = uuid4()
    turn_id = uuid4()
    payload: dict[str, object] = {
        "conversation_id": str(conversation_id),
        "turn_id": str(turn_id),
        "state": state,
        "outcome": outcome,
        "user_message": None,
        "assistant_message": None,
        "error": None,
        "request_id": str(uuid4()),
        "status_url": f"/api/v1/conversations/{conversation_id}/turns/{turn_id}",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }
    if state == "processing":
        payload["outcome"] = None
        return payload
    payload["user_message"] = {
        "message_id": str(uuid4()),
        "role": "user",
        "content": "測試問題",
        "response_kind": None,
        "answer_parts": None,
        "citations": [],
        "created_at": "2026-01-01T00:00:00Z",
    }
    if outcome == "answered":
        payload["assistant_message"] = {
            "message_id": str(uuid4()),
            "role": "assistant",
            "content": "安全回答",
            "response_kind": "answered",
            "answer_parts": [{"text": "安全回答", "citation_ids": ["C1"]}],
            "citations": [
                {
                    "citation_id": "C1",
                    "title": "測試規章",
                    "section_path": "第一條",
                    "page_number": 1,
                }
            ],
            "created_at": "2026-01-01T00:00:00Z",
        }
    return payload


class FakeGateway:
    """只供 presentation tests 使用的 application port fake。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.next_turn = turn_payload(state="processing", outcome=None)

    async def create_conversation(self, *, title: str | None, request_id: str) -> GatewayResult:
        self.calls.append(("create", {"title": title, "request_id": request_id}))
        return GatewayResult(status_code=201, payload=conversation_payload())

    async def submit_turn(
        self,
        *,
        conversation_id: UUID,
        idempotency_key: str,
        question: str,
        language: str | None,
        request_id: str,
    ) -> GatewayResult:
        self.calls.append(
            (
                "submit",
                {
                    "conversation_id": conversation_id,
                    "idempotency_key": idempotency_key,
                    "question": question,
                    "language": language,
                    "request_id": request_id,
                },
            )
        )
        return GatewayResult(status_code=202, payload=self.next_turn)

    async def get_turn(
        self,
        *,
        conversation_id: UUID,
        turn_id: UUID,
        request_id: str,
    ) -> GatewayResult:
        self.calls.append(("get", {"conversation_id": conversation_id, "turn_id": turn_id}))
        return GatewayResult(status_code=200, payload=turn_payload())

    async def readiness(self, *, request_id: str) -> bool:
        self.calls.append(("ready", {"request_id": request_id}))
        return True


@pytest.fixture
async def fake_gateway() -> AsyncIterator[FakeGateway]:
    """提供獨立 fake，避免測試互相共享狀態。"""

    yield FakeGateway()
