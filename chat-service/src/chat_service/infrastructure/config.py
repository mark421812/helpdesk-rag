"""Chat Service 的強型別設定與啟動安全驗證。

這個 module 屬於 Infrastructure layer：它可以使用 pydantic-settings 讀取環境變數，但 Domain
與 Application 不應反向 import 它。設定在 process 啟動時一次驗證，避免服務運行途中才發現
timeout、queue 或 production identity 設定彼此矛盾。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from chat_service.domain.entities import MAX_RETRY_AFTER_SECONDS

LOCAL_DEVELOPMENT_KEY = "local-development-only"


class Settings(BaseSettings):
    """規格第 16 節的完整設定集合。

    Pydantic 會把欄位名稱自動對應到同名的大寫環境變數，例如 ``retrieval_url`` 對應
    ``RETRIEVAL_URL``。``model_validator`` 適合檢查跨欄位關係，因為那時所有欄位都已轉型。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "chat-service"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    chat_bind_address: str = "127.0.0.1"
    chat_api_port: int = Field(default=8120, ge=1, le=65_535)
    helpdesk_network: str = "helpdesk-rag"
    cors_origins: list[str] = Field(default_factory=list)
    identity_mode: Literal["development_headers", "entra_jwt"] = "development_headers"
    admin_role: str = "helpdesk-admin"

    database_url: str = "postgresql+asyncpg://knowledge:knowledge@postgres:5432/knowledge"
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)

    retrieval_url: str = "http://retrieval-api:8100"
    retrieval_api_key: str = LOCAL_DEVELOPMENT_KEY
    chat_retrieval_limit: int = Field(default=8, ge=1, le=20)
    retrieval_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    retrieval_read_timeout_seconds: float = Field(default=55.0, gt=0)
    retrieval_total_timeout_seconds: float = Field(default=60.0, gt=0)
    retrieval_readiness_timeout_seconds: float = Field(default=5.0, gt=0)
    retrieval_max_response_body_bytes: int = Field(default=5_000_000, gt=0)

    answer_generation_url: str = "http://answer-generation-api:8110"
    answer_generation_api_key: str = LOCAL_DEVELOPMENT_KEY
    answer_connect_timeout_seconds: float = Field(default=3.0, gt=0)
    answer_read_timeout_seconds: float = Field(default=85.0, gt=0)
    answer_total_timeout_seconds: float = Field(default=90.0, gt=0)
    answer_readiness_timeout_seconds: float = Field(default=5.0, gt=0)
    answer_max_response_body_bytes: int = Field(default=524_288, gt=0)

    max_active_turns: int = Field(default=10, ge=1, le=1_000)
    max_turn_queue_size: int = Field(default=20, ge=0, le=10_000)
    turn_queue_wait_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        le=MAX_RETRY_AFTER_SECONDS,
        allow_inf_nan=False,
    )
    processing_stale_seconds: float = Field(default=360.0, gt=0)
    purge_preview_ttl_seconds: int = Field(default=600, gt=0, le=86_400)
    api_max_request_body_bytes: int = Field(default=65_536, gt=0)

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_environment(cls, value: object) -> object:
        """在 Pydantic 轉型前接受常見的 ``dev``/``prod`` 縮寫。"""

        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold()
        if normalized == "dev":
            return "development"
        if normalized == "prod":
            return "production"
        return normalized

    @field_validator("app_name", "chat_bind_address", "helpdesk_network", "admin_role", "log_level")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        """統一清理簡單文字設定，避免空白值繞過 non-blank 檢查。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("text setting must not be blank")
        return normalized

    @field_validator("retrieval_url", "answer_generation_url")
    @classmethod
    def normalize_dependency_url(cls, value: str) -> str:
        """固定下游 URL 只允許 scheme/host/path，不接受 runtime credential 或 query。"""

        normalized = value.strip().rstrip("/")
        try:
            parsed = urlparse(normalized)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("dependency URL must contain a valid host and port") from None
        invalid_hostname = hostname is None or any(
            ord(character) <= 0x20 or ord(character) == 0x7F for character in hostname
        )
        invalid_port = port is not None and not 1 <= port <= 65_535
        if parsed.scheme not in {"http", "https"} or invalid_hostname or invalid_port:
            raise ValueError("dependency URL must use HTTP(S) and contain a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("dependency URL must not contain credentials, query, or fragment")
        return normalized

    @field_validator("cors_origins")
    @classmethod
    def normalize_cors_origins(cls, values: list[str]) -> list[str]:
        """清理 CORS origin；production 的 wildcard 禁止會在跨欄位 validator 檢查。"""

        normalized = [value.strip().rstrip("/") for value in values]
        if any(not value for value in normalized):
            raise ValueError("cors_origins must not contain blank origins")
        if len(set(normalized)) != len(normalized):
            raise ValueError("cors_origins must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_consistency(self) -> Settings:
        """驗證 timeout/stale 關係與 production fail-closed 條件。"""

        if self.retrieval_connect_timeout_seconds > self.retrieval_total_timeout_seconds:
            raise ValueError("retrieval connect timeout must not exceed total timeout")
        if self.retrieval_read_timeout_seconds > self.retrieval_total_timeout_seconds:
            raise ValueError("retrieval read timeout must not exceed total timeout")
        if self.answer_connect_timeout_seconds > self.answer_total_timeout_seconds:
            raise ValueError("answer connect timeout must not exceed total timeout")
        if self.answer_read_timeout_seconds > self.answer_total_timeout_seconds:
            raise ValueError("answer read timeout must not exceed total timeout")

        minimum_stale_seconds = (
            self.turn_queue_wait_timeout_seconds
            + self.retrieval_total_timeout_seconds
            + self.answer_total_timeout_seconds
            + 30
        )
        if self.processing_stale_seconds <= minimum_stale_seconds:
            raise ValueError(
                "processing_stale_seconds must exceed queue + retrieval + answer timeouts + 30"
            )

        if self.app_env == "production":
            if self.retrieval_api_key.strip() in {"", LOCAL_DEVELOPMENT_KEY}:
                raise ValueError("Production requires a real RETRIEVAL_API_KEY")
            if self.answer_generation_api_key.strip() in {"", LOCAL_DEVELOPMENT_KEY}:
                raise ValueError("Production requires a real ANSWER_GENERATION_API_KEY")
            if "*" in self.cors_origins:
                raise ValueError("Production forbids wildcard CORS")
            if self.identity_mode != "entra_jwt":
                raise ValueError("Production forbids development_headers identity mode")
            raise ValueError(
                "Production identity adapter is not implemented; refusing to start fail-open"
            )

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """每個 process 只解析一次環境變數；測試可直接建立 ``Settings`` 避免污染 cache。"""

    return Settings()
