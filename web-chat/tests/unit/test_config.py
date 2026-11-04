"""設定與 URL/header validation unit tests。"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from web_chat.infrastructure.config import Settings


def test_defaults_and_group_header() -> None:
    settings = Settings(app_env="test")
    assert settings.web_chat_port == 8130
    assert settings.chat_service_url == "http://chat-api:8120"
    assert settings.group_header == "all-employees"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"app_env": "production"},
        {"chat_service_url": "http://user:secret@chat-api:8120"},
        {"chat_service_url": "http://chat-api:8120?secret=1"},
        {"chat_service_url": "ftp://chat-api:8120"},
        {"demo_user_id": "demo\nuser"},
        {"demo_group_ids": "all-employees,all-employees"},
        {"allowed_hosts": "*"},
        {"upstream_timeout_seconds": math.inf},
    ],
)
def test_invalid_runtime_settings_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        values = {"app_env": "test", **kwargs}
        Settings(**values)
