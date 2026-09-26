"""TaskSet, Timer, Batcher and FrameProcessor on a VirtualClock."""

from __future__ import annotations

import asyncio
import heapq
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiorehom.clock import VirtualClock
from aiorehom.state import ChangeSet, StoreSet
from aiorehom.sync import Batcher, FrameProcessor, TaskSet, Timer

from .sync_fakes import termo
from .test_state_store import snapshot

T0 = datetime(2026, 9, 25, 10, tzinfo=UTC)


async def test_task_set_logs_failures_and_refuses_spawns_once_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tasks = TaskSet("test")

    async def boom() -> None:
        raise RuntimeError("background bug (test)")

    with caplog.at_level(logging.ERROR, logger="aiorehom.sync"):
        task = tasks.spawn(boom(), "boom")
        assert task is not None
        await asyncio.wait([task])
    assert "background task test:boom failed" in caplog.text
    assert len(tasks) == 0
    assert not tasks.closed
    await tasks.close()
    assert tasks.closed
    coro = boom()
    assert tasks.spawn(coro, "late") is None
    assert coro.cr_frame is None  # closed, never "was never awaited"


async def test_task_set_close_skips_the_calling_task() -> None:
    tasks = TaskSet()
    clock = VirtualClock(T0)

    async def closer() -> str:
        await tasks.close()
        return "done"

    sleeper = tasks.spawn(clock.sleep(100), "sleeper")
    me = tasks.spawn(closer(), "closer")
    assert sleeper is not None and me is not None
    assert await me == "done"
    assert sleeper.cancelled()


async def test_timer_rearm_cancel_async_callback_and_closed_task_set() -> None:
    clock = VirtualClock(T0)
    tasks = TaskSet()
    fired: list[float] = []

    async def callback() -> None:
        fired.append(clock.monotonic())

    timer = Timer(clock, tasks, callback, name="t")
    timer.arm_in(5)
    await clock.settle()
    timer.arm_in(10)  # re-arm: the first run is cancelled
    assert timer.deadline == clock.monotonic() + 10
    await clock.advance(6)
    assert fired == []
    await clock.advance(5)
    assert fired == [10_010.0]
    assert timer.deadline is None
    timer.arm_in(1)
    timer.cancel()
    await clock.advance(2)
    assert fired == [10_010.0]
    assert clock.pending_sleepers == 0
    timer.arm_in(-3)  # a past deadline fires on the next loop turn
    await clock.settle()
    assert len(fired) == 2
    await tasks.close()
    timer.arm_in(1)
    assert timer.deadline is None


async def test_timer_can_rearm_itself_from_its_callback() -> None:
    clock = VirtualClock(T0)
    tasks = TaskSet()
    fired: list[float] = []

    def callback() -> None:
        fired.append(clock.monotonic())
        if len(fired) < 3:
            timer.arm_in(1)

    timer = Timer(clock, tasks, callback, name="t")
    timer.arm_in(1)
    await clock.advance(10)
    assert fired == [10_001.0, 10_002.0, 10_003.0]
    await tasks.close()


async def test_batcher_window_and_flush_now() -> None:
    clock = VirtualClock(T0)
    tasks = TaskSet()
    flushed: list[ChangeSet] = []
    batcher = Batcher(clock, tasks, 0.1, flushed.append)
    batcher.add(ChangeSet())  # empty: ignored
    assert not batcher.pending
    batcher.add(ChangeSet(changed={"a"}))
    await clock.advance(0.05)
    batcher.add(ChangeSet(changed={"b"}, removed={"b"}))
    assert batcher.pending
    await clock.advance(0.05)
    assert [c.changed for c in flushed] == [{"a", "b"}]
    batcher.add(ChangeSet(touched=True))
    batcher.flush_now()
    assert flushed[-1].touched
    batcher.flush_now()  # nothing open: no-op
    batcher.add(ChangeSet(changed={"c"}))
    batcher.discard()
    await clock.advance(1)
    assert len(flushed) == 2
    await tasks.close()


def _processor(
    clock: VirtualClock, stores: StoreSet, seen: list[ChangeSet], *, max_buffered: int = 100
) -> tuple[FrameProcessor, TaskSet]:
    tasks = TaskSet()
    processor = FrameProcessor(
        stores, clock, tasks, on_changes=seen.append, max_buffered=max_buffered
    )
    processor.start()
    processor.start()  # idempotent
    return processor, tasks


