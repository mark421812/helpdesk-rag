"""Ingestion structlog JSON logging 設定。"""

from __future__ import annotations

import logging

import structlog


def configure_logging(level: str) -> None:
    """將標準 logging 與 structlog 組成 stdout JSON pipeline。"""

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
