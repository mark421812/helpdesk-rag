from __future__ import annotations

import pytest
from pydantic import ValidationError

from answer_generation_service.infrastructure.config import Settings


def test_defaults_are_offline_and_not_externally_approved() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "fake"
    assert settings.external_llm_approved is False


def test_nvidia_requires_key_and_allowlisted_https_host() -> None:
    with pytest.raises(ValidationError, match="NVIDIA_API_KEY"):
        Settings(_env_file=None, llm_provider="nvidia_hosted")
    with pytest.raises(ValidationError, match="PROVIDER_ALLOWED_HOSTS"):
        Settings(
            _env_file=None,
            llm_provider="nvidia_hosted",
            llm_provider_usage_mode="trial",
            nvidia_api_key="nvapi-test",
            nvidia_base_url="https://attacker.example/v1",
        )


def test_groq_requires_key_explicit_model_and_allowlisted_https_host() -> None:
    with pytest.raises(ValidationError, match="GROQ_API_KEY"):
        Settings(_env_file=None, llm_provider="groq_hosted")
    with pytest.raises(ValidationError, match="GROQ_ALLOWED_HOSTS"):
        Settings(
            _env_file=None,
            llm_provider="groq_hosted",
            llm_provider_usage_mode="trial",
            groq_api_key="gsk_synthetic",
            groq_base_url="https://attacker.example/openai/v1",
        )
    with pytest.raises(ValidationError, match="explicit LLM_MODEL"):
        Settings(
            _env_file=None,
            llm_provider="groq_hosted",
            llm_provider_usage_mode="trial",
            groq_api_key="gsk_synthetic",
        )


def test_groq_configuration_is_normalized() -> None:
    settings = Settings(
        _env_file=None,
        llm_provider="groq_hosted",
        llm_provider_usage_mode="trial",
        llm_model="openai/gpt-oss-20b",
        groq_api_key="  gsk_synthetic  ",
        groq_base_url="https://api.groq.com/openai/v1/",
    )

    assert settings.groq_api_key == "gsk_synthetic"
    assert settings.groq_base_url == "https://api.groq.com/openai/v1"


def test_production_forbids_fake_and_trial_provider() -> None:
    with pytest.raises(ValidationError, match="Fake Provider"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key="production-answer-key",
            external_llm_approved=True,
        )
    with pytest.raises(ValidationError, match="trial/test"):
        Settings(
            _env_file=None,
            app_env="production",
            api_internal_key="production-answer-key",
            llm_provider="nvidia_hosted",
            llm_provider_usage_mode="trial",
            external_llm_approved=True,
            nvidia_api_key="nvapi-test",
            llm_model="approved-model",
        )


def test_production_requires_strong_key_and_non_trial_endpoint() -> None:
    common = {
        "_env_file": None,
        "app_env": "production",
        "llm_provider": "nvidia_hosted",
        "llm_provider_usage_mode": "production",
        "external_llm_approved": True,
        "nvidia_api_key": "nvapi-test",
        "llm_model": "approved-model",
    }
    with pytest.raises(ValidationError, match="at least 32"):
        Settings(
            **common,
            api_internal_key="too-short",
            nvidia_base_url="https://approved-nim.company.example/v1",
            provider_allowed_hosts=["approved-nim.company.example"],
        )
    with pytest.raises(ValidationError, match="Trial endpoint"):
        Settings(
            **common,
            api_internal_key="a" * 32,
        )


def test_attestation_policy_versions_and_prompt_version_are_validated() -> None:
    with pytest.raises(ValidationError, match="policy_versions"):
        Settings(_env_file=None, attestation_accepted_policy_versions=[])
    with pytest.raises(ValidationError, match="prompt_version"):
        Settings(_env_file=None, prompt_version="unbound-label-v99")
    with pytest.raises(ValidationError, match="policy_prompt_bindings"):
        Settings(
            _env_file=None,
            prompt_version="grounded-answer-v1",
            attestation_policy_prompt_bindings={"retrieval-policy-v1": ["future-prompt-v2"]},
        )
