"""Worker/Scheduler heartbeat healthcheck 與 DB probe。

獨立背景 process 沒有 HTTP endpoint，因此以定期更新檔案，再由 Docker HEALTHCHECK 檢查
檔案新鮮度；只有 PostgreSQL probe 成功才更新，避免 process 活著但實際卡死仍被判健康。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from ingestion_service.runtime_support import wait_or_stop

if TYPE_CHECKING:
    from ingestion_service.bootstrap import Runtime

logger = structlog.get_logger(__name__)

WORKER_HEARTBEAT_PATH = Path("/tmp/ingestion-worker.heartbeat")
SCHEDULER_HEARTBEAT_PATH = Path("/tmp/ingestion-scheduler.heartbeat")


def heartbeat_is_fresh(
    path: Path,
    *,
    max_age_seconds: float,
    now: float | None = None,
) -> bool:
    """用檔案 mtime 判斷背景角色是否仍持續回報健康。"""

    if max_age_seconds <= 0:
        return False
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        return False
    age = (time.time() if now is None else now) - modified_at
    return 0 <= age <= max_age_seconds


async def refresh_database_heartbeat(runtime: Runtime, path: Path) -> bool:
    """Touch the role heartbeat only after PostgreSQL answers a probe."""

    if not await runtime.database_ready():
        return False
    try:
        await asyncio.to_thread(_touch_heartbeat, path)
    except OSError:
        return False
    return True


async def maintain_database_heartbeat(
    runtime: Runtime,
    *,
    role: str,
    path: Path,
    stop_event: asyncio.Event,
) -> None:
    """定期 probe PostgreSQL；成功才更新 heartbeat，直到要求停止。"""

    interval = runtime.settings.role_heartbeat_interval_seconds
    previous_state: bool | None = None
    while not stop_event.is_set():
        healthy = await refresh_database_heartbeat(runtime, path)
        if healthy != previous_state:
            log = logger.info if healthy else logger.warning
            log("role_database_heartbeat_changed", role=role, healthy=healthy)
            previous_state = healthy
        if await wait_or_stop(stop_event, interval):
            break


def _touch_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check a runtime role heartbeat file")
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--max-age", required=True, type=float)
    return parser


def main() -> None:
    """Docker HEALTHCHECK CLI entry point，以 0/1 表示 heartbeat 是否新鮮。"""

    args = _build_parser().parse_args()
    raise SystemExit(
        0
        if heartbeat_is_fresh(
            args.path,
            max_age_seconds=args.max_age,
        )
        else 1
    )


if __name__ == "__main__":
    main()
