"""Pydantic Settings：啟動時驗證 Provider、外送核准與安全邊界。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from answer_generation_service.application.prompt_builder import SYSTEM_PROMPTS

DEFAULT_POLICY_PATH = str(Path(__file__).parent / "outbound_policy" / "policy.yaml")
NVIDIA_TRIAL_HOST = "integrate.api.nvidia.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_internal_key: str | None = None
    api_max_request_body_bytes: int = Field(default=262_144, ge=1, le=5_000_000)

    llm_provider: Literal["fake", "nvidia_hosted", "groq_hosted"] = "fake"
    llm_provider_usage_mode: Literal["test", "trial", "production"] = "test"
    external_llm_approved: bool = False
    llm_model: str = "fake-grounded-v1"
    max_generation_concurrency: int = Field(default=2, ge=1, le=100)
    max_generation_queue_size: int = Field(default=8, ge=0, le=1_000)
    llm_queue_wait_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    llm_max_output_tokens: int = Field(default=1_024, ge=1, le=16_384)
    llm_max_response_bytes: int = Field(default=262_144, ge=1, le=5_000_000)
    llm_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    llm_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    llm_total_timeout_seconds: float = Field(default=75.0, gt=0, le=900)
    llm_total_attempts: int = Field(default=2, ge=1, le=3)
    llm_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=10)
    llm_retry_max_backoff_seconds: float = Field(default=4.0, ge=0, le=30)

    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_api_key: str | None = None
    provider_allowed_hosts: list[str] = Field(default_factory=lambda: ["integrate.api.nvidia.com"])
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_api_key: str | None = None
    groq_allowed_hosts: list[str] = Field(default_factory=lambda: ["api.groq.com"])

    max_context_tokens: int = Field(default=3_000, ge=1, le=100_000)
    max_prompt_tokens: int = Field(default=12_000, ge=1, le=1_000_000)
    max_answer_characters: int = Field(default=20_000, ge=1, le=200_000)
    generation_temperature: float = Field(default=0.0, ge=0, le=2)
    prompt_version: str = "grounded-answer-v1"

    outbound_policy_path: str = DEFAULT_POLICY_PATH
    attestation_public_key_paths: dict[str, str] = Field(
        default_factory=lambda: {
            "retrieval-dev-001": "/run/secrets/retrieval-attestation-public.pem"
        }
    )
    attestation_issuer: str = "retrieval-service"
    attestation_audience: str = "answer-generation-service"
    attestation_context_schema_version: str = "retrieval-context-v1"
    attestation_accepted_policy_versions: list[str] = Field(
        default_factory=lambda: ["retrieval-policy-v1"]
    )
    attestation_policy_prompt_bindings: dict[str, list[str]] = Field(
        default_factory=lambda: {"retrieval-policy-v1": ["grounded-answer-v1"]}
    )
    attestation_clock_skew_seconds: int = Field(default=30, ge=0, le=120)
    attestation_max_lifetime_seconds: int = Field(default=600, ge=30, le=3_600)

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_environment(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold()
        if normalized == "dev":
            return "development"
        if normalized == "prod":
            return "production"
        return normalized

    @field_validator("nvidia_base_url", "groq_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("provider base URL must not be blank")
        return normalized

    @field_validator("nvidia_api_key", "groq_api_key")
    @classmethod
    def normalize_provider_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if any(character in normalized for character in ("\r", "\n", "\x00")):
            raise ValueError("provider API key must not contain control characters")
        return normalized

    @field_validator(
        "llm_model",
        "prompt_version",
        "outbound_policy_path",
        "attestation_issuer",
        "attestation_audience",
        "attestation_context_schema_version",
    )
    @classmethod
    def text_settings_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("text settings must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_security_configuration(self) -> Settings:
        if self.llm_retry_max_backoff_seconds < self.llm_retry_backoff_seconds:
            raise ValueError("retry max backoff must not be smaller than initial backoff")
        if self.prompt_version not in SYSTEM_PROMPTS:
            raise ValueError("prompt_version must identify a supported immutable prompt")
        if not self.attestation_public_key_paths or any(
            not key_id.strip() or not path.strip()
            for key_id, path in self.attestation_public_key_paths.items()
        ):
            raise ValueError("attestation_public_key_paths must contain non-blank key mappings")
        accepted_policy_versions = [
            value.strip() for value in self.attestation_accepted_policy_versions
        ]
        if (
            not accepted_policy_versions
            or any(not value for value in accepted_policy_versions)
            or len(set(accepted_policy_versions)) != len(accepted_policy_versions)
        ):
            raise ValueError(
                "attestation_accepted_policy_versions must contain unique non-blank versions"
            )
        self.attestation_accepted_policy_versions = accepted_policy_versions
        normalized_bindings = {
            policy.strip(): [prompt.strip() for prompt in prompts]
            for policy, prompts in self.attestation_policy_prompt_bindings.items()
        }
        if set(normalized_bindings) != set(accepted_policy_versions) or any(
            not policy
            or not prompts
            or any(not prompt for prompt in prompts)
            or len(set(prompts)) != len(prompts)
            or self.prompt_version not in prompts
            for policy, prompts in normalized_bindings.items()
        ):
            raise ValueError(
                "attestation_policy_prompt_bindings must map every accepted retrieval policy "
                "to unique non-blank prompts including the configured prompt_version"
            )
        self.attestation_policy_prompt_bindings = normalized_bindings

        if self.llm_provider == "nvidia_hosted":
            if not self.nvidia_api_key:
                raise ValueError("NVIDIA_API_KEY is required for nvidia_hosted")
            _validate_provider_url(
                value=self.nvidia_base_url,
                allowed_hosts=self.provider_allowed_hosts,
                setting_name="NVIDIA_BASE_URL",
                allowlist_name="PROVIDER_ALLOWED_HOSTS",
            )
        if self.llm_provider == "groq_hosted":
            if not self.groq_api_key:
                raise ValueError("GROQ_API_KEY is required for groq_hosted")
            _validate_provider_url(
                value=self.groq_base_url,
                allowed_hosts=self.groq_allowed_hosts,
                setting_name="GROQ_BASE_URL",
                allowlist_name="GROQ_ALLOWED_HOSTS",
            )
        if self.llm_provider != "fake" and self.llm_model == "fake-grounded-v1":
            raise ValueError("External provider requires an explicit LLM_MODEL")
        if self.llm_provider == "fake" and self.llm_provider_usage_mode != "test":
            raise ValueError("Fake provider requires LLM_PROVIDER_USAGE_MODE=test")

        if self.app_env == "production":
            if self.api_internal_key is None or not self.api_internal_key.strip():
                raise ValueError("Production requires API_INTERNAL_KEY")
            if self.api_internal_key == "local-development-only":
                raise ValueError("Production forbids the development API key")
            if self.llm_provider == "fake":
                raise ValueError("Production forbids Fake Provider")
            if self.llm_provider_usage_mode != "production":
                raise ValueError("Production forbids trial/test Provider usage")
            if not self.external_llm_approved:
                raise ValueError("Production requires explicit external LLM approval")
            if len(self.api_internal_key.strip()) < 32:
                raise ValueError("Production API_INTERNAL_KEY must contain at least 32 characters")
            if (
                self.llm_provider == "nvidia_hosted"
                and (urlparse(self.nvidia_base_url).hostname or "").casefold() == NVIDIA_TRIAL_HOST
            ):
                raise ValueError("Production forbids the NVIDIA hosted Trial endpoint")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _validate_provider_url(
    *,
    value: str,
    allowed_hosts: list[str],
    setting_name: str,
    allowlist_name: str,
) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{setting_name} must use HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{setting_name} must not contain credentials/query/fragment")
    normalized_hosts = {host.strip().casefold() for host in allowed_hosts if host.strip()}
    if parsed.hostname.casefold() not in normalized_hosts:
        raise ValueError(f"{setting_name} host is not in {allowlist_name}")
