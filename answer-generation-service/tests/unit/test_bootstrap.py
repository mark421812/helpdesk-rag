from __future__ import annotations

import pytest

from answer_generation_service.bootstrap import build_runtime
from answer_generation_service.infrastructure.config import Settings
from answer_generation_service.infrastructure.llm.groq_hosted import GroqHostedProvider


def test_offline_fake_provider_does_not_require_external_delivery_approval() -> None:
    settings = Settings(_env_file=None, external_llm_approved=False)

    runtime = build_runtime(settings)

    assert runtime.outbound_policy.is_ready() is True


@pytest.mark.asyncio
async def test_external_provider_remains_blocked_without_explicit_approval() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="nvidia_hosted",
        llm_provider_usage_mode="trial",
        llm_model="test/model",
        nvidia_api_key="nvapi-synthetic",
        external_llm_approved=False,
    )

    runtime = build_runtime(settings)
    try:
        assert runtime.outbound_policy.is_ready() is False
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_groq_provider_is_selected_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="groq_hosted",
        llm_provider_usage_mode="trial",
        external_llm_approved=True,
        llm_model="openai/gpt-oss-20b",
        groq_api_key="gsk_synthetic",
    )

    runtime = build_runtime(settings)
    try:
        assert isinstance(runtime.provider, GroqHostedProvider)
        assert runtime.readiness_checks()["provider_configured"] is True
        assert runtime.outbound_policy.is_ready() is True
    finally:
        await runtime.close()
