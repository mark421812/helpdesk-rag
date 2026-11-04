"""Worker/Scheduler 共用的 signal handler 與可中斷等待 helpers。"""

from __future__ import annotations

import asyncio
import signal


def install_shutdown_handlers(stop_event: asyncio.Event) -> None:
    """收到 SIGINT/SIGTERM 時設定 event，讓背景 loop 完成目前工作後停止。"""

    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows event loop fallback
            signal.signal(signal_number, lambda *_: stop_event.set())


async def wait_or_stop(stop_event: asyncio.Event, timeout_seconds: float) -> bool:
    """等待 shutdown 或 timeout；回傳 True 表示收到停止要求。"""

    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except TimeoutError:
        return False
    return True