def _stores() -> StoreSet:
    from .conftest import rec

    stores = StoreSet()
    stores.replace(
        snapshot([rec("ZONA", "001", "", "T", "1"), rec("METEO", "0", "", "VENTO", "x")]), T0
    )
    return stores


async def test_processor_pause_buffers_and_resume_replays_in_order() -> None:
    clock = VirtualClock(T0)
    stores = _stores()
    seen: list[ChangeSet] = []
    processor, tasks = _processor(clock, stores, seen)
    processor.pause()
    assert processor.paused
    for value in ("2", "3", "4"):
        assert processor.enqueue(clock.utcnow(), clock.monotonic(), termo("ZONA.001..T", value))
    await clock.settle()
    assert seen == []
    assert processor.backlog == 3
    processor.resume()
    await clock.settle()
    assert stores.value("ZONA", "001", "", "T") == "4"
    assert [c.changed for c in seen] == [{"ZONA.001..T"}] * 3
    assert processor.buffered_max == 3
    await tasks.close()


async def test_processor_overflow_signal_and_clear() -> None:
    clock = VirtualClock(T0)
    processor, tasks = _processor(clock, _stores(), [], max_buffered=100)
    processor.pause()
    results = [
        processor.enqueue(clock.utcnow(), clock.monotonic(), termo("ZONA.001..T", str(i)))
        for i in range(101)
    ]
    assert results[:100] == [True] * 100
    assert results[100] is False
    processor.clear()
    assert processor.backlog == 0
    await tasks.close()


async def test_processor_survives_a_failing_apply_and_yields_on_long_backlogs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = VirtualClock(T0)

    class Flaky(StoreSet):
        calls = 0

        def apply_frame(self, frame: Any, received_at: datetime, received_mono: float) -> ChangeSet:
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("apply bug (test)")
            return super().apply_frame(frame, received_at, received_mono)

    stores = Flaky()
    stores.replace(snapshot([]), T0)
    seen: list[ChangeSet] = []
    processor, tasks = _processor(clock, stores, seen)
    processor.pause()
    for i in range(600):
        processor.enqueue(clock.utcnow(), clock.monotonic(), termo(f"ZONA.{i:03d}..T", "1"))
    with caplog.at_level(logging.ERROR, logger="aiorehom.sync"):
        processor.resume()
        await clock.settle()
    assert "failed to apply a WebSocket frame" in caplog.text
    assert len(seen) == 599
    await tasks.close()


async def test_processor_expiry_timer_waits_for_pause_and_backlog() -> None:
    clock = VirtualClock(T0)
    stores = _stores()
    seen: list[ChangeSet] = []
    processor, tasks = _processor(clock, stores, seen)
    processor.enqueue(clock.utcnow(), clock.monotonic(), termo("METEO.0..VENTO", kind="remove"))
    await clock.settle()
    deadline = stores.next_expiry()
    assert deadline == clock.monotonic() + 1.0
    processor.pause()  # due while paused: skipped, re-armed once the queue drains
    await clock.advance(1.5)
    assert stores.pending_removes == 1
    processor.resume()
    await clock.settle()
    assert stores.pending_removes == 0
    assert seen[-1].removed == {"METEO.0..VENTO"}
    # a second pending remove re-arms the timer; an unchanged deadline is kept
    processor.enqueue(clock.utcnow(), clock.monotonic(), termo("ZONA.001..T", kind="remove"))
    await clock.settle()
    armed = processor._expiry.deadline
    processor._arm_expiry()
    assert processor._expiry.deadline == armed
    await clock.advance(2)
    assert seen[-1].removed == {"ZONA.001..T"}
    await tasks.close()
    assert clock.pending_sleepers == 0


async def test_virtual_clock_internal_races() -> None:
    clock = VirtualClock(T0)
    # a sleeper cancelled after its entry was popped (resolved) but before it ran
    task = asyncio.create_task(clock.sleep(1))
    await clock.settle()
    _deadline, _seq, future = heapq.heappop(clock._heap)
    future.set_result(None)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # an already-cancelled future left on the heap is skipped
    loop = asyncio.get_running_loop()
    dead: asyncio.Future[None] = loop.create_future()
    dead.cancel()
    heapq.heappush(clock._heap, (1, -1, dead))
    assert clock.pending_sleepers == 0
    await clock.advance(timedelta(seconds=1).total_seconds())
    assert clock._heap == []
