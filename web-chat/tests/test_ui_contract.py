"""無瀏覽器依賴的 UI contract smoke；動態文字仍以 DOM textContent 建立。"""

from __future__ import annotations

import pytest


@pytest.mark.ui
def test_ui_has_required_controls_and_no_external_assets() -> None:
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "web_chat" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="messages"' in html
    assert 'id="question"' in html
    assert 'id="send"' in html
    assert 'id="new-conversation"' in html
    assert 'id="confirm-request"' in html
    assert "https://" not in html
    assert "setTimeout" in js
    assert "crypto.randomUUID" in js
    assert "textContent" in js
