from __future__ import annotations

import pytest
from pydantic import ValidationError

from knowledge_index_service.infrastructure.config import Settings


def test_production_requires_internal_api_key() -> None:
    with pytest.raises(ValidationError, match="API_INTERNAL_KEY"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key=None,
            qdrant_api_key="production-qdrant-secret",
        )


def test_production_rejects_development_internal_api_key() -> None:
    with pytest.raises(ValidationError, match="non-development"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key="local-development-only",
            qdrant_api_key="production-qdrant-secret",
        )


@pytest.mark.parametrize(
    "qdrant_api_key",
    [None, "", "   ", "local-development-only", " local-development-only "],
)
def test_production_rejects_missing_or_development_qdrant_key(
    qdrant_api_key: str | None,
) -> None:
    with pytest.raises(ValidationError, match="QDRANT_API_KEY"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key="production-internal-secret",
            qdrant_api_key=qdrant_api_key,
        )


def test_production_accepts_explicit_non_development_secrets() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        api_internal_key="production-internal-secret",
        qdrant_api_key="production-qdrant-secret",
    )

    assert settings.app_env == "production"


def test_business_timezone_defaults_to_asia_taipei() -> None:
    settings = Settings(_env_file=None)

    assert settings.business_timezone == "Asia/Taipei"


def test_business_timezone_rejects_unknown_zone() -> None:
    with pytest.raises(ValidationError, match="Unknown business timezone"):
        Settings(_env_file=None, business_timezone="Not/A-Timezone")
