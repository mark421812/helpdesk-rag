"""Phase 5 bounded active/queue capacity gate tests。"""

from __future__ import annotations

import asyncio

import pytest

from chat_service.infrastructure.capacity import InMemoryCapacityGate


@pytest.mark.asyncio
async def test_capacity_gate_bounds_active_and_waiting_tasks() -> None:
    gate = InMemoryCapacityGate(max_active_turns=1, max_queue_size=1)
    first = await gate.acquire(1)
    assert first is not None
    assert gate.active_count == 1

    waiter = asyncio.create_task(gate.acquire(1))
    await asyncio.sleep(0)
    assert gate.queued_count == 1
    assert await gate.acquire(1) is None

    await gate.release(first)
    second = await waiter
    assert second is not None
    assert gate.active_count == 1
    assert gate.queued_count == 0

    await gate.release(second)
    await gate.release(second)
    assert gate.active_count == 0


@pytest.mark.asyncio
async def test_capacity_gate_timeout_removes_queue_entry() -> None:
    gate = InMemoryCapacityGate(max_active_turns=1, max_queue_size=1)
    lease = await gate.acquire(1)
    assert lease is not None

    assert await gate.acquire(0.001) is None
    assert gate.queued_count == 0
    assert gate.active_count == 1
    await gate.release(lease)


@pytest.mark.parametrize(
    ("active", "queue"),
    [(0, 1), (1, -1), (True, 1), (1, True)],
)
def test_capacity_gate_rejects_invalid_bounds(active: object, queue: object) -> None:
    with pytest.raises(ValueError):
        InMemoryCapacityGate(max_active_turns=active, max_queue_size=queue)  # type: ignore[arg-type]
