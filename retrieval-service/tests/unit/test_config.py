from __future__ import annotations

import pytest
from pydantic import ValidationError

from retrieval_service.bootstrap import build_runtime
from retrieval_service.infrastructure.config import Settings


def test_defaults_use_stable_knowledge_index_alias() -> None:
    settings = Settings(_env_file=None)

    assert settings.knowledge_index_url == "http://knowledge-index-api:8080"
    assert settings.identity_mode == "development_headers"
    assert settings.score_threshold is None


def test_blank_environment_score_threshold_is_unset() -> None:
    settings = Settings(_env_file=None, score_threshold="")  # type: ignore[arg-type]

    assert settings.score_threshold is None


def test_production_rejects_development_identity() -> None:
    with pytest.raises(ValidationError, match="forbids the development_headers"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key="retrieval-secret",
            knowledge_index_api_key="knowledge-secret",
            identity_mode="development_headers",
        )


def test_production_requires_both_service_secrets() -> None:
    with pytest.raises(ValidationError, match="API_INTERNAL_KEY"):
        Settings(
            _env_file=None,
            app_env="production",
            identity_mode="entra_jwt",
        )


def test_unknown_environment_cannot_bypass_production_validation() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="produciton")  # type: ignore[arg-type]


def test_result_limit_must_fit_candidate_limit() -> None:
    with pytest.raises(ValidationError, match="max_retrieval_limit"):
        Settings(
            _env_file=None,
            max_retrieval_limit=20,
            max_candidate_limit=10,
        )


def test_reserved_entra_mode_fails_closed_until_adapter_exists() -> None:
    settings = Settings(_env_file=None, app_env="test", identity_mode="entra_jwt")

    with pytest.raises(RuntimeError, match="not implemented"):
        build_runtime(settings)
