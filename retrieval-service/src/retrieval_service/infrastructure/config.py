"""使用 Pydantic Settings 載入並驗證環境變數。

欄位名稱會自動對應大寫環境變數，例如 ``knowledge_index_url`` 對應
``KNOWLEDGE_INDEX_URL``。設定驗證在服務啟動時完成，錯誤設定會直接拒絕啟動。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Retrieval Service 的強型別設定與跨欄位安全規則。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    api_internal_key: str | None = None
    cors_origins: list[str] = Field(default_factory=list)
    identity_mode: Literal["development_headers", "entra_jwt"] = "development_headers"

    knowledge_index_url: str = "http://knowledge-index-api:8080"
    knowledge_index_api_key: str | None = None
    knowledge_index_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    knowledge_index_read_timeout_seconds: float = Field(default=45.0, gt=0)
    knowledge_index_total_timeout_seconds: float = Field(default=50.0, gt=0)
    knowledge_index_readiness_timeout_seconds: float = Field(default=5.0, gt=0)
    knowledge_index_max_response_body_bytes: int = Field(default=5_000_000, ge=1)
    knowledge_index_retry_attempts: int = Field(default=1, ge=0, le=3)
    knowledge_index_retry_backoff_seconds: float = Field(default=0.2, ge=0, le=5)
    knowledge_index_retry_max_backoff_seconds: float = Field(default=2.0, ge=0, le=10)

    api_max_request_body_bytes: int = Field(default=65_536, ge=1)
    default_retrieval_limit: int = Field(default=8, ge=1, le=50)
    max_retrieval_limit: int = Field(default=20, ge=1, le=50)
    max_candidate_limit: int = Field(default=50, ge=1, le=50)
    candidate_multiplier: int = Field(default=3, ge=1, le=10)
    max_context_tokens: int = Field(default=3_000, ge=1)
    max_chunks_per_document: int = Field(default=3, ge=1)
    score_threshold: float | None = Field(default=None, ge=-1, le=1)

    # Retrieval 持有私鑰；Answer Service 只會取得相對應的公鑰。
    attestation_private_key_path: str = "/run/secrets/retrieval-attestation-private.pem"
    attestation_key_id: str = "retrieval-dev-001"
    attestation_ttl_seconds: int = Field(default=120, ge=30, le=600)
    attestation_issuer: str = "retrieval-service"
    attestation_audience: str = "answer-generation-service"
    attestation_policy_version: str = "retrieval-policy-v1"

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_app_env(cls, value: object) -> object:
        """在 Pydantic 轉型前統一 dev/prod 等常見縮寫。"""

        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("app_env must not be blank")
        if normalized == "dev":
            return "development"
        if normalized == "prod":
            return "production"
        return normalized

    @field_validator("knowledge_index_url")
    @classmethod
    def normalize_knowledge_index_url(cls, value: str) -> str:
        """移除尾端斜線，避免組合 endpoint 時出現雙斜線。"""

        normalized = value.strip().rstrip("/")
        if not normalized:
            raise ValueError("knowledge_index_url must not be blank")
        return normalized

    @field_validator("score_threshold", mode="before")
    @classmethod
    def blank_score_threshold_is_unset(cls, value: object) -> object:
        """Compose 傳入空字串時，把 threshold 視為尚未校準。"""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "attestation_private_key_path",
        "attestation_key_id",
        "attestation_issuer",
        "attestation_audience",
        "attestation_policy_version",
    )
    @classmethod
    def attestation_text_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("attestation settings must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_consistency(self) -> Settings:
        """欄位都完成轉型後，再驗證彼此之間的關係與 production 安全要求。"""

        if self.default_retrieval_limit > self.max_retrieval_limit:
            raise ValueError("default_retrieval_limit must not exceed max_retrieval_limit")
        if self.max_retrieval_limit > self.max_candidate_limit:
            raise ValueError("max_retrieval_limit must not exceed max_candidate_limit")
        if (
            self.knowledge_index_retry_max_backoff_seconds
            < self.knowledge_index_retry_backoff_seconds
        ):
            raise ValueError(
                "knowledge_index_retry_max_backoff_seconds must not be smaller than "
                "knowledge_index_retry_backoff_seconds"
            )
        if self.app_env == "production":
            missing_secrets = [
                name
                for name, value in (
                    ("API_INTERNAL_KEY", self.api_internal_key),
                    ("KNOWLEDGE_INDEX_API_KEY", self.knowledge_index_api_key),
                )
                if value is None or not value.strip()
            ]
            if missing_secrets:
                raise ValueError(
                    "Production requires non-blank secrets: " + ", ".join(missing_secrets)
                )
            if self.api_internal_key == "local-development-only":
                raise ValueError(
                    "Production API_INTERNAL_KEY must not use the development placeholder"
                )
            if self.knowledge_index_api_key == "local-development-only":
                raise ValueError(
                    "Production KNOWLEDGE_INDEX_API_KEY must not use the development placeholder"
                )
            if self.identity_mode == "development_headers":
                raise ValueError("Production forbids the development_headers identity mode")
        return self


@lru_cache
def get_settings() -> Settings:
    """每個 process 只解析一次環境變數；後續呼叫回傳同一個 Settings。"""

    return Settings()
