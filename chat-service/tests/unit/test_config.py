"""Phase 1 Settings 的預設值、範圍與 production fail-closed 測試。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chat_service.domain.entities import MAX_RETRY_AFTER_SECONDS
from chat_service.infrastructure.config import LOCAL_DEVELOPMENT_KEY, Settings


def _settings(**overrides: object) -> Settings:
    """測試不讀取 workspace 的 .env，避免本機環境污染結果。"""

    return Settings(_env_file=None, **overrides)


def test_defaults_match_phase_one_contract() -> None:
    settings = _settings()

    assert settings.app_name == "chat-service"
    assert settings.chat_api_port == 8120
    assert settings.chat_retrieval_limit == 8
    assert settings.retrieval_url == "http://retrieval-api:8100"
    assert settings.answer_generation_url == "http://answer-generation-api:8110"
    assert settings.retrieval_api_key == LOCAL_DEVELOPMENT_KEY
    assert settings.answer_generation_api_key == LOCAL_DEVELOPMENT_KEY
    assert settings.processing_stale_seconds == 360


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_api_port", 0),
        ("chat_retrieval_limit", 0),
        ("database_pool_size", 0),
        ("api_max_request_body_bytes", 0),
    ],
)
def test_positive_and_range_settings_are_validated(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_stale_recovery_window_must_cover_the_longest_request() -> None:
    with pytest.raises(ValidationError, match="processing_stale_seconds"):
        _settings(processing_stale_seconds=300)


def test_queue_wait_timeout_is_finite_and_bounded() -> None:
    settings = _settings(
        turn_queue_wait_timeout_seconds=MAX_RETRY_AFTER_SECONDS,
        processing_stale_seconds=MAX_RETRY_AFTER_SECONDS + 181,
    )
    assert settings.turn_queue_wait_timeout_seconds == MAX_RETRY_AFTER_SECONDS

    for invalid in (MAX_RETRY_AFTER_SECONDS + 1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            _settings(turn_queue_wait_timeout_seconds=invalid)


def test_dependency_timeout_must_not_exceed_total_timeout() -> None:
    with pytest.raises(ValidationError, match="retrieval read timeout"):
        _settings(retrieval_read_timeout_seconds=61)


@pytest.mark.parametrize(
    "url",
    [
        "http://retrieval.test:0",
        "http://retrieval.test:65536",
        "http://retrieval.test:not-a-port",
    ],
)
def test_dependency_url_rejects_invalid_ports(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(retrieval_url=url)


def test_settings_validation_error_hides_url_credentials() -> None:
    secret = "settings-url-secret"
    with pytest.raises(ValidationError) as raised:
        _settings(retrieval_url=f"http://user:{secret}@retrieval.test:not-a-port")

    assert secret not in str(raised.value)


def test_production_always_fails_closed_until_entra_adapter_exists() -> None:
    with pytest.raises(ValidationError, match="identity adapter is not implemented"):
        _settings(
            app_env="production",
            identity_mode="entra_jwt",
            retrieval_api_key="r" * 32,
            answer_generation_api_key="a" * 32,
        )


def test_production_rejects_development_identity_mode() -> None:
    with pytest.raises(ValidationError, match="development_headers"):
        _settings(
            app_env="production",
            identity_mode="development_headers",
            retrieval_api_key="r" * 32,
            answer_generation_api_key="a" * 32,
        )
