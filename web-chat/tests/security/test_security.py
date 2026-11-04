"""XSS、body limit、無任意 proxy path 與敏感輸出 security tests。"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from web_chat.presentation.app import create_app


async def call(app, method: str, url: str, **kwargs: object) -> httpx.Response:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8130"
        ) as client:
            return await client.request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_security_headers_and_dynamic_text_is_json(settings, fake_gateway) -> None:
    app = create_app(settings=settings, gateway=fake_gateway)
    response = await call(
        app,
        "POST",
        "/api/v1/conversations",
        headers={"content-type": "application/json"},
        json={"title": "<script>alert(1)</script>"},
    )
    assert response.status_code == 201
    assert "<script>" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


@pytest.mark.asyncio
async def test_non_json_and_oversized_body_are_rejected(settings, fake_gateway) -> None:
    app = create_app(settings=settings, gateway=fake_gateway)
    response = await call(app, "POST", "/api/v1/conversations", content=b"{}")
    assert response.status_code == 415
    huge = b"{" + b"x" * 70_000 + b"}"
    response = await call(
        app,
        "POST",
        "/api/v1/conversations",
        headers={"content-type": "application/json"},
        content=huge,
    )
    assert response.status_code == 413


def test_static_assets_do_not_render_dynamic_html() -> None:
    static_dir = Path(__file__).parents[2] / "src" / "web_chat" / "static"
    javascript = (static_dir / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in javascript
    assert "textContent" in javascript
    assert "/api/v1/conversations" in javascript
    assert "Idempotency-Key" in javascript
