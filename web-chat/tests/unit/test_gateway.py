"""HTTP gateway body/content-type/redirect/identity tests。"""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from web_chat.application.dtos import GatewayFailure
from web_chat.infrastructure.chat_http import ChatHttpGateway


@pytest.mark.asyncio
async def test_gateway_projects_request_and_validates_response(settings) -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/conversations"):
            return httpx.Response(
                201,
                headers={"content-type": "application/json"},
                json={
                    "conversation_id": str(uuid4()),
                    "title": None,
                    "status": "active",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "last_activity_at": "2026-01-01T00:00:00Z",
                },
            )
        return httpx.Response(
            500,
            headers={"content-type": "application/json"},
            json={"secret": "hidden"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    ) as client:
        gateway = ChatHttpGateway(client=client, settings=settings)
        result = await gateway.create_conversation(title=None, request_id="request-1")
    assert result.status_code == 201
    assert seen[0].headers["x-dev-user-id"] == "web-chat-demo-user"
    assert seen[0].headers["x-dev-group-ids"] == "all-employees"
    assert json.loads(seen[0].content) == {"title": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, headers={"content-type": "text/html"}, text="bad"),
        httpx.Response(200, headers={"content-type": "application/json"}, text="not-json"),
        httpx.Response(
            302,
            headers={"content-type": "application/json", "location": "http://evil"},
            json={},
        ),
        httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "5000001"},
            content=b"{}",
        ),
    ],
)
async def test_gateway_rejects_unsafe_upstream_response(settings, response: httpx.Response) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    ) as client:
        gateway = ChatHttpGateway(client=client, settings=settings)
        with pytest.raises(GatewayFailure) as raised:
            await gateway.create_conversation(title=None, request_id="request-1")
    assert raised.value.status_code == 502
    assert "secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_gateway_maps_status_without_retry(settings) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"content-type": "application/json", "retry-after": "99"},
            json={"error": "raw-secret", "message": "raw-body"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False
    ) as client:
        gateway = ChatHttpGateway(client=client, settings=settings)
        with pytest.raises(GatewayFailure) as raised:
            await gateway.submit_turn(
                conversation_id=uuid4(),
                idempotency_key=str(uuid4()),
                question="question",
                language="zh-TW",
                request_id="request-1",
            )
    assert calls == 1
    assert raised.value.status_code == 503
    assert "raw" not in str(raised.value)
