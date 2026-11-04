"""Web Chat 的開發期設定與 fail-closed 驗證。

這個 Demo 只支援 localhost development。Settings 使用 pydantic-settings 從環境讀值，跨欄位
驗證則在 process 啟動時執行；因此不會把客戶端傳入的 header 當成身分或把下游 URL 開放給瀏覽器。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """所有 Web Chat runtime 會用到的固定設定。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_env: Literal["development", "test"] = "development"
    web_chat_port: int = Field(default=8130, ge=1, le=65_535)
    chat_service_url: str = "http://chat-api:8120"
    demo_user_id: str = "web-chat-demo-user"
    demo_group_ids: str = "all-employees"
    allowed_hosts: str = "localhost,127.0.0.1"
    upstream_timeout_seconds: float = Field(default=330.0, gt=0, allow_inf_nan=False)
    readiness_timeout_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    upstream_max_response_body_bytes: int = Field(default=5_000_000, gt=0)
    api_max_request_body_bytes: int = Field(default=65_536, gt=0)

    @field_validator("app_env", mode="before")
    @classmethod
    def normalize_environment(cls, value: object) -> object:
        """接受 dev 縮寫，但拒絕 production 模式的誤啟動。"""

        if isinstance(value, str) and value.strip().casefold() == "dev":
            return "development"
        return value.strip().casefold() if isinstance(value, str) else value

    @field_validator("demo_user_id")
    @classmethod
    def validate_user_id(cls, value: str) -> str:
        """Demo subject 會成為 downstream header，因此拒絕控制字元。"""

        normalized = value.strip()
        if not normalized or len(normalized) > 512:
            raise ValueError("DEMO_USER_ID must be 1-512 characters")
        if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in normalized):
            raise ValueError("DEMO_USER_ID must not contain control characters")
        return normalized

    @field_validator("demo_group_ids")
    @classmethod
    def validate_group_ids(cls, value: str) -> str:
        """將逗號分隔群組驗證成不重複、非空的 development header 值。"""

        groups = [part.strip() for part in value.split(",")]
        if not groups or any(not group or len(group) > 512 for group in groups):
            raise ValueError("DEMO_GROUP_IDS contains a blank or oversized group")
        if len(groups) > 100 or len(set(groups)) != len(groups):
            raise ValueError("DEMO_GROUP_IDS must contain at most 100 unique groups")
        contains_control = any(
            any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in group) for group in groups
        )
        if contains_control:
            raise ValueError("DEMO_GROUP_IDS must not contain control characters")
        return ",".join(groups)

    @field_validator("allowed_hosts")
    @classmethod
    def validate_hosts(cls, value: str) -> str:
        """Host allowlist 不接受 wildcard 或空值。"""

        hosts = [part.strip().casefold() for part in value.split(",")]
        if not hosts or any(not host or host == "*" for host in hosts):
            raise ValueError("ALLOWED_HOSTS must contain explicit hosts")
        if len(set(hosts)) != len(hosts):
            raise ValueError("ALLOWED_HOSTS must not contain duplicates")
        return ",".join(hosts)

    @field_validator("chat_service_url")
    @classmethod
    def validate_chat_url(cls, value: str) -> str:
        """Chat base URL 只允許 HTTP(S) host，不接受 credentials/query/fragment。"""

        normalized = value.strip().rstrip("/")
        try:
            parsed = urlparse(normalized)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            raise ValueError("CHAT_SERVICE_URL must contain a valid host and port") from None
        if (
            parsed.scheme not in {"http", "https"}
            or hostname is None
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65_535)
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in hostname)
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in normalized)
        ):
            raise ValueError("CHAT_SERVICE_URL must be a credential-free HTTP(S) URL")
        return normalized

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        """只允許 development/test，避免無登入 Demo 被當成 production auth。"""

        if self.app_env not in {"development", "test"}:
            raise ValueError("Web Chat Demo only supports development or test environment")
        if self.readiness_timeout_seconds > self.upstream_timeout_seconds:
            raise ValueError("readiness timeout must not exceed upstream timeout")
        return self

    @property
    def group_header(self) -> str:
        """已驗證的 downstream group header。"""

        return self.demo_group_ids

    @property
    def host_allowlist(self) -> list[str]:
        """轉成 Starlette TrustedHostMiddleware 使用的明確 host 清單。"""

        return self.allowed_hosts.split(",")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Production process 只解析一次設定；測試可直接建立 Settings。"""

    return Settings()
