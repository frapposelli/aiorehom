"""``RehomClient``: a live client of one Rehom RadiaxWeb controller (reads plus opt-in writes).

The client keeps a live, immutable :class:`~aiorehom.models.RehomState`:

* **WS-first sync.**  ``connect()`` opens the WebSocket, buffers its frames,
  fetches the REST snapshot (interface, overrides, plant conf, config),
  replaces the stores and replays the buffer.  Every resync (periodic,
  after a reconnect, on an ``/alive/`` version change, or on request) uses the
  same procedure without closing the socket, at most once per
  ``min_resync_interval``.
* **Notifications.**  Changes within ``notify_batch_window`` are published as
  one :class:`~aiorehom.models.StateUpdate`; a frame whose value is
  semantically unchanged never notifies, and neither does a resync that
  finds no difference.  The state is also rebuilt at ``next_change_at``
  (slot boundaries, override expiry, alarm debounce, heartbeat deadlines).
* **Availability.**  ``/alive/`` is polled; the WebSocket reconnects with
  backoff; while it is down a REST snapshot is taken periodically.  A socket
  that stays silent for ``ws_idle_timeout`` (the controller sends a heartbeat
  frame every minute) is treated as dead and reconnected, without sending
  anything.  A failed resync is retried after ``min_resync_interval``
  (doubling up to ``resync_interval``) and counted in :attr:`RehomClient.stats`.

There is no re-login.  Unless created with ``allow_writes=True``, the client
uses only :class:`~aiorehom.sync.ReadTransport` methods (GETs plus at most one
login); writes are the ``set_*`` methods (see :class:`RehomClient`).  The
WebSocket is receive-only.  All timing goes through the injected
:class:`~aiorehom.clock.Clock`.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Final, Protocol, Self

import aiohttp

from .builder import StateBuilder
from .clock import (
    SKEW_DEADBAND,
    Clock,
    DeviceClock,
    SystemClock,
    async_load_zone,
    measured_skew,
)
from .enums import (
    ConnectionState,
    HistorySeries,
    MasterPreset,
    UpdateReason,
    VmcMode,
    ZoneSetp,
)
from .exceptions import (
    ForbiddenRequestError,
    RehomConnectionError,
    RehomError,
    RehomNotReadyError,
    RehomResponseError,
    RehomTimeoutError,
    RehomWriteNotConfirmedError,
    RehomWriteRefusedError,
)
from .logic.parse import parse_device_timestamp
from .models import HistorySample, RehomState, StateUpdate, SyncStats
from .state import ChangeSet, RowView, StoreSet, StoreView, parse_config
from .store import OVERRIDE_GRUPPO
from .sync import (
    Batcher,
    FrameProcessor,
    ReadTransport,
    TaskSet,
    Timer,
    WsConnection,
    WsConnector,
    fetch_snapshot,
)
from .transport import HISTORY_TIME_FORMAT, ReadOnlyTransport, validate_host
from .values import parse_float, values_equal
from .websocket import STABLE_CONNECTION_S, backoff_delay, open_receive_only
from .writes import (
    WritePlan,
    plan_comfort_temperature,
    plan_house_preset,
    plan_predictive,
    plan_temporary_comfort,
    plan_vmc_fan,
    plan_vmc_mode,
    plan_zone_mode,
    plan_zone_offset,
)

__all__ = ["ClientOptions", "RehomClient", "StateBuilderProtocol"]

_LOGGER = logging.getLogger(__name__)

MAX_HISTORY_SPAN: Final = timedelta(days=7)
HISTORY_CHUNK: Final = timedelta(hours=24)
_UNIT_RE: Final = re.compile(r"\d{3}")
_TEMPO_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?")
_ONE_SECOND: Final = timedelta(seconds=1)
#: Request-log lines the client's own transport keeps (the most recent ones).
_CLIENT_REQUEST_LOG_SIZE: Final = 100
#: Cap of the failed-sync retry doubling exponent (the delay is capped anyway).
_MAX_RETRY_DOUBLINGS: Final = 16


# ---------------------------------------------------------------------------
# Options and builder protocol
# ---------------------------------------------------------------------------


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _integer(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an int")
    return value


#: field -> (low, low_inclusive, high) ; ``None`` = unbounded.
_FLOAT_BOUNDS: Final[dict[str, tuple[float | None, bool, float | None]]] = {
    "alive_interval": (15.0, True, 300.0),
    "alive_timeout": (5.0, True, None),
    "config_interval": (60.0, True, None),
    "resync_interval": (60.0, True, None),
    "min_resync_interval": (60.0, True, None),
    "interface_timeout": (15.0, True, None),
    "request_timeout": (5.0, True, None),
    "request_min_interval": (1.0, True, None),
    "ws_connect_timeout": (0.0, False, None),
    "rest_fallback_after": (30.0, True, None),
    "rest_fallback_interval": (60.0, True, None),
    "remove_coalesce_window": (0.0, False, 5.0),
    "notify_batch_window": (0.0, True, 1.0),
    "alarm_debounce": (30.0, True, None),
    "heartbeat_stale_after": (120.0, True, None),
    "forecast_burst_gap": (0.0, False, None),
    "history_chunk_spacing": (5.0, True, None),
    "clock_tick_margin": (0.0, True, 1.0),
    "write_confirm_timeout": (5.0, True, 120.0),
    "write_poll_interval": (0.0, False, 5.0),
}
#: Like ``_FLOAT_BOUNDS``, for fields where ``None`` disables the feature.
_OPTIONAL_FLOAT_BOUNDS: Final[dict[str, tuple[float | None, bool, float | None]]] = {
    "ws_idle_timeout": (120.0, True, None),
}
_INT_BOUNDS: Final[dict[str, int]] = {"alive_failures_unavailable": 1, "max_buffered_frames": 100}


@dataclass(frozen=True, slots=True, kw_only=True)
class ClientOptions:
    """Timing and buffering knobs (seconds unless stated otherwise).

    ``ws_idle_timeout``: a live WebSocket that delivers no frame for this long
    is closed and reconnected (a half-open socket never ends on its own, and the
    socket is receive-only, so silence is the only sign).  The controller
    pulses ``WATCHDOG_MASTER`` every 60 s, so the default is three periods.
    ``None`` disables the watchdog (for a controller without that pulse).
    """

    alive_interval: float = 30.0
    alive_timeout: float = 5.0
    alive_failures_unavailable: int = 3
    config_interval: float = 300.0
    resync_interval: float = 600.0
    min_resync_interval: float = 60.0
    interface_timeout: float = 15.0
    request_timeout: float = 10.0
    request_min_interval: float = 1.0
    ws_connect_timeout: float = 10.0
    ws_idle_timeout: float | None = 180.0
    rest_fallback_after: float = 60.0
    rest_fallback_interval: float = 60.0
    remove_coalesce_window: float = 1.0
    notify_batch_window: float = 0.1
    alarm_debounce: float = 60.0
    heartbeat_stale_after: float = 180.0
    forecast_burst_gap: float = 60.0
    max_buffered_frames: int = 5000
    history_chunk_spacing: float = 5.0
    clock_tick_margin: float = 0.05
    write_confirm_timeout: float = 20.0
    write_poll_interval: float = 0.25

    def __post_init__(self) -> None:
        bounds = [
            *_FLOAT_BOUNDS.items(),
            *(
                item
                for item in _OPTIONAL_FLOAT_BOUNDS.items()
                if getattr(self, item[0]) is not None
            ),
        ]
        for name, (low, inclusive, high) in bounds:
            value = _number(name, getattr(self, name))
            if low is not None and (value < low if inclusive else value <= low):
                raise ValueError(f"{name} must be {'>=' if inclusive else '>'} {low:g}")
            if high is not None and value > high:
                raise ValueError(f"{name} must be <= {high:g}")
        for name, low_int in _INT_BOUNDS.items():
            if _integer(name, getattr(self, name)) < low_int:
                raise ValueError(f"{name} must be >= {low_int}")


class WriteTransport(Protocol):
    """The write half of the transport (``ReadOnlyTransport`` with ``allow_writes=True``)."""

    async def post_bulk_update(
        self,
        path: str,
        records: Any,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> int: ...


class StateBuilderProtocol(Protocol):
    """Turns the stores into a :class:`RehomState` (``aiorehom.builder.StateBuilder``)."""

    def build(self, view: StoreView, *, now: datetime, clock: DeviceClock) -> RehomState: ...


# ---------------------------------------------------------------------------
# Callback registry
# ---------------------------------------------------------------------------


class _Subscription[T]:
    __slots__ = ("active", "callback")

    def __init__(self, callback: Callable[[T], None]) -> None:
        self.callback = callback
        self.active = True


class _Callbacks[T]:
    """Synchronous callbacks, called in registration order; errors are logged."""

    def __init__(self, what: str) -> None:
        self._what = what
        self._entries: list[_Subscription[T]] = []

    def add(self, callback: Callable[[T], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("callback must be callable")
        entry = _Subscription(callback)
        self._entries.append(entry)

        def remove() -> None:
            if entry.active:
                entry.active = False
                self._entries.remove(entry)

        return remove

    def call(self, arg: T) -> None:
        for entry in list(self._entries):
            if not entry.active:
                continue
            try:
                entry.callback(arg)
            except Exception:
                _LOGGER.exception("%s callback %r raised", self._what, entry.callback)


class _Streak:
    """Logs the first failure of a streak as a warning and the rest at debug level."""

    def __init__(self, what: str) -> None:
        self._what = what
        self._failing = False

    def failure(self, err: BaseException) -> None:
        level = logging.DEBUG if self._failing else logging.WARNING
        _LOGGER.log(level, "%s failed: %s", self._what, err)
        self._failing = True

    def success(self) -> None:
        if self._failing:
            _LOGGER.info("%s recovered", self._what)
        self._failing = False


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class RehomClient:
    """Client of one Rehom controller (see the module docstring).

    Writes need ``allow_writes=True`` (an exact bool).  The ``set_*`` methods run
    one at a time in call order, each planned against the latest state (see
    :mod:`aiorehom.writes`), sent once, never retried, and confirmed from the
    controller's echo or, failing that, one resync.  They return ``False`` when
    nothing needed sending.  :class:`~aiorehom.exceptions.RehomWriteNotConfirmedError`
    means the POST was accepted but not confirmed; any other error raised by
    the POST itself (a timeout in particular) means it may or may not have
    landed.

    ``session``: an optional caller-owned ``aiohttp.ClientSession`` for the REST
    requests (for example Home Assistant's shared session).  It is never
    modified or closed.  The WebSocket always uses a private session (its own
    cookie jar, timeouts and request guard); with ``session`` given, that
    private session runs on ``session.connector`` (borrowed, never closed), so
    the WebSocket resolves the host exactly like REST does (for example Home
    Assistant's mDNS-capable resolver for ``*.local`` names).
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 8000,
        ws_port: int = 8000,
        username: str | None = None,
        password: str | None = None,
        session: aiohttp.ClientSession | None = None,
        options: ClientOptions | None = None,
        transport: ReadTransport | None = None,
        ws_connector: WsConnector | None = None,
        clock: Clock | None = None,
        builder: StateBuilderProtocol | None = None,
        rng: Callable[[], float] | None = None,
        allow_writes: bool = False,
    ) -> None:
        self._host = validate_host(host)
        # The one write opt-in: an exact bool, never coerced (bool("false") is True).
        flag: object = allow_writes
        if not isinstance(flag, bool):
            raise TypeError("allow_writes must be a bool")
        self._allow_writes = flag
        for name, value in (("port", port), ("ws_port", ws_port)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
                raise ValueError(f"invalid {name}")
        if options is not None and not isinstance(options, ClientOptions):
            raise ValueError("options must be a ClientOptions")
        self._options = options if options is not None else ClientOptions()
        opts = self._options
        self._clock: Clock = clock if clock is not None else SystemClock()
        for name, cred in (("username", username), ("password", password)):
            if cred is not None and not isinstance(cred, str):
                raise ValueError(f"{name} must be a string")
        if transport is None:
            if username is None or password is None:
                raise ValueError("username and password are required without an injected transport")
            owned = ReadOnlyTransport(
                self._host,
                port,
                session=session,
                min_interval=opts.request_min_interval,
                timeout=opts.request_timeout,
                allow_login=True,
                allow_writes=self._allow_writes,
                clock=self._clock.monotonic,
                sleep=self._clock.sleep,
                request_log_size=_CLIENT_REQUEST_LOG_SIZE,
            )
            self._transport: ReadTransport = owned
            self._owns_transport = True
            if ws_connector is None:
                ws_connector = partial(
                    open_receive_only,
                    self._host,
                    ws_port,
                    connect_timeout=opts.ws_connect_timeout,
                    gate=owned.pacing_slot,
                    # Same name resolution and connection pool as the REST session.
                    connector=None if session is None else session.connector,
                )
        else:
            if session is not None:
                raise ValueError("session must be None when a transport is injected")
            if ws_connector is None:
                raise ValueError("ws_connector must be injected together with a transport")
            if self._allow_writes is True and not callable(
                getattr(transport, "post_bulk_update", None)
            ):
                raise ValueError("allow_writes needs a transport with post_bulk_update()")
            self._transport = transport
            self._owns_transport = False
        self._ws_connector: WsConnector = ws_connector
        self._username = username
        self._password = password
        self._rng: Callable[[], float] = rng if rng is not None else random.random
        self._builder: StateBuilderProtocol = (
            builder
            if builder is not None
            else StateBuilder(
                alarm_debounce=timedelta(seconds=opts.alarm_debounce),
                heartbeat_stale_after=timedelta(seconds=opts.heartbeat_stale_after),
            )
        )

        self._stores = StoreSet(
            remove_coalesce_window=opts.remove_coalesce_window,
            forecast_burst_gap=opts.forecast_burst_gap,
        )
        self._tasks = TaskSet(f"aiorehom[{self._host}]")
        self._batcher = Batcher(
            self._clock, self._tasks, opts.notify_batch_window, self._flush_batch
        )
        self._processor = FrameProcessor(
            self._stores,
            self._clock,
            self._tasks,
            on_changes=self._batcher.add,
            max_buffered=opts.max_buffered_frames,
        )
        self._tick = Timer(self._clock, self._tasks, self._on_tick, name="tick")
        self._periodic = Timer(self._clock, self._tasks, self._on_periodic, name="resync")
        self._fallback = Timer(self._clock, self._tasks, self._on_fallback_due, name="fallback")
        self._ws_idle = Timer(self._clock, self._tasks, self._on_ws_idle, name="ws-idle")
        self._subscribers: _Callbacks[StateUpdate] = _Callbacks("subscriber")
        self._conn_callbacks: _Callbacks[ConnectionState] = _Callbacks("connection")

        self._state: RehomState | None = None
        self._device_clock: DeviceClock | None = None
        self._conn_state = ConnectionState.DISCONNECTED
        self._connect_called = False
        self._connecting = False
        self._synced = False
        self._closed = False
        self._transport_closed = False
        self._ws_conn: WsConnection | None = None
        self._ws_live = False
        self._ws_pump: asyncio.Task[Any] | None = None
        self._ws_last_frame: float | None = None  # monotonic, while a pump runs
        self._raw_skew: timedelta | None = None  # measured skew behind _device_clock
        self._fallback_failed = False
        self._alive_failures = 0
        self._last_version: str | None = None
        self._last_sync_done: float | None = None
        self._sync_running = False
        self._sync_pending: UpdateReason | None = None
        self._sync_wakeup = asyncio.Event()
        self._sync_waiters: list[asyncio.Future[None]] = []
        self._alive_streak = _Streak("GET /api/alive/")
        self._config_streak = _Streak("GET /api/config/")
        self._sync_streak = _Streak("resync")
        self._ws_streak = _Streak("WebSocket connect")
        # counters that are not frame accounting (StoreSet.counters holds those)
        self._frames_received = 0
        self._syncs = 0
        self._resyncs = 0
        self._fallbacks = 0
        self._reconnects = 0
        self._alive_failures_total = 0
        self._notifications = 0
        self._sync_failures = 0
        self._sync_failure_streak = 0
        self._last_sync_error: str | None = None
        self._ws_idle_timeouts = 0
        # writes: one at a time, in call order (asyncio.Lock wakes waiters FIFO)
        self._write_lock = asyncio.Lock()

    # -- public properties -------------------------------------------------

    def __repr__(self) -> str:
        return f"RehomClient(host={self._host!r}, state={self._conn_state.value})"

    @property
    def host(self) -> str:
        """The controller's host name or IP address, as validated at construction."""
        return self._host

    @property
    def options(self) -> ClientOptions:
        """The timing and buffering options in use."""
        return self._options

    @property
    def state(self) -> RehomState:
        """The latest state; :class:`RehomNotReadyError` before the first complete sync.

        After :meth:`close` it keeps returning the last state.
        """
        if self._state is None:
            raise RehomNotReadyError("no complete sync yet: call connect() first")
        return self._state

    @property
    def connection_state(self) -> ConnectionState:
        """``CONNECTED`` (WebSocket live), ``DEGRADED`` (REST only), ``UNAVAILABLE``...

        ``DISCONNECTED`` before :meth:`connect`, ``CONNECTING`` during it,
        ``UNAVAILABLE`` after ``alive_failures_unavailable`` failed ``/alive/``
        polls or a failed REST fallback, ``CLOSED`` after :meth:`close`.
        :meth:`on_connection_change` reports every transition.
        """
        return self._conn_state

    @property
    def available(self) -> bool:
        """``connection_state`` is ``CONNECTED`` or ``DEGRADED`` (the state is being kept)."""
        return self._conn_state in (ConnectionState.CONNECTED, ConnectionState.DEGRADED)

    @property
    def device_clock(self) -> DeviceClock | None:
        """The controller's time zone and skew (``None`` before the first sync)."""
        return self._device_clock

    @property
    def last_synced_at(self) -> datetime | None:
        """When the stores last took a complete REST snapshot (aware UTC), else ``None``.

        Updated by every successful sync, resync or fallback, including one that
        found no difference (and so published nothing, leaving
        ``state.synced_at`` older).
        """
        return self._stores.synced_at if self._stores.has_snapshot else None

    @property
    def stats(self) -> SyncStats:
        """Frame, sync and notification counters (see :class:`SyncStats`)."""
        counters = self._stores.counters
        return SyncStats(
            frames_received=self._frames_received,
            frames_ignored=counters.frames_ignored,
            noop_updates=counters.noop_updates,
            changed_updates=counters.changed_updates,
            removes_coalesced=counters.removes_coalesced,
            removes_applied=counters.removes_applied,
            forecast_frames=counters.forecast_frames,
            buffered_max=self._processor.buffered_max,
            syncs=self._syncs,
            resyncs=self._resyncs,
            fallbacks=self._fallbacks,
            reconnects=self._reconnects,
            alive_failures=self._alive_failures_total,
            notifications=self._notifications,
            sync_failures=self._sync_failures,
            sync_failure_streak=self._sync_failure_streak,
            last_sync_error=self._last_sync_error,
            ws_idle_timeouts=self._ws_idle_timeouts,
        )

    @property
    def allow_writes(self) -> bool:
        """Whether this client was created with ``allow_writes=True``."""
        return self._allow_writes

    def subscribe(self, callback: Callable[[StateUpdate], None]) -> Callable[[], None]:
        """Call ``callback(update)`` after each published update; returns ``unsubscribe``."""
        return self._subscribers.add(callback)

    def on_connection_change(
        self, callback: Callable[[ConnectionState], None]
    ) -> Callable[[], None]:
        """Call ``callback(state)`` on every transition of :attr:`connection_state`."""
        return self._conn_callbacks.add(callback)

    def next_change_at(self) -> datetime | None:
        """``state.next_change_at`` (the client re-publishes at that instant itself)."""
        return None if self._state is None else self._state.next_change_at

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- connect -------------------------------------------------------------

    async def connect(self) -> None:
        """Log in if needed, open the WebSocket, take the first snapshot and go live.

        Callable once.  On failure everything started is cleaned up, the client
        stays ``DISCONNECTED`` and must be closed.
        """
        if self._connect_called:
            raise RuntimeError("connect() can be called only once per client")
        if self._closed:
            raise RehomNotReadyError("the client is closed")
        self._connect_called = True
        self._connecting = True
        self._update_connection_state()
        try:
            await self._connect()
        except BaseException:
            self._connecting = False
            self._drop_credentials()
            await self._stop_background()
            self._update_connection_state()
            raise
        self._connecting = False
        self._synced = True
        self._update_connection_state()

    async def _connect(self) -> None:
        opts = self._options
        alive = await self._transport.get_alive(timeout=opts.alive_timeout)
        self._check_open()
        self._apply_alive(alive, trigger=False)
        username, password = self._username, self._password
        self._drop_credentials()
        if username is not None and password is not None and not self._transport.has_token:
            await self._transport.login(username, password)
            self._check_open()
        # WS first: frames queue (paused processor) while the snapshot is fetched.
        self._processor.pause()
        self._processor.start()
        conn: WsConnection | None = None
        try:
            conn = await self._ws_connector()
        except RehomConnectionError as err:
            self._ws_streak.failure(err)
            _LOGGER.warning("WebSocket unavailable at connect; continuing REST-only (degraded)")
        self._ws_conn = conn  # owned by the WS loop from here on; cleanup closes it otherwise
        self._check_open()
        if conn is not None:
            self._ws_streak.success()
        self._tasks.spawn(self._ws_loop(conn), "ws")
        await self._run_sync(UpdateReason.SYNC)
        self._check_open()
        self._tasks.spawn(self._alive_loop(), "alive")
        self._tasks.spawn(self._config_loop(), "config")
        self._tasks.spawn(self._sync_loop(), "sync")

    def _check_open(self) -> None:
        if self._closed:
            raise RehomNotReadyError("the client was closed during connect()")

    def _drop_credentials(self) -> None:
        self._username = None
        self._password = None

    # -- close ---------------------------------------------------------------

    async def close(self) -> None:
        """Stop everything; idempotent.  The last state stays readable.

        Every call finishes whatever is still running, so a ``close()`` that was
        itself cancelled (the cancellation propagates) can simply be repeated.
        A write not yet sent is refused (:class:`RehomNotReadyError`); one
        already sent stops waiting for its confirmation and raises
        :class:`RehomWriteNotConfirmedError`.  A POST already in flight is not
        awaited: it may or may not land.
        """
        if not self._closed:
            self._closed = True
            self._drop_credentials()
            self._update_connection_state()
            self._fail_waiters(RehomNotReadyError("the client was closed"))
        await self._stop_background()
        if self._owns_transport and not self._transport_closed:
            self._transport_closed = True
            await self._transport.close()

    async def _stop_background(self) -> None:
        self._tick.cancel()
        self._periodic.cancel()
        self._fallback.cancel()
        self._ws_idle.cancel()
        self._batcher.discard()
        await self._tasks.close()
        conn, self._ws_conn = self._ws_conn, None
        if conn is not None:  # never handed to a running WS loop
            await _close_quietly(conn)
        self._ws_live = False
        self._stores.set_live_since(None)

    def _fail_waiters(self, err: RehomError) -> None:
        waiters, self._sync_waiters = self._sync_waiters, []
        _settle(waiters, err)

    # -- publishing ----------------------------------------------------------

    def _publish(self, reason: UpdateReason, changes: ChangeSet) -> bool:
        """Build the state and publish it when due; returns whether it was published."""
        if self._device_clock is None:
            return False
        now = self._clock.utcnow()
        new = self._builder.build(self._stores, now=now, clock=self._device_clock)
        previous = self._state
        if reason is not UpdateReason.SYNC and not changes.reportable() and new == previous:
            return False
        self._state = new
        self._notifications += 1
        update = StateUpdate(
            state=new,
            previous=None if reason is UpdateReason.SYNC else previous,
            reason=reason,
            at=now,
            changed=frozenset(changes.changed),
            removed=frozenset(changes.removed),
            conf_changed=frozenset(changes.conf_changed),
            config_changed=frozenset(changes.config_changed),
            forecast_changed=changes.forecast_changed,
        )
        self._subscribers.call(update)
        self._arm_tick()
        return True

    def _safe_publish(self, reason: UpdateReason, changes: ChangeSet) -> bool:
        if self._closed:
            return False
        try:
            return self._publish(reason, changes)
        except Exception:
            _LOGGER.exception("building the state failed (%s)", reason.value)
            return False

    def _flush_batch(self, batch: ChangeSet) -> None:
        self._safe_publish(UpdateReason.FRAMES, batch)

    def _refresh(self) -> None:
        """Rebuild after a path-less change (WS live/down); publish only if the state moved."""
        if self._synced or self._state is not None:
            self._safe_publish(UpdateReason.CLOCK, ChangeSet())

    def _arm_tick(self) -> None:
        state = self._state
        if self._closed or state is None or state.next_change_at is None:
            self._tick.cancel()
            return
        delay = (state.next_change_at - self._clock.utcnow()).total_seconds()
        self._tick.arm_in(delay + self._options.clock_tick_margin)

    def _on_tick(self) -> None:
        if self._safe_publish(UpdateReason.CLOCK, ChangeSet()):
            return
        # Nothing moved.  Re-arm only if the instant is still ahead (an early wake-up).
        state = self._state
        if state is not None and state.next_change_at is not None:
            margin = timedelta(seconds=self._options.clock_tick_margin)
            if state.next_change_at + margin > self._clock.utcnow():
                self._arm_tick()

    # -- connection state ----------------------------------------------------

    def _compute_connection_state(self) -> ConnectionState:
        if self._closed:
            return ConnectionState.CLOSED
        if not self._synced:
            return ConnectionState.CONNECTING if self._connecting else ConnectionState.DISCONNECTED
        if self._alive_failures >= self._options.alive_failures_unavailable or (
            not self._ws_live and self._fallback_failed
        ):
            return ConnectionState.UNAVAILABLE
        return ConnectionState.CONNECTED if self._ws_live else ConnectionState.DEGRADED

    def _update_connection_state(self) -> None:
        new = self._compute_connection_state()
        if new is self._conn_state:
            return
        self._conn_state = new
        self._conn_callbacks.call(new)

    # -- sync ------------------------------------------------------------------

    def _request_sync(self, kind: UpdateReason) -> None:
        """Ask for a sync; requests coalesce into one pending sync (RESYNC wins)."""
        if self._closed:
            return
        if self._sync_pending is None or kind is UpdateReason.RESYNC:
            self._sync_pending = kind
        self._sync_wakeup.set()

    async def resync(self) -> None:
        """Run a full resync and return when it has completed (see ``min_resync_interval``)."""
        if self._closed or not self._synced:
            raise RehomNotReadyError("resync() needs a connected, open client")
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sync_waiters.append(waiter)
        self._request_sync(UpdateReason.RESYNC)
        await waiter

    async def _sync_loop(self) -> None:
        while True:
            await self._sync_wakeup.wait()
            await self._wait_min_interval()  # frames keep being applied live meanwhile
            self._sync_wakeup.clear()
            kind, self._sync_pending = self._sync_pending, None
            waiters, self._sync_waiters = self._sync_waiters, []
            if kind is None:  # pragma: no cover - a wake-up always comes with a request
                continue
            try:
                await self._run_sync(kind)
            except ForbiddenRequestError as err:
                _settle(waiters, err)
                raise
            except Exception as err:  # RehomError, or a bug that must not stop future syncs
                if isinstance(err, RehomError):
                    self._sync_streak.failure(err)
                else:
                    _LOGGER.exception("%s failed", kind.value)
                _settle(waiters, err)
            except BaseException:  # cancelled by close(): nobody may wait forever
                _settle(waiters, RehomNotReadyError("the client was closed"))
                raise
            else:
                self._sync_streak.success()
                _settle(waiters, None)

    async def _wait_min_interval(self) -> None:
        while self._last_sync_done is not None:
            remaining = (
                self._last_sync_done + self._options.min_resync_interval - self._clock.monotonic()
            )
            if remaining <= 0:
                return
            await self._clock.sleep(remaining)

    async def _run_sync(self, kind: UpdateReason) -> None:
        opts = self._options
        self._sync_running = True
        # Every kind buffers.  A FALLBACK runs while the WS is down, but the socket
        # can come back during it: those frames are newer than the snapshot and
        # must be applied after it, not overwritten by it (invariant 4.9.2).
        self._processor.pause()
        self._batcher.flush_now()  # an open FRAMES batch is published against the old stores
        failed = False
        try:
            try:
                snapshot = await fetch_snapshot(
                    self._transport,
                    self._clock,
                    interface_timeout=opts.interface_timeout,
                    request_timeout=opts.request_timeout,
                )
            finally:
                self._last_sync_done = self._clock.monotonic()
            # A new TIMEZONE is read from the tz database in a worker thread.
            await async_load_zone(snapshot.config)
            if self._closed:
                raise RehomNotReadyError("the client was closed during a sync")
            diff = self._stores.replace(snapshot, self._clock.utcnow())
            self._device_clock = self._next_device_clock(
                snapshot.config, snapshot.config_received_at
            )
            self._fallback_failed = False
            if kind is UpdateReason.SYNC:
                self._syncs += 1
                self._publish(kind, diff)
            else:
                if kind is UpdateReason.FALLBACK:
                    self._fallbacks += 1
                else:
                    self._resyncs += 1
                self._safe_publish(kind, diff)
        except Exception as err:
            failed = True
            self._sync_failures += 1
            self._sync_failure_streak += 1
            self._last_sync_error = type(err).__name__
            if kind is UpdateReason.FALLBACK and isinstance(err, RehomError):
                self._fallback_failed = True
            raise
        else:
            self._sync_failure_streak = 0
            self._last_sync_error = None
        finally:
            self._sync_running = False
            self._processor.resume()  # the buffer replays in arrival order
            if not self._closed:
                self._periodic.arm_in(self._next_resync_delay(kind, failed=failed))
                if not self._ws_live and self._fallback.deadline is None:
                    self._fallback.arm_in(opts.rest_fallback_interval)
            self._update_connection_state()

    def _next_resync_delay(self, kind: UpdateReason, *, failed: bool) -> float:
        """Delay of the next periodic resync after a sync of ``kind`` ended.

        After a success: ``resync_interval``.  After a failed resync (the stores
        may miss deltas, for example those lost while the WS was down): a retry
        after ``min_resync_interval``, doubling per consecutive failure up to
        ``resync_interval``.  A failed FALLBACK is retried by its own timer.
        """
        opts = self._options
        if not failed or kind is UpdateReason.FALLBACK:
            return opts.resync_interval
        doublings = min(self._sync_failure_streak - 1, _MAX_RETRY_DOUBLINGS)
        return min(opts.resync_interval, opts.min_resync_interval * 2.0**doublings)

    def _next_device_clock(self, config: Mapping[str, Any], received_at: datetime) -> DeviceClock:
        """The device clock after a ``/config/`` read, with hysteresis on the skew.

        The current clock is kept unless the zone changed or the skew really
        moved: the raw measured skew (before the dead band) differs by at least
        :data:`SKEW_DEADBAND` from the one the current clock was taken from.
        Comparing the dead-banded values instead would flap for a constant skew
        of 2-3 s (``LOCAL_TIME`` is truncated to the second, so successive
        measurements wander over 1 s across the dead band's edge).
        """
        new = DeviceClock.from_config(config, received_at)
        raw = measured_skew(config, received_at)
        old, baseline = self._device_clock, self._raw_skew
        if old is not None and old.tz == new.tz and old.tz_name == new.tz_name:
            if raw is not None and baseline is not None:
                moved = abs(raw - baseline) >= SKEW_DEADBAND
            else:
                moved = abs(new.skew - old.skew) >= SKEW_DEADBAND
            if not moved:
                if baseline is None:
                    self._raw_skew = raw
                return old
        self._raw_skew = raw
        return new

    def _on_periodic(self) -> None:
        self._request_sync(UpdateReason.RESYNC)

    def _on_fallback_due(self) -> None:
        if not self._ws_live:
            self._request_sync(UpdateReason.FALLBACK)

    # -- WebSocket -------------------------------------------------------------

    async def _ws_loop(self, conn: WsConnection | None) -> None:
        attempt = 0
        if conn is None:  # the handshake in connect() failed
            self._on_ws_down()
            attempt = 1
            await self._clock.sleep(backoff_delay(0, self._rng))
        while True:
            reconnected = conn is None
            if conn is None:
                try:
                    conn = await self._ws_connector()
                except RehomConnectionError as err:
                    self._ws_streak.failure(err)
                    delay = backoff_delay(attempt, self._rng)
                    attempt += 1
                    await self._clock.sleep(delay)
                    continue
                self._ws_streak.success()
                self._reconnects += 1
            up_at = self._clock.monotonic()
            self._ws_conn = conn
            self._on_ws_live(reconnected=reconnected)
            try:
                await self._pump_watched(conn)
            finally:
                self._ws_conn = None
                await _close_quietly(conn)
            conn = None
            self._on_ws_down()
            if self._clock.monotonic() - up_at >= STABLE_CONNECTION_S:
                attempt = 0
            delay = backoff_delay(attempt, self._rng)
            attempt += 1
            await self._clock.sleep(delay)

    async def _pump_watched(self, conn: WsConnection) -> None:
        """Receive until the connection ends, or until it is silent for ``ws_idle_timeout``.

        A half-open socket (controller reboot, Wi-Fi drop) never delivers a
        FIN/RST to a client that never sends, so without this the client would
        stay CONNECTED on a dead socket forever.  The pump runs as its own task;
        :meth:`_on_ws_idle` cancels it when the deadline passes with no frame.
        """
        idle = self._options.ws_idle_timeout
        if idle is None:
            await self._pump(conn)
            return
        pump = self._tasks.spawn(self._pump(conn), "ws-pump")
        if pump is None:  # pragma: no cover - the client is closing
            return
        self._ws_pump = pump
        self._ws_last_frame = self._clock.monotonic()
        self._ws_idle.arm_in(idle)
        try:
            await asyncio.wait((pump,))
        finally:
            self._ws_pump = None
            self._ws_last_frame = None
            self._ws_idle.cancel()
            if not pump.done():
                pump.cancel()
                await asyncio.wait((pump,))

    def _on_ws_idle(self) -> None:
        idle = self._options.ws_idle_timeout
        pump, last = self._ws_pump, self._ws_last_frame
        if idle is None or pump is None or last is None or pump.done():
            return
        remaining = last + idle - self._clock.monotonic()
        if remaining > 0:  # a frame arrived meanwhile: wait for the new deadline
            self._ws_idle.arm_in(remaining)
            return
        self._ws_idle_timeouts += 1
        _LOGGER.warning("no WebSocket frame for %g s; reconnecting the WebSocket", idle)
        pump.cancel()

    async def _pump(self, conn: WsConnection) -> None:
        while True:
            try:
                frame = await conn.receive()
            except Exception as err:
                _LOGGER.debug("WebSocket receive failed: %s", type(err).__name__)
                return
            if frame is None:
                return
            self._frames_received += 1
            self._ws_last_frame = self._clock.monotonic()
            if not self._processor.enqueue(self._clock.utcnow(), self._clock.monotonic(), frame):
                _LOGGER.warning(
                    "more than %d frames buffered during a sync; reconnecting the WebSocket",
                    self._options.max_buffered_frames,
                )
                self._processor.clear()
                return

    def _on_ws_live(self, *, reconnected: bool) -> None:
        self._ws_live = True
        self._fallback_failed = False
        self._fallback.cancel()
        self._stores.set_live_since(self._clock.utcnow())
        self._update_connection_state()
        self._refresh()
        if reconnected:
            self._request_sync(UpdateReason.RESYNC)

    def _on_ws_down(self) -> None:
        self._ws_live = False
        self._stores.set_live_since(None)
        self._fallback.arm_in(self._options.rest_fallback_after)
        self._update_connection_state()
        self._refresh()

    # -- polls -------------------------------------------------------------------

    def _apply_alive(self, body: object, *, trigger: bool = True) -> None:
        self._stores.set_alive(body)
        version = body.get("version") if isinstance(body, dict) else None
        if not isinstance(version, str):
            return
        if trigger and self._last_version is not None and version != self._last_version:
            _LOGGER.info("controller web version changed; resyncing")
            self._request_sync(UpdateReason.RESYNC)
        self._last_version = version

    async def _alive_loop(self) -> None:
        opts = self._options
        while True:
            await self._clock.sleep(opts.alive_interval)
            try:
                body = await self._transport.get_alive(timeout=opts.alive_timeout)
            except ForbiddenRequestError:
                raise
            except RehomError as err:
                self._alive_streak.failure(err)
                self._alive_failures += 1
                self._alive_failures_total += 1
                self._update_connection_state()
                continue
            self._alive_streak.success()
            self._alive_failures = 0
            self._apply_alive(body)
            self._update_connection_state()

    async def _config_loop(self) -> None:
        opts = self._options
        while True:
            await self._clock.sleep(opts.config_interval)
            try:
                raw = await self._transport.get_config(timeout=opts.request_timeout)
                received_at = self._clock.utcnow()
                config = parse_config(raw)
            except ForbiddenRequestError:
                raise
            except RehomError as err:
                self._config_streak.failure(err)
                continue
            self._config_streak.success()
            await async_load_zone(config)  # before the check: no await until replaced
            if self._sync_running:
                continue  # the running sync brings a fresh config itself
            changes = self._stores.replace_config(config)
            self._device_clock = self._next_device_clock(config, received_at)
            self._safe_publish(UpdateReason.CONFIG, changes)

    # -- history -----------------------------------------------------------------

    async def get_history(
        self,
        series: HistorySeries | str,
        unit: str = "",
        *,
        start: datetime,
        end: datetime,
    ) -> tuple[HistorySample, ...]:
        """Fetch ``/api/history/`` for ``[start, end]`` (aware; at most 7 days).

        The window is converted to device-local time and fetched in chunks of at
        most 24 h, ``history_chunk_spacing`` seconds apart.
        """
        if self._closed or self._device_clock is None:
            raise RehomNotReadyError("get_history() needs a connected, open client")
        key = HistorySeries(series)
        for name, value in (("start", start), ("end", end)):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ValueError(f"{name} must be an aware datetime")
            if value.utcoffset() is None:
                raise ValueError(f"{name} must be an aware datetime")
        if end <= start:
            raise ValueError("end must be after start")
        if end - start > MAX_HISTORY_SPAN:
            raise ValueError("the history window must be at most 7 days")
        if key.per_zone:
            if not isinstance(unit, str) or _UNIT_RE.fullmatch(unit) is None:
                raise ValueError(f"{key.value} needs a zone id like '001'")
        elif unit != "":
            raise ValueError(f"{key.value} takes no unit")
        device_clock = self._device_clock
        local_start = device_clock.from_utc(start).replace(microsecond=0)
        local_end = device_clock.from_utc(end)
        if local_end.microsecond:
            local_end = local_end.replace(microsecond=0) + _ONE_SECOND
        local_end = max(local_end, local_start + _ONE_SECOND)
        samples: dict[tuple[str, object], HistorySample] = {}
        chunk_start = local_start
        while chunk_start < local_end:
            chunk_end = min(chunk_start + HISTORY_CHUNK, local_end)
            if chunk_start != local_start:  # chunks are spaced from the previous one's end
                await self._clock.sleep(self._options.history_chunk_spacing)
            if self._closed:
                raise RehomNotReadyError("the client was closed")
            payload = await self._transport.get_history(
                key.value,
                unit,
                chunk_start.strftime(HISTORY_TIME_FORMAT),
                chunk_end.strftime(HISTORY_TIME_FORMAT),
                timeout=self._options.request_timeout,
            )
            if not isinstance(payload, list):
                raise RehomResponseError(None, "GET /api/history/: unexpected payload")
            for item in payload:
                sample = _history_sample(item, device_clock)
                if sample is None:
                    continue
                ident: tuple[str, object] = (
                    ("rowid", sample.rowid)
                    if sample.rowid is not None
                    else ("tempo", sample.local_time)
                )
                samples.setdefault(ident, sample)
            chunk_start = chunk_end
        return tuple(sorted(samples.values(), key=lambda s: (s.time, s.rowid or 0)))

    # -- diagnostics -------------------------------------------------------------

    def record_values(self) -> dict[str, str]:
        """Every interface and override record as ``{path: raw value}``.

        Raw values include secrets: never log or persist the result unredacted.
        """
        out = {row.path: row.value for row in self._stores.interface_rows()}
        out.update({row.path: row.value for row in self._stores.override_rows()})
        return out

    # -- writes ----------------------------------------------------------------

    async def set_house_preset(self, preset: MasterPreset) -> bool:
        """House AUTO or MANUAL at ECONOMY / PRE_COMFORT / COMFORT (see :mod:`aiorehom.writes`).

        Returns ``False`` when nothing needed sending (already in that state).
        """
        return await self._execute(lambda st: plan_house_preset(st, preset))

    async def set_comfort_temperature(self, temperature: float) -> bool:
        """Set the comfort temperature (house must be MANUAL/COMFORT)."""
        return await self._execute(lambda st: plan_comfort_temperature(st, temperature))

    async def set_predictive(self, enabled: bool) -> bool:
        """Switch the predictive algorithm (house must be in AUTO)."""
        return await self._execute(lambda st: plan_predictive(st, enabled))

    async def set_zone_offset(self, zone_id: str, offset: int) -> bool:
        """Set a zone's whole-degree offset (-3..+3)."""
        return await self._execute(lambda st: plan_zone_offset(st, zone_id, offset))

    async def set_zone_mode(self, zone_id: str, setp: ZoneSetp) -> bool:
        """Put a zone on its schedule (``ZoneSetp.UNSET``) or at a manual level."""
        return await self._execute(lambda st: plan_zone_mode(st, zone_id, setp))

    async def set_temporary_comfort(self, zone_id: str, minutes: int) -> bool:
        """Force COMFORT on a zone for ``minutes`` from now (or extend an active one)."""
        return await self._execute(
            lambda st: plan_temporary_comfort(st, zone_id, minutes, self._clock.utcnow())
        )

    async def set_vmc_fan(self, vmc_id: str, value: int) -> bool:
        """Set a VMC's fan speed (discrete :class:`FanSpeed` or a continuous value)."""
        return await self._execute(lambda st: plan_vmc_fan(st, vmc_id, value))

    async def set_vmc_mode(self, vmc_id: str, mode: VmcMode) -> bool:
        """Set a VMC's operating mode."""
        return await self._execute(lambda st: plan_vmc_mode(st, vmc_id, mode))

    def _override_row(self, path: str) -> RowView | None:
        _gruppo, unita, subuni, key = path.split(".", 3)
        for row in self._stores.override_rows():
            if (row.unita, row.subuni, row.key) == (unita, subuni, key):
                return row
        return None

    def _current_value(self, path: str) -> str | None:
        gruppo, unita, subuni, key = path.split(".", 3)
        if gruppo == OVERRIDE_GRUPPO:
            row = self._override_row(path)
            return None if row is None else row.value
        return self._stores.value(gruppo, unita, subuni, key)

    def _window_matches(self, path: str, window: tuple[str, str]) -> bool:
        row = self._override_row(path)
        if row is None:
            return False
        start, end = window
        return _same_device_time(row.impostazione, start) and _same_device_time(row.scadenza, end)

    def _confirmed(self, plan: WritePlan) -> bool:
        """The stores report every expected value and override window of ``plan``."""
        return all(
            values_equal(self._current_value(path), value) for path, value in plan.expect.items()
        ) and all(self._window_matches(path, window) for path, window in plan.expect_window.items())

    async def _execute(self, planner: Callable[[RehomState], WritePlan]) -> bool:
        """Plan against the latest state, send once, and wait for the controller to report it.

        Writes run one at a time in call order.  Inside the lock the open
        notify batch is published first: the controller echoes a write before
        its POST returns, so the previous write's echo may still sit in the
        batch window, and the plan must see it.  The batch is published again
        before returning ``True``, so ``state`` shows a confirmed write as soon
        as the call returns.  Returns ``False`` if the controller already
        reports the requested values (nothing sent), ``True`` once a sent write
        is confirmed.  Nothing is ever retried.

        Raises:
            RehomWriteRefusedError: refused before sending (read-only client,
                controller unavailable, or a planner guard).
            RehomNotReadyError: no complete sync yet, or the client is closed;
                nothing was sent.
            RehomWriteNotConfirmedError: the POST was accepted (2xx) but the
                write was not confirmed, including when the confirming resync
                failed or the client was closed meanwhile (the cause is chained).
            RehomError: raised by the POST itself (connection, timeout, HTTP
                status).  A POST that timed out, or was cancelled or closed while
                in flight, may or may not have landed.
        """
        if self._allow_writes is not True:
            raise RehomWriteRefusedError("writes_disabled", "this client was created read-only")
        async with self._write_lock:
            self._batcher.flush_now()
            state = self._state
            if self._closed or not self._synced or state is None:
                raise RehomNotReadyError("no complete sync yet, or the client is closed")
            if not self.available:
                raise RehomWriteRefusedError("unavailable", "the controller is not reachable")
            plan = planner(state)
            if self._confirmed(plan):
                return False
            # A POST already inside post_bulk_update when close() runs is stopped
            # only by an owned transport's own closed check; otherwise it may land.
            writer: WriteTransport = self._transport  # type: ignore[assignment]
            await writer.post_bulk_update(plan.path, list(plan.records))
            _LOGGER.debug("write sent: %s", plan.description)
            await self._confirm_sent(plan)
            self._batcher.flush_now()
            return True

    async def _confirm_sent(self, plan: WritePlan) -> None:
        """Wait for a sent write to be reported, else take one snapshot to decide.

        Every failure here is :class:`RehomWriteNotConfirmedError`: the POST was
        already accepted, so the caller must not read it as "nothing happened".
        """
        try:
            if await self._wait_confirmed(plan):
                return
            if self._closed:
                raise RehomNotReadyError("the client was closed")
            # No matching echo in time: one fresh snapshot decides.
            await self._resync_within(_write_resync_limit(self._options))
        except Exception as err:
            raise RehomWriteNotConfirmedError(
                f"{plan.description}: sent, but the confirmation failed ({type(err).__name__})"
            ) from err
        if not self._confirmed(plan):
            raise RehomWriteNotConfirmedError(
                f"{plan.description}: the controller did not report the new value"
            )

    async def _wait_confirmed(self, plan: WritePlan) -> bool:
        """Poll the stores until ``plan`` is confirmed; ``False`` on timeout or close()."""
        opts = self._options
        deadline = self._clock.monotonic() + opts.write_confirm_timeout
        while True:
            if self._confirmed(plan):
                return True
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0 or self._closed:
                return False
            await self._clock.sleep(min(opts.write_poll_interval, remaining))

    async def _resync_within(self, limit: float) -> None:
        """:meth:`resync`, or :class:`RehomTimeoutError` after ``limit`` seconds (clock time).

        The bound matters if the sync loop has stopped: a bare ``resync()`` would
        then wait, holding the write lock, until :meth:`close`.
        """
        resync = asyncio.ensure_future(self.resync())
        timer = asyncio.ensure_future(self._clock.sleep(limit))
        try:
            await asyncio.wait((resync, timer), return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            finished = resync.done()
            if not finished:
                resync.cancel()
        if not finished:
            raise RehomTimeoutError(f"resync did not complete within {limit:g} s")
        resync.result()

    def dump(self) -> dict[str, Any]:
        """JSON-ready copy of the stores (secrets redacted; personal data NOT masked).

        Everything is copied (nested ``/config/`` and ``/alive/`` values too), so
        the caller may edit the result, for example to mask it, in place.
        """
        stores = self._stores
        forecast = stores.forecast
        return {
            "interface": stores.dump_records(),
            "overrides": stores.dump_records(overrides=True),
            "plant_conf": dict(stores.plant_conf),
            "config": copy.deepcopy(dict(stores.config)),
            "alive": copy.deepcopy(dict(stores.alive)),
            "forecast": None
            if forecast is None
            else {
                "received_at": _iso(forecast.received_at),
                "items": {str(epoch): raw for epoch, raw in sorted(forecast.items.items())},
            },
            "stats": asdict(self.stats),
            "connection_state": self._conn_state.value,
            "synced_at": _iso(stores.synced_at) if stores.has_snapshot else None,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _same_device_time(stored: str | None, expected: str) -> bool:
    """Two device-local override timestamps name the same instant (``" "`` or ``"T"``)."""
    if stored == expected:
        return True
    parsed = parse_device_timestamp(stored)
    return parsed is not None and parsed == parse_device_timestamp(expected)


def _write_resync_limit(opts: ClientOptions) -> float:
    """How long a write's confirming resync may take before it is given up.

    The resync waits out ``min_resync_interval`` and possibly a sync already
    running, then fetches a snapshot: ``/interface/`` plus three other GETs,
    each paced by ``request_min_interval``.
    """
    fetch = opts.interface_timeout + 3 * opts.request_timeout + 4 * opts.request_min_interval
    return opts.min_resync_interval + 2 * fetch


def _settle(waiters: list[asyncio.Future[None]], err: BaseException | None) -> None:
    """Resolve every pending ``resync()`` waiter (``err`` ``None`` = success)."""
    for waiter in waiters:
        if waiter.done():
            continue
        if err is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(err)


async def _close_quietly(conn: WsConnection) -> None:
    try:
        await conn.close()
    except Exception as err:
        _LOGGER.debug("closing the WebSocket failed: %s", type(err).__name__)


def _history_sample(item: object, device_clock: DeviceClock) -> HistorySample | None:
    """One history row, or ``None`` when it is malformed."""
    if not isinstance(item, dict):
        return None
    tempo = item.get("Tempo")
    if not isinstance(tempo, str) or _TEMPO_RE.fullmatch(tempo.strip()) is None:
        return None
    try:
        local = datetime.fromisoformat(tempo.strip())
    except ValueError:
        return None
    raw_value = item.get("Valore")
    value: float | None
    if isinstance(raw_value, bool):
        value = None
    elif isinstance(raw_value, (int, float)):
        value = float(raw_value)
    elif isinstance(raw_value, str):
        value = parse_float(raw_value)
    else:
        value = None
    if value is None or not math.isfinite(value):
        return None
    rowid = item.get("rowid")
    if rowid is not None and (isinstance(rowid, bool) or not isinstance(rowid, int)):
        return None
    try:
        when = device_clock.to_utc(local)
    except (ValueError, OverflowError):
        return None
    return HistorySample(time=when, local_time=local, value=value, rowid=rowid)
