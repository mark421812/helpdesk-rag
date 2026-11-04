"""Ingestion background worker process：claim、process、poll 與 graceful shutdown。"""

from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

import structlog

from ingestion_service.bootstrap import Runtime, build_runtime, ensure_default_source
from ingestion_service.domain.exceptions import LeaseLostError, NotFoundError
from ingestion_service.healthcheck import (
    WORKER_HEARTBEAT_PATH,
    maintain_database_heartbeat,
)
from ingestion_service.infrastructure.config import get_settings
from ingestion_service.infrastructure.logging import configure_logging
from ingestion_service.runtime_support import install_shutdown_handlers, wait_or_stop

logger = structlog.get_logger(__name__)


async def process_next_job(runtime: Runtime, worker_id: str) -> bool:
    """嘗試 claim 並處理一個 job；無工作時回傳 False。"""

    claimed = await runtime.claim_next_job.execute(worker_id)
    if claimed is None:
        return False
    result = await runtime.process_job.execute(claimed.id, worker_id)
    logger.info(
        "ingestion_job_processed",
        job_id=str(result.job_id),
        job_status=result.status.value,
        item_status=result.item_status.value,
        deferred=result.deferred,
    )
    return True


async def run_worker(
    runtime: Runtime,
    *,
    worker_id: str,
    stop_event: asyncio.Event,
) -> None:
    """持續拉取工作直到 stop_event 被 signal handler 設定。"""

    poll_interval = runtime.settings.worker_poll_interval_seconds
    logger.info("ingestion_worker_started", worker_id=worker_id)
    while not stop_event.is_set():
        try:
            handled = await process_next_job(runtime, worker_id)
        except (LeaseLostError, NotFoundError) as exc:
            logger.warning(
                "ingestion_job_lease_or_record_lost",
                worker_id=worker_id,
                error=str(exc),
            )
            handled = True
        except Exception:
            logger.exception("ingestion_worker_iteration_failed", worker_id=worker_id)
            handled = False
        if not handled and await wait_or_stop(stop_event, poll_interval):
            break
    logger.info("ingestion_worker_stopped", worker_id=worker_id)


def default_worker_id() -> str:
    """組合 hostname、PID 與隨機尾碼，降低多 worker ID 碰撞機率。"""

    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


async def async_main() -> None:
    """組裝 runtime、heartbeat 與 graceful shutdown 的 async entry point。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)
    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None
    install_shutdown_handlers(stop_event)
    try:
        await ensure_default_source(runtime)
        # create_task 讓 heartbeat 與主 worker loop 在同一 event loop 並行。
        heartbeat_task = asyncio.create_task(
            maintain_database_heartbeat(
                runtime,
                role="worker",
                path=WORKER_HEARTBEAT_PATH,
                stop_event=stop_event,
            )
        )
        await run_worker(runtime, worker_id=default_worker_id(), stop_event=stop_event)
    finally:
        stop_event.set()
        if heartbeat_task is not None:
            await heartbeat_task
        await runtime.close()


def main() -> None:
    """同步 console entry point；asyncio.run 建立並關閉 event loop。"""

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
