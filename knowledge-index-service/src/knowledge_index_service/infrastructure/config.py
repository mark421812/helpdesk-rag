"""以 Pydantic Settings 載入 KIS 環境變數並在啟動時驗證安全限制。"""

from __future__ import annotations

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KIS 強型別設定；欄位名稱自動對應同名大寫環境變數。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    log_level: str = "INFO"
    api_internal_key: str | None = None

    database_url: str = "postgresql+asyncpg://knowledge:knowledge@postgres:5432/knowledge"
    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=20, ge=0)

    qdrant_url: str = "http://qdrant:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = Field(
        default="helpdesk_knowledge",
        min_length=1,
        max_length=240,
    )
    qdrant_vectors_on_disk: bool = True
    qdrant_hnsw_on_disk: bool = True
    qdrant_payload_on_disk: bool = True

    model_id: str = "intfloat/multilingual-e5-small"
    model_revision: str = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
    model_device: str = "cpu"
    model_batch_size: int = Field(default=16, ge=1)
    model_max_sequence_length: int = Field(default=512, ge=1)
    normalize_embeddings: bool = True
    torch_num_threads: int = Field(default=2, ge=1)
    embedding_max_concurrency: int = Field(default=1, ge=1)

    max_embedding_texts: int = Field(default=64, ge=1)
    max_characters_per_text: int = Field(default=20_000, ge=1)
    max_search_limit: int = Field(default=50, ge=1)
    max_search_candidates: int = Field(default=500, ge=1)
    max_document_chunks: int = Field(default=10_000, ge=1)
    max_document_characters: int = Field(default=20_000_000, ge=1)
    max_request_body_bytes: int = Field(default=25_000_000, ge=1)

    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=3600, ge=1)
    worker_retry_base_seconds: int = Field(default=5, ge=1)

    cors_origins: list[str] = Field(default_factory=list)
    business_timezone: str = Field(default="Asia/Taipei", min_length=1)

    @field_validator("business_timezone")
    @classmethod
    def validate_business_timezone(cls, value: str) -> str:
        """在啟動時確認 IANA timezone 存在，避免查詢日期悄悄算錯。"""

        value = value.strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown business timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def require_production_secrets(self) -> Settings:
        """所有欄位轉型後，執行跨欄位的 production secret policy。"""

        if self.app_env.strip().lower() != "production":
            return self
        internal_key = self.api_internal_key.strip() if self.api_internal_key else ""
        qdrant_key = self.qdrant_api_key.strip() if self.qdrant_api_key else ""
        if not internal_key or internal_key == "local-development-only":
            raise ValueError(
                "API_INTERNAL_KEY must use a non-development secret when APP_ENV=production"
            )
        if not qdrant_key or qdrant_key == "local-development-only":
            raise ValueError(
                "QDRANT_API_KEY must use a non-development secret when APP_ENV=production"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """每個 process 只建立一次 Settings。"""

    return Settings()
