"""Sync machinery: I/O protocols, the REST snapshot, the frame processor and timers.

* :class:`ReadTransport` is the only HTTP surface the client uses (GETs plus
  the single existing login); :class:`WsConnection`/:data:`WsConnector` the
  only WebSocket surface (receive and close, never send).
* :func:`fetch_snapshot` runs the four snapshot GETs sequentially and returns
  a validated, redacted :class:`~aiorehom.state.Snapshot` without applying it.
* :class:`FrameProcessor` owns the single frame queue.  Pausing it is the
  WS-first buffer: frames keep queuing while a snapshot is fetched and are
  applied afterwards, in arrival order.
* :class:`Batcher` groups the change sets of ``notify_batch_window`` seconds
  into one notification.
* :class:`Timer` and :class:`TaskSet` run every timer on the injected
  :class:`~aiorehom.clock.Clock` and make sure ``close()`` leaves no task behind.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from typing import Any, Protocol

from .clock import Clock
from .state import (
    ChangeSet,
    Snapshot,
    StoreSet,
    parse_config,
    parse_plant_conf,
    parse_record_rows,
)

__all__ = [
    "Batcher",
    "FrameProcessor",
    "ReadTransport",
    "TaskSet",
    "Timer",
    "WsConnection",
    "WsConnector",
    "fetch_snapshot",
]

_LOGGER = logging.getLogger(__name__)

#: Frames applied back to back before the processor yields to the event loop.
_YIELD_EVERY = 256


class ReadTransport(Protocol):
    """The HTTP surface of the read path (``ReadOnlyTransport`` satisfies it)."""

    @property
    def has_token(self) -> bool: ...
    async def login(self, username: str, password: str) -> None: ...
    async def get_alive(self, *, timeout: float | None = None) -> Any: ...  # noqa: ASYNC109
    async def get_config(self, *, timeout: float | None = None) -> Any: ...  # noqa: ASYNC109
    async def get_interface(self, *, timeout: float | None = None) -> Any: ...  # noqa: ASYNC109
    async def get_overrides(self, *, timeout: float | None = None) -> Any: ...  # noqa: ASYNC109
    async def get_plant_conf(self, *, timeout: float | None = None) -> Any: ...  # noqa: ASYNC109
    async def get_history(
        self,
        key: str,
        unita: str,
        gte: str,
        lte: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any: ...
    async def close(self) -> None: ...


class WsConnection(Protocol):
    """One open WebSocket connection that can only receive."""

    async def receive(self) -> Any | None:
        """Next decoded frame; ``None`` once the connection has ended."""
        ...

    async def close(self) -> None: ...


#: Opens one connection; raises ``RehomConnectionError``/``RehomTimeoutError``.
WsConnector = Callable[[], Awaitable[WsConnection]]


async def fetch_snapshot(
    transport: ReadTransport,
    clock: Clock,
    *,
    interface_timeout: float,
    request_timeout: float,
) -> Snapshot:
    """GET interface, overrides, plant conf and config (in this order) and validate them.

    Raises :class:`~aiorehom.exceptions.RehomResponseError` for a payload of the
    wrong shape, and whatever the transport raises.  Nothing is applied.
    """
    interface = parse_record_rows(
        await transport.get_interface(timeout=interface_timeout), path="/api/interface/"
    )
    overrides = parse_record_rows(
        await transport.get_overrides(timeout=request_timeout),
        path="/api/overrides/",
        overrides=True,
    )
    plant_conf = parse_plant_conf(await transport.get_plant_conf(timeout=request_timeout))
    raw_config = await transport.get_config(timeout=request_timeout)
    config_received_at = clock.utcnow()
    config = parse_config(raw_config)
    return Snapshot(
        interface=tuple(interface),
        overrides=tuple(overrides),
        plant_conf=plant_conf,
        config=config,
        config_received_at=config_received_at,
    )


class TaskSet:
    """Background tasks of one client, cancelled and awaited together by :meth:`close`."""

    def __init__(self, name: str = "aiorehom") -> None:
        self._name = name
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        return len(self._tasks)

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any] | None:
        """Start ``coro`` as a tracked task (``None``, and ``coro`` closed, once closed)."""
        if self._closed:
            coro.close()
            return None
        task = asyncio.get_running_loop().create_task(coro, name=f"{self._name}:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        err = task.exception()
        if err is not None:
            _LOGGER.error("background task %s failed", task.get_name(), exc_info=err)

    async def close(self) -> None:
        """Cancel every task (except the caller's own) once and wait until they are done.

        A task that is already being cancelled is only waited for, so a repeated
        ``close()`` never interrupts cleanup (a WebSocket close) still running.
        """
        self._closed = True
        current = asyncio.current_task()
        while True:
            tasks = [task for task in self._tasks if task is not current and not task.done()]
            if not tasks:
                return
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.wait(tasks)


class Timer:
    """Calls ``callback`` once at a monotonic deadline of ``clock``; re-armable."""

    def __init__(
        self,
        clock: Clock,
        tasks: TaskSet,
        callback: Callable[[], Awaitable[None] | None],
        *,
        name: str,
    ) -> None:
        self._clock = clock
        self._tasks = tasks
        self._callback = callback
        self._name = name
        self._task: asyncio.Task[Any] | None = None
        self._deadline: float | None = None

    @property
    def deadline(self) -> float | None:
        """Monotonic deadline while armed, else ``None``."""
        return self._deadline

    def arm_in(self, delay: float) -> None:
        self.arm_at(self._clock.monotonic() + max(0.0, delay))

    def arm_at(self, deadline: float) -> None:
        """(Re-)arm for ``deadline``; a pending run is cancelled first."""
        self.cancel()
        task = self._tasks.spawn(self._run(deadline), self._name)
        if task is not None:
            self._task = task
            self._deadline = deadline

    def cancel(self) -> None:
        task, self._task, self._deadline = self._task, None, None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _run(self, deadline: float) -> None:
        await self._clock.sleep(deadline - self._clock.monotonic())
        # A re-armed or cancelled timer's old task is cancelled before it resumes,
        # so reaching this point means this run is the armed one.
        self._task = None
        self._deadline = None
        result = self._callback()
        if inspect.isawaitable(result):
            await result


class Batcher:
    """Groups change sets into one flush per ``window`` seconds (the first change opens it)."""

    def __init__(
        self,
        clock: Clock,
        tasks: TaskSet,
        window: float,
        flush: Callable[[ChangeSet], None],
    ) -> None:
        self._window = window
        self._flush = flush
        self._batch: ChangeSet | None = None
        self._timer = Timer(clock, tasks, self.flush_now, name="batch")

    @property
    def pending(self) -> bool:
        return self._batch is not None

    def add(self, changes: ChangeSet) -> None:
        if changes.empty():
            return
        if self._batch is None:
            self._batch = ChangeSet()
            self._timer.arm_in(self._window)
        self._batch.merge(changes)

    def flush_now(self) -> None:
        """Publish the open batch, if any, right now."""
        self._timer.cancel()
        batch, self._batch = self._batch, None
        if batch is not None:
            self._flush(batch)

    def discard(self) -> None:
        self._timer.cancel()
        self._batch = None


class FrameProcessor:
    """The single frame queue and the task that applies it to the stores.

    :meth:`pause` stops applying (frames keep queuing: the WS-first buffer);
    :meth:`resume` replays them in arrival order.  While the queue is empty and
    processing is not paused, pending removes expire from a timer.
    """

    def __init__(
        self,
        stores: StoreSet,
        clock: Clock,
        tasks: TaskSet,
        *,
        on_changes: Callable[[ChangeSet], None],
        max_buffered: int,
    ) -> None:
        self._stores = stores
        self._clock = clock
        self._tasks = tasks
        self._on_changes = on_changes
        self._max_buffered = max_buffered
        self._queue: deque[tuple[datetime, float, Any]] = deque()
        self._resumed = asyncio.Event()
        self._resumed.set()
        self._wakeup = asyncio.Event()
        self._expiry = Timer(clock, tasks, self._on_expiry, name="expire")
        self._task: asyncio.Task[Any] | None = None
        self.buffered_max = 0

    @property
    def paused(self) -> bool:
        return not self._resumed.is_set()

    @property
    def backlog(self) -> int:
        return len(self._queue)

    def start(self) -> None:
        if self._task is None:
            self._task = self._tasks.spawn(self._run(), "processor")

    def pause(self) -> None:
        self._resumed.clear()

    def resume(self) -> None:
        self._resumed.set()
        self._wakeup.set()

    def clear(self) -> None:
        """Drop every queued frame (buffer overflow)."""
        self._queue.clear()

    def enqueue(self, received_at: datetime, received_mono: float, frame: Any) -> bool:
        """Queue one frame; ``False`` when a paused queue exceeds ``max_buffered``."""
        self._queue.append((received_at, received_mono, frame))
        if self.paused:
            size = len(self._queue)
            self.buffered_max = max(self.buffered_max, size)
            if size > self._max_buffered:
                return False
        self._wakeup.set()
        return True

    async def _run(self) -> None:
        applied = 0
        while True:
            if not self._resumed.is_set():
                await self._resumed.wait()
                continue
            if not self._queue:
                self._arm_expiry()
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            received_at, received_mono, frame = self._queue.popleft()
            try:
                changes = self._stores.apply_frame(frame, received_at, received_mono)
            except Exception:  # a bug must not stop the processor
                _LOGGER.exception("failed to apply a WebSocket frame")
                continue
            self._on_changes(changes)
            applied += 1
            if applied % _YIELD_EVERY == 0:
                await self._clock.sleep(0)

    def _arm_expiry(self) -> None:
        deadline = self._stores.next_expiry()
        if deadline is None:
            self._expiry.cancel()
        elif deadline != self._expiry.deadline:
            self._expiry.arm_at(deadline)

    def _on_expiry(self) -> None:
        if self.paused or self._queue:
            return  # the processor re-arms once the queue has drained
        self._on_changes(self._stores.expire(self._clock.monotonic()))
        self._arm_expiry()
