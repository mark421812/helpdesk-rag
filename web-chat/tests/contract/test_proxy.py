"""Web proxy route contract and error mapping tests。"""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from web_chat.presentation.app import create_app


async def request_app(app, method: str, url: str, **kwargs: object) -> httpx.Response:
    """在 test lifespan 內呼叫 ASGI app。"""

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8130",
        ) as client:
            return await client.request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_create_turn_processing_and_polling(settings, fake_gateway) -> None:
    app = create_app(settings=settings, gateway=fake_gateway)
    response = await request_app(
        app,
        "POST",
        "/api/v1/conversations",
        headers={"content-type": "application/json"},
        json={"title": None},
    )
    assert response.status_code == 201
    conversation_id = response.json()["conversation_id"]
    key = str(uuid4())
    response = await request_app(
        app,
        "POST",
        f"/api/v1/conversations/{conversation_id}/turns",
        headers={"content-type": "application/json", "Idempotency-Key": key},
        json={"question": "測試問題", "language": "zh-TW"},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "processing"
    assert response.json()["status_url"].startswith("/api/v1/conversations/")


@pytest.mark.asyncio
async def test_route_rejects_unknown_fields_missing_key_and_foreign_origin(
    settings, fake_gateway
) -> None:
    app = create_app(settings=settings, gateway=fake_gateway)
    response = await request_app(
        app,
        "POST",
        "/api/v1/conversations",
        headers={"content-type": "application/json", "origin": "https://evil.example"},
        json={"title": None},
    )
    assert response.status_code == 403
    response = await request_app(
        app,
        "POST",
        "/api/v1/conversations",
        headers={"content-type": "application/json"},
        json={"title": None, "user_id": "forged"},
    )
    assert response.status_code == 422
    response = await request_app(
        app,
        "POST",
        f"/api/v1/conversations/{uuid4()}/turns",
        headers={"content-type": "application/json"},
        json={"question": "測試問題", "language": "zh-TW"},
    )
    assert response.status_code == 422
