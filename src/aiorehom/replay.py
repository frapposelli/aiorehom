"""Offline replay of a capture through the real :class:`~aiorehom.client.RehomClient`.

A capture directory (``interface.json``, ``overrides.json``, ``plant_conf.json``,
``config.json``, ``ws.jsonl`` and optionally ``alive.json``) becomes a
:class:`ReplayDevice`: a fake controller whose REST state at any instant is
the snapshot plus every WebSocket frame up to that instant.  The client talks
to it through a :class:`ReplayTransport` (``ReadTransport``) and
:class:`ReplayConnection` (``WsConnection``), and a :class:`ReplayDriver`
feeds the frames at their recorded times on a :class:`~aiorehom.clock.VirtualClock`
(deterministic, seconds for a 30-minute capture) or a :class:`ScaledClock`
(real time, sped up).

Nothing here opens a socket.  Every file is re-redacted on load.  The model
helpers (:func:`flatten_state`, :func:`mask_flat`, :func:`diff_flat`) reduce
state updates to printable model-level changes for ``rehom-probe replay``.
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import time
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum, StrEnum
from pathlib import Path
from typing import Any, Final, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

from ._files import read_json, read_jsonl
from .clock import VirtualClock
from .enums import Level
from .exceptions import RehomConnectionError
from .models import RehomState, StateUpdate
from .redact import is_redacted, redact_capture_file
from .store import OVERRIDE_GRUPPO, RecordKey, RecordStore, display_path, record_key, split_path
from .sync import WsConnection, WsConnector

__all__ = [
    "ABSENT",
    "DEFAULT_LATENCY",
    "REQUIRED_FILES",
    "ReplayConnection",
    "ReplayData",
    "ReplayDevice",
    "ReplayDriver",
    "ReplayError",
    "ReplayTransport",
    "ScaledClock",
    "diff_flat",
    "flatten_state",
    "format_value",
    "mask_flat",
    "masked_records",
    "update_alarm_ids",
]

#: Virtual latency of each ``ReplayTransport`` method (seconds).
DEFAULT_LATENCY: Final[Mapping[str, float]] = {
    "get_alive": 0.1,
    "get_interface": 2.4,
    "get_overrides": 0.2,
    "get_plant_conf": 0.2,
    "get_config": 0.2,
    "get_history": 0.2,
    "login": 0.2,
}
REQUIRED_FILES: Final = (
    "interface.json",
    "overrides.json",
    "plant_conf.json",
    "config.json",
    "ws.jsonl",
)
_DEFAULT_ALIVE: Final = {"version": "unknown"}
_METEO_DATA: Final = "METEO_DATA"
_LOCAL_TIME_FORMAT: Final = "%Y-%m-%dT%H:%M:%S"
_MAX_SPEED: Final = 1000.0


class ReplayError(ValueError):
    """The capture directory is missing a file or holds an invalid one."""


# ---------------------------------------------------------------------------
# Clocks
# ---------------------------------------------------------------------------


class ScaledClock:
    """Real-time clock sped up ``speed`` times: virtual time = start + real elapsed x speed."""

    def __init__(self, start: datetime, speed: float, *, monotonic_start: float = 10_000.0) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("ScaledClock start must be an aware datetime")
        if not 0 < speed <= _MAX_SPEED:
            raise ValueError(f"speed must be > 0 and <= {_MAX_SPEED:g}")
        self._start = start.astimezone(UTC)
        self._speed = float(speed)
        self._mono0 = float(monotonic_start)
        self._real0 = time.monotonic()

    @property
    def speed(self) -> float:
        return self._speed

    def _elapsed(self) -> float:
        return (time.monotonic() - self._real0) * self._speed

    def monotonic(self) -> float:
        return self._mono0 + self._elapsed()

    def utcnow(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed())

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds / self._speed))


# ---------------------------------------------------------------------------
# The fake device
# ---------------------------------------------------------------------------


@dataclass
class ReplayData:
    """A mutable REST snapshot of the replayed controller (``patch`` edits it in place)."""

    interface: list[dict[str, Any]]
    overrides: list[dict[str, Any]]
    plant_conf: dict[str, Any]
    config: dict[str, Any]
    alive: dict[str, Any]


def _parse_time(raw: object, where: str) -> datetime:
    if not isinstance(raw, str):
        raise ReplayError(f"{where}: missing timestamp")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ReplayError(f"{where}: invalid timestamp") from None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_forecast(frame: Mapping[str, Any]) -> bool:
    if frame.get("gruppo") == _METEO_DATA:
        return True
    parts = split_path(frame.get("path"))
    return parts is not None and parts[0] == _METEO_DATA


def _frame_key(frame: Mapping[str, Any]) -> tuple[bool, RecordKey] | None:
    """``(is_override, key)`` of a ``termo`` frame, routed like ``RecordStore.apply_ws``."""
    parts = split_path(frame.get("path"))
    if parts is None:
        return None
    gruppo = frame.get("gruppo")
    is_override = gruppo == OVERRIDE_GRUPPO or (gruppo is None and parts[0] == OVERRIDE_GRUPPO)
    if is_override:
        return True, (OVERRIDE_GRUPPO, parts[1], parts[2], parts[3])
    return False, parts


def _ordered(store: RecordStore, order: Iterable[RecordKey]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in order:
        row = store.get(key)
        if row is not None:
            out.append(row)
    return out


class ReplayDevice:
    """A captured controller: REST snapshot plus timed WebSocket frames."""

    def __init__(
        self,
        data: ReplayData,
        frames: Sequence[tuple[datetime, Mapping[str, Any]]],
        *,
        start: datetime,
        end: datetime,
    ) -> None:
        if end < start:
            raise ReplayError("the capture ends before it starts")
        self._data = data
        self.frames: tuple[tuple[datetime, Mapping[str, Any]], ...] = tuple(frames)
        self._times = [when for when, _frame in self.frames]
        self.start = start
        self.end = end
        #: Method names called on any :class:`ReplayTransport` of this device.
        self.transport_calls: list[str] = []
        self._connection: ReplayConnection | None = None
        #: Connections opened by :meth:`ws_connector` connectors.
        self.connections = 0
        #: Frames delivered while no connection was open (lost, as on a real socket).
        self.frames_dropped = 0

    @property
    def data(self) -> ReplayData:
        """The snapshot (after ``patch``); do not modify it after construction."""
        return self._data

    @classmethod
    def from_dir(
        cls, path: Path, *, patch: Callable[[ReplayData], None] | None = None
    ) -> ReplayDevice:
        """Load and re-redact a capture directory; ``patch`` edits the snapshot in memory."""
        if not path.is_dir():
            raise ReplayError(f"not a directory: {path}")
        missing = [name for name in REQUIRED_FILES if not (path / name).is_file()]
        if missing:
            raise ReplayError(f"missing file(s) in {path}: {', '.join(missing)}")
        loaded: dict[str, Any] = {}
        names = [*REQUIRED_FILES, *(["alive.json"] if (path / "alive.json").is_file() else [])]
        for name in names:
            try:
                content = (
                    read_jsonl(path / name) if name.endswith(".jsonl") else read_json(path / name)
                )
            except (OSError, ValueError):
                raise ReplayError(f"{name}: unreadable or invalid JSON") from None
            loaded[name], _count = redact_capture_file(name, content)
        for name, kind in (
            ("interface.json", list),
            ("overrides.json", list),
            ("plant_conf.json", dict),
            ("config.json", dict),
            ("ws.jsonl", list),
            ("alive.json", dict),
        ):
            if name in loaded and not isinstance(loaded[name], kind):
                raise ReplayError(f"{name}: expected a JSON {kind.__name__}")
        data = ReplayData(
            interface=[row for row in loaded["interface.json"] if isinstance(row, dict)],
            overrides=[row for row in loaded["overrides.json"] if isinstance(row, dict)],
            plant_conf=loaded["plant_conf.json"],
            config=loaded["config.json"],
            alive=loaded.get("alive.json", dict(_DEFAULT_ALIVE)),
        )
        if patch is not None:
            patch(data)
        frames: list[tuple[datetime, Mapping[str, Any]]] = []
        connect_at: datetime | None = None
        last: datetime | None = None
        for index, line in enumerate(loaded["ws.jsonl"], start=1):
            if not isinstance(line, Mapping):
                raise ReplayError(f"ws.jsonl line {index}: not an object")
            when = _parse_time(line.get("t"), f"ws.jsonl line {index}")
            last = when if last is None else max(last, when)
            event = line.get("event")
            if "frame" in line:
                frames.append((when, line["frame"]))
            elif (
                connect_at is None
                and isinstance(event, Mapping)
                and event.get("event") == "connect"
            ):
                connect_at = when
        if last is None:
            raise ReplayError("ws.jsonl: empty")
        frames.sort(key=lambda item: item[0])  # stable: equal times keep file order
        if connect_at is None:
            if not frames:
                raise ReplayError("ws.jsonl: no connect event and no frame")
            connect_at = frames[0][0] - timedelta(seconds=1)
        return cls(data, frames, start=connect_at, end=last)

    # -- REST side ---------------------------------------------------------------

    def state_at(self, t: datetime) -> ReplayData:
        """The snapshot plus every frame at or before ``t`` (``store.RecordStore`` semantics).

        Removes are immediate on the device side.  ``METEO_DATA`` (a WS-only
        forecast feed) never enters the snapshot, and neither do commissioning
        (``RIS_*``) bus keys or domotica frames.
        """
        data = self._data
        interface = RecordStore(data.interface, role="interface")
        overrides = RecordStore(data.overrides, role="overrides")
        order_i: list[RecordKey] = list(dict.fromkeys(record_key(row) for row in data.interface))
        order_o: list[RecordKey] = list(dict.fromkeys(record_key(row) for row in data.overrides))
        known_i, known_o = set(order_i), set(order_o)
        plant_conf = copy.deepcopy(data.plant_conf)
        for _when, frame in self.frames[: bisect_right(self._times, t)]:
            if not isinstance(frame, Mapping) or "domotica" in frame:
                continue
            domain = frame.get("domain")
            if domain == "termo" and not _is_forecast(frame):
                routed = _frame_key(frame)
                if routed is None:
                    continue
                is_override, key = routed
                store, order, known = (
                    (overrides, order_o, known_o) if is_override else (interface, order_i, known_i)
                )
                store.apply_ws(frame)
                if key in store and key not in known:
                    known.add(key)
                    order.append(key)
            elif domain == "bus":
                self._apply_bus(plant_conf, frame)
        return ReplayData(
            interface=_ordered(interface, order_i),
            overrides=_ordered(overrides, order_o),
            plant_conf=plant_conf,
            config=copy.deepcopy(data.config),
            alive=copy.deepcopy(data.alive),
        )

    @staticmethod
    def _apply_bus(plant_conf: dict[str, Any], frame: Mapping[str, Any]) -> None:
        key = frame.get("key")
        if not isinstance(key, str) or not key or key.startswith("RIS_"):
            return
        if frame.get("type") == "update":
            value = frame.get("value")
            plant_conf[key] = "" if value is None else str(value)
        elif frame.get("type") == "remove":
            plant_conf.pop(key, None)

    def local_time(self, when: datetime) -> str:
        """``LOCAL_TIME`` the device reports at ``when``: naive, seconds, in its ``TIMEZONE``."""
        name = self._data.config.get("TIMEZONE")
        zone: tzinfo = UTC
        if isinstance(name, str) and name.strip():
            try:
                zone = ZoneInfo(name.strip())
            except (ValueError, OSError, KeyError):
                zone = UTC
        return when.astimezone(zone).strftime(_LOCAL_TIME_FORMAT)

    def transport(
        self, clock: Any, *, latency: Mapping[str, float] | None = None
    ) -> ReplayTransport:
        """A ``ReadTransport`` answering from :meth:`state_at` after a virtual latency."""
        merged = dict(DEFAULT_LATENCY)
        if latency is not None:
            merged.update(latency)
        return ReplayTransport(self, clock, merged)

    # -- WebSocket side -------------------------------------------------------------

    def ws_connector(self, clock: Any) -> WsConnector:
        """A connector whose connections are fed by :class:`ReplayDriver`."""

        async def connect() -> WsConnection:
            if clock.utcnow() > self.end:
                raise RehomConnectionError("replay: the capture has ended")
            if self._connection is not None:
                self._connection.end()
            connection = ReplayConnection()
            self._connection = connection
            self.connections += 1
            return connection

        return connect

    def deliver(self, frame: Mapping[str, Any]) -> bool:
        """Push ``frame`` into the open connection; ``False`` (dropped) if none is open."""
        connection = self._connection
        if connection is None or connection.closed:
            self.frames_dropped += 1
            return False
        connection.push(copy.deepcopy(frame))
        return True

    def disconnect(self) -> None:
        """End the open connection (its ``receive()`` returns ``None``)."""
        if self._connection is not None:
            self._connection.end()


class ReplayTransport:
    """``ReadTransport`` over a :class:`ReplayDevice` (GETs only; ``login`` is a no-op)."""

    def __init__(self, device: ReplayDevice, clock: Any, latency: Mapping[str, float]) -> None:
        self._device = device
        self._clock = clock
        self._latency = dict(latency)

    @property
    def has_token(self) -> bool:
        return False

    async def _call(self, name: str) -> datetime:
        self._device.transport_calls.append(name)
        started: datetime = self._clock.utcnow()
        await self._clock.sleep(self._latency.get(name, 0.0))
        return started

    async def login(self, username: str, password: str) -> None:
        await self._call("login")

    async def get_alive(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        await self._call("get_alive")
        return copy.deepcopy(self._device.data.alive)

    async def get_config(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        started = await self._call("get_config")
        config = self._device.state_at(started).config
        config["LOCAL_TIME"] = self._device.local_time(self._clock.utcnow())
        return config

    async def get_interface(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return self._device.state_at(await self._call("get_interface")).interface

    async def get_overrides(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return self._device.state_at(await self._call("get_overrides")).overrides

    async def get_plant_conf(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return self._device.state_at(await self._call("get_plant_conf")).plant_conf

    async def get_history(
        self,
        key: str,
        unita: str,
        gte: str,
        lte: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        await self._call("get_history")
        return []

    async def close(self) -> None:
        self._device.transport_calls.append("close")


class ReplayConnection:
    """A receive-only connection fed by the driver (no send method exists)."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def push(self, frame: Any) -> None:
        if not self._closed:
            self._queue.put_nowait(frame)

    def end(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    async def receive(self) -> Any | None:
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()

    async def close(self) -> None:
        self.end()


class ReplayDriver:
    """Moves the clock frame by frame and delivers each frame at its recorded time."""

    def __init__(self, device: ReplayDevice, clock: VirtualClock | ScaledClock) -> None:
        self._device = device
        self._clock = clock
        self._next = 0

    @property
    def delivered(self) -> int:
        """Frames handed to the device so far (delivered or dropped)."""
        return self._next

    async def _wait_until(self, when: datetime) -> None:
        clock = self._clock
        if isinstance(clock, VirtualClock):
            if when > clock.utcnow():
                await clock.advance_to(when)  # timers due by then fire first, in order
            else:
                await clock.settle()
            return
        await clock.sleep((when - clock.utcnow()).total_seconds())

    async def _settle(self) -> None:
        if isinstance(self._clock, VirtualClock):
            await self._clock.settle()
        else:
            await asyncio.sleep(0)

    async def run_until(self, when: datetime) -> None:
        """Deliver every frame recorded at or before ``when``, then advance to ``when``."""
        frames = self._device.frames
        while self._next < len(frames) and frames[self._next][0] <= when:
            at, frame = frames[self._next]
            await self._wait_until(at)
            self._device.deliver(frame)
            self._next += 1
            await self._settle()
        await self._wait_until(when)

    async def run_to_end(self) -> None:
        await self.run_until(self._device.end)


# ---------------------------------------------------------------------------
# Model-level changes
# ---------------------------------------------------------------------------


class _Absent:
    """Marks a path that does not exist on one side of a diff."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<absent>"


#: Value of a path missing on one side of :func:`diff_flat`.
ABSENT: Final = _Absent()


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.isoformat(timespec="milliseconds")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scalar(value: Any) -> Any:
    """A JSON-ready scalar for one model value."""
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Enum):  # IntEnum and anything else: by name
        return value.name
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tzinfo):
        return str(value)
    return str(value)


def _csv_item(value: Any, levels: bool) -> str:
    if value is None:
        return "?" if levels else ""
    if isinstance(value, Level):
        return str(int(value))
    return str(_scalar(value))


def _key(value: object) -> str:
    return value.name if isinstance(value, Enum) else str(value)


_KEYED_FIELDS: dict[type, frozenset[str]] = {}


def _keyed_fields(cls: type) -> frozenset[str]:
    """Fields of dataclass ``cls`` typed as a tuple of dataclasses (keyed, never a CSV)."""
    cached = _KEYED_FIELDS.get(cls)
    if cached is None:
        try:
            hints = get_type_hints(cls)
        except (NameError, TypeError):  # pragma: no cover - models always resolve
            hints = {}
        keyed: set[str] = set()
        for name, hint in hints.items():
            args = get_args(hint)
            if get_origin(hint) is tuple and args and is_dataclass(args[0]):
                keyed.add(name)
        cached = _KEYED_FIELDS[cls] = frozenset(keyed)
    return cached


def _flatten(value: Any, prefix: str, out: dict[str, Any], *, keyed: bool = False) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        tuple_fields = _keyed_fields(type(value))
        for item in fields(value):
            if not prefix and item.name == "built_at":
                continue
            name = f"{prefix}.{item.name}" if prefix else item.name
            _flatten(getattr(value, item.name), name, out, keyed=item.name in tuple_fields)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _flatten(item, f"{prefix}.{_key(key)}", out)
    elif isinstance(value, (tuple, list)):
        if keyed or (
            value and all(is_dataclass(item) and not isinstance(item, type) for item in value)
        ):
            for index, item in enumerate(value):
                ident = getattr(item, "id", index)
                _flatten(item, f"{prefix}[{ident}]", out)
        else:
            levels = any(isinstance(item, Level) for item in value)
            out[prefix] = ",".join(_csv_item(item, levels) for item in value)
    else:
        out[prefix] = _scalar(value)


def flatten_state(state: RehomState | None) -> dict[str, Any]:
    """``{path: scalar}`` of a state (``built_at`` excluded); ``{}`` for ``None``.

    Dataclasses flatten by field name and mappings by key (enum keys by
    ``.name``).  A tuple of dataclasses is keyed by their ``id`` field (else
    by position) in brackets; a tuple of scalars or ``Level``\\ s becomes one
    CSV string.  Datetimes are ISO UTC with ``Z``, a ``StrEnum`` is its
    value and an ``IntEnum`` its name.
    """
    out: dict[str, Any] = {}
    if state is not None:
        _flatten(state, "", out)
    return out


#: (glob, replacement, unless show_names)
_MASKS: Final = (
    ("*.name", "<name>", True),
    ("hub.mac", "<mac>", False),
    ("*.identity.serial", "<id>", False),
    ("*.identity.custom_id", "<id>", False),
)


def mask_flat(flat: Mapping[str, Any], *, show_names: bool = False) -> dict[str, Any]:
    """Copy of a flattened state that is safe to print.

    ``*.name`` becomes ``<name>`` unless ``show_names``; ``hub.mac`` is always
    ``<mac>``; unit serials and custom ids are always ``<id>``; any redaction
    marker is shown as ``<redacted>`` (presence only, never a length).
    """
    out: dict[str, Any] = {}
    for path, value in flat.items():
        masked = value
        for pattern, replacement, name_mask in _MASKS:
            if name_mask and show_names:
                continue
            if value is not None and fnmatch.fnmatchcase(path, pattern):
                masked = replacement
                break
        if is_redacted(masked):
            masked = "<redacted>"
        out[path] = masked
    return out


def diff_flat(
    old: Mapping[str, Any], new: Mapping[str, Any], *, ignore: Sequence[str] = ()
) -> list[tuple[str, Any, Any]]:
    """``[(path, old, new)]`` for every path whose value differs (:data:`ABSENT` if missing).

    Paths matching any ``ignore`` glob (``fnmatch``) are dropped.  Order: the
    paths of ``new`` in their order, then paths that exist only in ``old``.
    """
    out: list[tuple[str, Any, Any]] = []
    for path, value in new.items():
        before = old.get(path, ABSENT)
        if path not in old or before != value or type(before) is not type(value):
            out.append((path, before, value))
    out.extend((path, value, ABSENT) for path, value in old.items() if path not in new)
    if ignore:
        out = [
            change
            for change in out
            if not any(fnmatch.fnmatchcase(change[0], pattern) for pattern in ignore)
        ]
    return out


def format_value(value: Any) -> Any:
    """JSON-ready rendering of a flattened value (:data:`ABSENT` -> ``"<absent>"``)."""
    return "<absent>" if value is ABSENT else value


def masked_records(paths: Iterable[str]) -> list[str]:
    """Sorted record paths with ``UTEN`` user names shown as ``<user>``."""
    out: list[str] = []
    for path in paths:
        key = split_path(path)
        out.append(display_path(key) if key is not None else path)
    return sorted(out)


def update_alarm_ids(update: StateUpdate) -> tuple[set[str], set[str]]:
    """Alarm ids newly active and newly debounced in ``update`` (vs its previous state)."""
    previous = update.previous
    before = set() if previous is None else {alarm.id for alarm in previous.alarms}
    before_d = set() if previous is None else {alarm.id for alarm in previous.alarms_debounced}
    now = {alarm.id for alarm in update.state.alarms}
    now_d = {alarm.id for alarm in update.state.alarms_debounced}
    return now - before, now_d - before_d
