"""單一 Chat process 的 bounded active/queue capacity adapter。

這個 Infrastructure adapter 只管理 in-process asyncio state，不持有 PostgreSQL transaction，
也不宣稱可跨 replica 限流。`asyncio.Condition` 讓等待中的 task 在 slot 釋放時被喚醒；
`wait_for` 把 queue wait timeout 包在同一個等待操作內，超時會先撤銷 queue 計數再回傳
``None``，避免遺留幽靈 queue entry。Lease 使用 UUID 並以 set 記錄，release 重複呼叫
採 idempotent no-op，讓 orchestration 的 ``finally`` cleanup 不會二次釋放容量。
"""

from __future__ import annotations

import asyncio
from math import isfinite
from uuid import UUID, uuid4

from chat_service.application.dtos import CapacityLease


class InMemoryCapacityGate:
    """實作 ``CapacityGatePort`` 的單 process bounded gate。

    Active count 包含已取得 lease、尚未完成 Retrieval/Answer 的 turn；queue count 只包含
    正在等待 active slot 的 task。呼叫端必須在取得 lease 前不要開始下游 HTTP。
    """

    def __init__(self, *, max_active_turns: int, max_queue_size: int) -> None:
        if (
            isinstance(max_active_turns, bool)
            or not isinstance(max_active_turns, int)
            or max_active_turns <= 0
        ):
            raise ValueError("max_active_turns must be a positive integer")
        if (
            isinstance(max_queue_size, bool)
            or not isinstance(max_queue_size, int)
            or max_queue_size < 0
        ):
            raise ValueError("max_queue_size must be a non-negative integer")
        self._max_active_turns = max_active_turns
        self._max_queue_size = max_queue_size
        self._active_count = 0
        self._queued_count = 0
        self._leases: set[UUID] = set()
        self._condition = asyncio.Condition()

    @property
    def active_count(self) -> int:
        """目前持有 active lease 的數量；僅供 metrics/test 讀取。"""

        return self._active_count

    @property
    def queued_count(self) -> int:
        """目前等待 active slot 的 task 數量；僅供 metrics/test 讀取。"""

        return self._queued_count

    async def acquire(self, wait_timeout_seconds: float) -> CapacityLease | None:
        """在 bounded queue 內等待 active slot，超時或 queue 滿回傳 ``None``。"""

        if (
            isinstance(wait_timeout_seconds, bool)
            or not isinstance(wait_timeout_seconds, (int, float))
            or not isfinite(float(wait_timeout_seconds))
            or wait_timeout_seconds <= 0
        ):
            raise ValueError("wait_timeout_seconds must be a positive finite number")

        async with self._condition:
            if self._active_count < self._max_active_turns:
                return self._grant_locked()
            if self._queued_count >= self._max_queue_size:
                return None

            self._queued_count += 1
            try:
                async with asyncio.timeout(float(wait_timeout_seconds)):
                    await self._wait_for_active_slot_locked()
                return self._grant_locked()
            except TimeoutError:
                return None
            finally:
                self._queued_count -= 1

    async def release(self, lease: CapacityLease) -> None:
        """釋放 lease；未知或重複 lease 安全地忽略。"""

        if not isinstance(lease, CapacityLease):
            raise ValueError("lease must be a CapacityLease")
        async with self._condition:
            if lease.lease_id not in self._leases:
                return
            self._leases.remove(lease.lease_id)
            self._active_count -= 1
            self._condition.notify_all()

    async def _wait_for_active_slot_locked(self) -> None:
        """等待 condition predicate；呼叫者已持有 condition lock。"""

        await self._condition.wait_for(lambda: self._active_count < self._max_active_turns)

    def _grant_locked(self) -> CapacityLease:
        """在 condition lock 內建立唯一 lease。"""

        lease = CapacityLease(lease_id=uuid4())
        self._leases.add(lease.lease_id)
        self._active_count += 1
        return lease
