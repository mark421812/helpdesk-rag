"""以 Pydantic Settings 載入 Ingestion 環境變數與 production 安全限制。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """以 Pydantic Settings 將環境變數解析成有型別、已驗證的設定物件。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    api_internal_key: str | None = None
    cors_origins: list[str] = Field(default_factory=list)

    database_url: str = "postgresql+asyncpg://knowledge:knowledge@postgres:5432/knowledge"
    database_pool_size: int = Field(default=5, ge=1)
    database_max_overflow: int = Field(default=5, ge=0)

    knowledge_index_url: str = "http://knowledge-index-api:8080"
    knowledge_index_api_key: str | None = None
    knowledge_index_timeout_seconds: float = Field(default=120.0, gt=0)
    knowledge_index_readiness_timeout_seconds: float = Field(default=5.0, gt=0)
    knowledge_index_max_request_body_bytes: int = Field(default=25_000_000, ge=1)
    api_max_request_body_bytes: int = Field(default=65_536, ge=1)

    document_root: Path = Path("/data/documents")
    default_source_name: str = Field(default="local-documents", min_length=1, max_length=255)
    allowed_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".docx", ".txt", ".md"])
    max_file_size_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    missing_scans_before_delete: int = Field(default=2, ge=2)
    max_missing_ratio: float = Field(default=0.25, gt=0, le=1)
    source_default_metadata: dict[str, object] = Field(default_factory=dict)

    model_id: str = "intfloat/multilingual-e5-small"
    model_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    chunk_target_tokens: int = Field(default=350, ge=1)
    chunk_max_tokens: int = Field(default=450, ge=1)
    chunk_overlap_tokens: int = Field(default=50, ge=0)
    model_max_sequence_length: int = Field(default=512, ge=1)
    max_document_characters: int = Field(default=20_000_000, ge=1)
    max_document_chunks: int = Field(default=10_000, ge=1)

    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=3600, ge=1)
    worker_max_attempts: int = Field(default=5, ge=1)
    worker_retry_base_seconds: int = Field(default=5, ge=1)
    role_heartbeat_interval_seconds: float = Field(default=10.0, gt=0)
    role_heartbeat_max_age_seconds: float = Field(default=60.0, gt=0)

    schedule_hour: int = Field(default=2, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    schedule_timezone: str = "Asia/Taipei"

    @field_validator("allowed_extensions")
    @classmethod
    def normalize_extensions(cls, values: list[str]) -> list[str]:
        normalized = sorted(
            {value.lower() if value.startswith(".") else f".{value.lower()}" for value in values}
        )
        if not normalized:
            raise ValueError("allowed_extensions must not be empty")
        return normalized

    @field_validator("knowledge_index_url")
    @classmethod
    def strip_url_suffix(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("app_env")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("app_env must not be blank")
        return normalized

    @field_validator("schedule_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown schedule timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> Settings:
        if self.chunk_target_tokens > self.chunk_max_tokens:
            raise ValueError("chunk_target_tokens must not exceed chunk_max_tokens")
        if self.chunk_max_tokens >= self.model_max_sequence_length:
            raise ValueError("chunk_max_tokens must leave room below the model sequence limit")
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError("chunk_overlap_tokens must be smaller than chunk_target_tokens")
        if self.role_heartbeat_max_age_seconds <= self.role_heartbeat_interval_seconds:
            raise ValueError(
                "role_heartbeat_max_age_seconds must exceed role_heartbeat_interval_seconds"
            )
        if self.app_env in {"prod", "production"}:
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
        return self


@lru_cache
def get_settings() -> Settings:
    """載入並快取設定；同一 process 不會反覆解析環境變數。"""

    return Settings()
