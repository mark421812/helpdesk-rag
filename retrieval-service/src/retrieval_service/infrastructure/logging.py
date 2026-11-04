"""結構化 JSON logging 設定。

structlog 讓 log 以 key/value 表示，日後可由 Loki、OpenSearch 等工具查詢；此服務刻意不把
query、chunk 內容或 user ID 寫入 log。
"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str) -> None:
    """設定標準 logging 與 structlog processor pipeline。"""

    resolved_level = logging.getLevelName(level.upper())
    if not isinstance(resolved_level, int):
        resolved_level = logging.INFO
    logging.basicConfig(level=resolved_level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
