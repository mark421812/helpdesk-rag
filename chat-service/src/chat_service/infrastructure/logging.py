"""設定 stdout JSON logging，並避免在基礎 logger 中記錄問題、答案或 secrets。

structlog 的 contextvars processor 會把 request-scoped metadata 合併進目前事件；真正的
request ID binding 與 redaction middleware 由 Phase 6 建立，本階段只提供可測試的 logging 基礎。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "token",
        "context_attestation",
        "attestation",
        "query",
        "question",
        "answer",
        "content",
        "evidence",
        "source_path",
        "source_url",
        "url",
        "path",
        "body",
        "raw_body",
        "raw_response",
        "detail",
        "exception",
        "exc_info",
        "stack_info",
        "user_subject",
        "subject",
        "group_ids",
        "groups",
        "conversation_id",
        "turn_id",
        "message_id",
    }
)


def configure_logging(level: str) -> None:
    """以 UTC timestamp 與 JSON renderer 初始化 process logger。"""

    resolved_level = logging.getLevelName(level.upper())
    if not isinstance(resolved_level, int):
        resolved_level = logging.INFO

    logging.basicConfig(level=resolved_level, format="%(message)s", stream=sys.stdout, force=True)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _redact_sensitive,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _redact_sensitive(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """在 JSON renderer 前遞迴遮罩 secrets/content，連 traceback 也不輸出。"""

    for key in tuple(event_dict):
        event_dict[key] = _redact_value(str(key), event_dict[key])
    return event_dict


def _redact_value(key: str, value: Any) -> Any:
    """依 key 遞迴處理 nested event；不把不可信 raw exception/body 留在 log。"""

    normalized = key.casefold().replace("-", "_")
    if (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_token")
    ):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, child_value) for child_value in value]
    return value
