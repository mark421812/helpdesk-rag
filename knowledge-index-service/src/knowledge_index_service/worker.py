"""KIS outbox worker process 入口。

API 只把工作可靠寫入 PostgreSQL；此獨立 process 持續領取 outbox event、執行 embedding／
Qdrant 更新，並用 signal-aware Event 做 graceful shutdown。
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from knowledge_index_service.bootstrap import create_container
from knowledge_index_service.infrastructure.config import get_settings
from knowledge_index_service.infrastructure.logging import configure_logging


async def run_worker() -> None:
    """啟動事件迴圈，持續處理 outbox，沒有工作時可被 signal 喚醒。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    logger = structlog.get_logger()
    container = create_container(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGINT 通常來自 Ctrl+C；SIGTERM 通常由 Docker 在停止 container 時送出。
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stopping.set)

    logger.info("worker_started")
    try:
        await container.embedding_model.model_info()
        while not stopping.is_set():
            processed = await container.process_next_event.execute()
            if not processed:
                try:
                    await asyncio.wait_for(
                        stopping.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
    finally:
        await container.close()
        logger.info("worker_stopped")


def main() -> None:
    """同步 CLI 入口；``asyncio.run`` 建立 event loop 執行 async worker。"""

    asyncio.run(run_worker())


if __name__ == "__main__":
    # 只有 ``python -m knowledge_index_service.worker`` 直接執行時才呼叫 main。
    main()
