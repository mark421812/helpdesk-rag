"""每日來源掃描 scheduler process 與漏跑補償邏輯。"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import structlog

from ingestion_service.application.dtos import ScanSourceResult
from ingestion_service.bootstrap import Runtime, build_runtime, ensure_default_source
from ingestion_service.domain.entities import Source, utc_now
from ingestion_service.domain.enums import SyncTrigger
from ingestion_service.healthcheck import (
    SCHEDULER_HEARTBEAT_PATH,
    maintain_database_heartbeat,
)
from ingestion_service.infrastructure.config import get_settings
from ingestion_service.infrastructure.logging import configure_logging
from ingestion_service.runtime_support import install_shutdown_handlers, wait_or_stop

logger = structlog.get_logger(__name__)


def next_daily_run(
    now: datetime,
    *,
    hour: int,
    minute: int,
    timezone: str,
) -> datetime:
    """以指定時區計算下一次每日執行時間，回傳 timezone-aware datetime。"""

    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), time(hour, minute), tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def needs_daily_catch_up(
    source: Source,
    now: datetime,
    *,
    hour: int,
    minute: int,
    timezone: str,
) -> bool:
    """判斷服務重啟後是否錯過今天排程，若錯過則立即補掃。"""

    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    scheduled_today = datetime.combine(
        local_now.date(),
        time(hour, minute),
        tzinfo=zone,
    )
    if local_now < scheduled_today:
        return False
    last_success = source.last_successful_scan_at
    return last_success is None or last_success.astimezone(zone) < scheduled_today


async def run_scheduled_scans(
    runtime: Runtime,
    *,
    sources: tuple[Source, ...] | None = None,
) -> tuple[ScanSourceResult, ...]:
    """循序掃描所有 enabled sources；單一來源失敗不阻止其他來源。"""

    results: list[ScanSourceResult] = []
    resolved_sources = sources if sources is not None else await runtime.list_enabled_sources()
    for source in resolved_sources:
        try:
            result = await runtime.scan_source.execute(
                source.id,
                SyncTrigger.SCHEDULED,
                allow_mass_deletion=False,
            )
        except Exception:
            logger.exception("scheduled_source_scan_failed", source_id=str(source.id))
            continue
        results.append(result)
        logger.info(
            "scheduled_source_scan_completed",
            source_id=str(source.id),
            run_id=str(result.run_id),
            discovered_count=result.discovered_count,
            queued_count=result.queued_count,
        )
    return tuple(results)


async def run_catch_up_scans(
    runtime: Runtime,
    *,
    now: datetime | None = None,
) -> tuple[ScanSourceResult, ...]:
    """只掃描今天已到排程時間、但尚無成功紀錄的 sources。"""

    settings = runtime.settings
    checked_at = now or utc_now()
    due_sources = tuple(
        source
        for source in await runtime.list_enabled_sources()
        if needs_daily_catch_up(
            source,
            checked_at,
            hour=settings.schedule_hour,
            minute=settings.schedule_minute,
            timezone=settings.schedule_timezone,
        )
    )
    if not due_sources:
        return ()
    logger.info(
        "scheduler_catch_up_started",
        source_count=len(due_sources),
        checked_at=checked_at.isoformat(),
    )
    return await run_scheduled_scans(runtime, sources=due_sources)


async def run_scheduler(runtime: Runtime, *, stop_event: asyncio.Event) -> None:
    """啟動先補漏跑，再等待每天排程時間。"""

    settings = runtime.settings
    logger.info(
        "ingestion_scheduler_started",
        hour=settings.schedule_hour,
        minute=settings.schedule_minute,
        timezone=settings.schedule_timezone,
    )
    # Compose 預期一個 scheduler replica；PostgreSQL advisory lock 仍是防重疊的最後護欄。
    if not stop_event.is_set():
        await run_catch_up_scans(runtime)
    while not stop_event.is_set():
        now = utc_now()
        scheduled_at = next_daily_run(
            now,
            hour=settings.schedule_hour,
            minute=settings.schedule_minute,
            timezone=settings.schedule_timezone,
        )
        delay = max(0.0, (scheduled_at.astimezone(now.tzinfo) - now).total_seconds())
        logger.info("next_source_scan_scheduled", scheduled_at=scheduled_at.isoformat())
        if await wait_or_stop(stop_event, delay):
            break
        await run_scheduled_scans(runtime)
    logger.info("ingestion_scheduler_stopped")


async def async_main() -> None:
    """組裝 scheduler runtime、heartbeat 與 graceful shutdown。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)
    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    install_shutdown_handlers(stop_event)
    try:
        await ensure_default_source(runtime)
        heartbeat_task = asyncio.create_task(
            maintain_database_heartbeat(
                runtime,
                role="scheduler",
                path=SCHEDULER_HEARTBEAT_PATH,
                stop_event=stop_event,
            )
        )
        await run_scheduler(runtime, stop_event=stop_event)
    finally:
        stop_event.set()
        if heartbeat_task is not None:
            await heartbeat_task
        await runtime.close()


def main() -> None:
    """Scheduler console entry point。"""

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
