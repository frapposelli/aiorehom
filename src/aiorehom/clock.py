"""Time sources: the injectable :class:`Clock` and the controller's :class:`DeviceClock`.

Every timer of the library goes through a :class:`Clock` (``monotonic``,
``utcnow``, ``sleep``).  :class:`SystemClock` is the real one and the only
place in the library that reads the system clocks or calls ``asyncio.sleep``.
:class:`VirtualClock` is deterministic: virtual time moves only when a test
(or ``rehom-probe replay``) calls :meth:`VirtualClock.advance` /
:meth:`VirtualClock.advance_to`, and sleepers wake in deadline order (FIFO on
ties).

:class:`DeviceClock` converts between true UTC and the controller's naive local
wall time (``/api/config/`` ``TIMEZONE`` + ``LOCAL_TIME``), including the
controller's clock skew.  Loading a time zone the first time reads the tz
database (blocking file I/O), so async callers load ``TIMEZONE`` in a worker
thread first with :func:`async_load_zone`; :class:`DeviceClock` then finds it
in this module's cache.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import re
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any, Final, Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "SKEW_DEADBAND",
    "Clock",
    "DeviceClock",
    "SystemClock",
    "VirtualClock",
    "async_load_zone",
    "measured_skew",
]

#: A skew smaller than this is treated as zero (``LOCAL_TIME`` has 1-s resolution).
SKEW_DEADBAND: Final = timedelta(seconds=2)
#: Fixed-offset fallback zones are rounded to this granularity.
_OFFSET_GRANULARITY: Final = timedelta(minutes=15)
#: ``asyncio.sleep(0)`` rounds per :meth:`VirtualClock.settle`.
_SETTLE_ROUNDS: Final = 64
_LOCAL_TIME_RE: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_ONE_US: Final = timedelta(microseconds=1)
#: Most time zones kept by :func:`_zone` (a controller has one; the bound only
#: stops a misbehaving one from growing the cache without limit).
_ZONE_CACHE_SIZE: Final = 16
#: Zone name -> loaded zone (``None``: not a valid zone), oldest first.
_zone_cache: dict[str, ZoneInfo | None] = {}
_zone_cache_lock = threading.Lock()


class Clock(Protocol):
    """Time source used by every timer of the library."""

    def monotonic(self) -> float:
        """Seconds on a monotonic scale (only differences are meaningful)."""
        ...

    def utcnow(self) -> datetime:
        """Current time, aware UTC."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep ``seconds`` on this clock's scale (``<= 0`` yields once)."""
        ...


class SystemClock:
    """The real clock: ``time.monotonic``, ``datetime.now(UTC)`` and ``asyncio.sleep``."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    """Deterministic clock for tests and replays.

    Time is kept in integer microseconds, so ``utcnow()`` is exact for any
    instant reached with :meth:`advance_to`.  ``sleep(s <= 0)`` is one
    ``asyncio.sleep(0)``; any other sleep waits until the virtual time reaches
    its deadline.
    """

    def __init__(self, start: datetime, *, monotonic_start: float = 10_000.0) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("VirtualClock start must be an aware datetime")
        self._start = start.astimezone(UTC)
        self._mono0 = float(monotonic_start)
        self._now_us = 0
        self._heap: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def monotonic(self) -> float:
        return self._mono0 + self._now_us / 1_000_000

    def utcnow(self) -> datetime:
        return self._start + timedelta(microseconds=self._now_us)

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self._now_us + max(1, round(seconds * 1_000_000)), next(self._seq), future)
        heapq.heappush(self._heap, entry)
        try:
            await future
        except asyncio.CancelledError:
            self._discard(entry)
            raise

    def _discard(self, entry: tuple[int, int, asyncio.Future[None]]) -> None:
        try:
            self._heap.remove(entry)
        except ValueError:
            return
        heapq.heapify(self._heap)

    async def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` (``>= 0``), waking every sleeper due."""
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        await self._advance_us(self._now_us + round(seconds * 1_000_000))

    async def advance_to(self, when: datetime) -> None:
        """Advance virtual time to ``when`` (aware), waking every sleeper due, in order."""
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("advance_to() needs an aware datetime")
        target = (when.astimezone(UTC) - self._start) // _ONE_US
        if target < self._now_us:
            raise ValueError("cannot advance a clock backwards")
        await self._advance_us(target)

    async def _advance_us(self, target: int) -> None:
        # Everything already runnable runs at the current instant before time moves.
        await self.settle()
        while self._heap and self._heap[0][0] <= target:
            deadline, _seq, future = heapq.heappop(self._heap)
            self._now_us = max(self._now_us, deadline)
            if not future.done():
                future.set_result(None)
            await self.settle()
        self._now_us = max(self._now_us, target)
        await self.settle()

    async def settle(self) -> None:
        """Let every ready task run (``64 x asyncio.sleep(0)``) without moving time."""
        for _ in range(_SETTLE_ROUNDS):
            await asyncio.sleep(0)

    @property
    def pending_sleepers(self) -> int:
        """Sleepers still waiting for their deadline."""
        return sum(1 for _deadline, _seq, future in self._heap if not future.done())

    @property
    def next_deadline(self) -> datetime | None:
        """Aware UTC deadline of the earliest pending sleeper, or ``None``."""
        pending = [deadline for deadline, _seq, future in self._heap if not future.done()]
        if not pending:
            return None
        return self._start + timedelta(microseconds=min(pending))


def _parse_local_time(raw: object) -> datetime | None:
    """``LOCAL_TIME`` as a datetime (naive, or aware if it carries an offset), else ``None``."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if _LOCAL_TIME_RE.fullmatch(text) is None:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _zone_key(name: object) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def _load_zone(key: str) -> ZoneInfo | None:
    """Load (blocking on a first load: it reads the tz database) and cache ``key``."""
    try:
        zone: ZoneInfo | None = ZoneInfo(key)
    except (ValueError, OSError, KeyError):  # ZoneInfoNotFoundError is a KeyError
        zone = None
    with _zone_cache_lock:
        _zone_cache.pop(key, None)
        while len(_zone_cache) >= _ZONE_CACHE_SIZE:
            _zone_cache.pop(next(iter(_zone_cache)))
        _zone_cache[key] = zone
    return zone


def _zone(name: object) -> ZoneInfo | None:
    """The zone called ``name`` from the cache, else loaded here (blocking)."""
    key = _zone_key(name)
    if key is None:
        return None
    with _zone_cache_lock:
        if key in _zone_cache:
            return _zone_cache[key]
    return _load_zone(key)


async def async_load_zone(config: Mapping[str, Any]) -> None:
    """Load ``/api/config/`` ``TIMEZONE`` in a worker thread unless it is cached.

    Afterwards :meth:`DeviceClock.from_config` and :func:`measured_skew` find
    the zone in the cache and do no file I/O on the event loop.  Never raises
    for a bad name (it is cached as "no zone").
    """
    key = _zone_key(_config_get(config, "TIMEZONE"))
    if key is None:
        return
    with _zone_cache_lock:
        if key in _zone_cache:
            return
    await asyncio.get_running_loop().run_in_executor(None, _load_zone, key)


def _as_utc(value: object) -> datetime | None:
    """``value`` as aware UTC (a naive datetime is taken as UTC), else ``None``."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _config_get(config: object, key: str) -> object:
    return config.get(key) if isinstance(config, Mapping) else None


def _round_offset(offset: timedelta) -> timedelta:
    steps = round(offset / _OFFSET_GRANULARITY)
    return steps * _OFFSET_GRANULARITY


def _zone_skew(local: datetime, zone: ZoneInfo, received: datetime) -> timedelta:
    """``local`` (device wall time in ``zone``) minus ``received``, fold-aware.

    A wall time inside the repeated hour at the end of DST exists twice; one
    inside the skipped hour at its start does not exist.  Both readings
    (``fold=0`` and ``fold=1``) are tried and the one closer to ``received``
    wins, so an exact device clock measures ~0 in either occurrence of the
    repeated hour (``fold=0`` alone reads the second occurrence 1 h early).
    Outside those hours both readings are the same instant.
    """
    if local.tzinfo is not None:
        local = local.astimezone(zone).replace(tzinfo=None)
    readings = (local.replace(tzinfo=zone, fold=fold).astimezone(UTC) - received for fold in (0, 1))
    return min(readings, key=abs)


def measured_skew(config: Mapping[str, Any], received_at: datetime) -> timedelta | None:
    """Raw measured skew (``LOCAL_TIME`` minus ``received_at``), before the dead band.

    ``None`` unless ``TIMEZONE`` is a valid zone and ``LOCAL_TIME`` parses (the
    only case where :meth:`DeviceClock.from_config` measures a skew).  The value
    carries the 0..1 s truncation of ``LOCAL_TIME`` plus the response latency;
    :class:`~aiorehom.client.RehomClient` compares successive raw values to
    decide whether the skew really moved (hysteresis) and exposes
    :meth:`DeviceClock.from_config`'s dead-banded value.
    """
    received = _as_utc(received_at)
    if received is None:
        return None
    local = _parse_local_time(_config_get(config, "LOCAL_TIME"))
    zone = _zone(_config_get(config, "TIMEZONE"))
    if zone is None or local is None:
        return None
    try:
        return _zone_skew(local, zone, received)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True, slots=True)
class DeviceClock:
    """Controller wall clock: time zone plus skew (device wall clock minus true time)."""

    tz: tzinfo
    tz_name: str | None
    skew: timedelta = timedelta(0)

    @classmethod
    def from_config(cls, config: Mapping[str, Any], received_at: datetime) -> DeviceClock:
        """Derive the device clock from ``/api/config/``; never raises, never ``None``.

        * ``TIMEZONE`` valid: that zone; skew = :func:`measured_skew` (``LOCAL_TIME``
          localised in the zone, the ``fold`` closer to ``received_at``, minus
          ``received_at``), zeroed when smaller than :data:`SKEW_DEADBAND`.
        * otherwise, ``LOCAL_TIME`` usable: a fixed offset ``LOCAL_TIME -
          received_at`` rounded to 15 min (``tz_name=None``, skew 0).
        * otherwise UTC.
        """
        received = _as_utc(received_at)
        if received is None:
            return cls.utc()
        local = _parse_local_time(_config_get(config, "LOCAL_TIME"))
        zone = _zone(_config_get(config, "TIMEZONE"))
        if zone is not None:
            skew = measured_skew(config, received) or timedelta(0)
            if abs(skew) < SKEW_DEADBAND:
                skew = timedelta(0)
            return cls(tz=zone, tz_name=zone.key, skew=skew)
        if local is not None:
            try:
                if local.tzinfo is not None:
                    offset = local.utcoffset()
                    if offset is None:  # pragma: no cover - fromisoformat never does this
                        return cls.utc()
                else:
                    offset = local.replace(tzinfo=UTC) - received
                rounded = _round_offset(offset)
                return cls(tz=timezone(rounded), tz_name=None)
            except (ValueError, OverflowError):
                return cls.utc()
        return cls.utc()

    @classmethod
    def utc(cls) -> DeviceClock:
        """A device clock on UTC with no skew (fallback)."""
        return cls(tz=UTC, tz_name=None)

    def local_now(self, now: datetime) -> datetime:
        """Naive device wall time at the true instant ``now`` (aware)."""
        return self.from_utc(now)

    def to_utc(self, local: datetime) -> datetime:
        """Naive device wall time (fold 0) -> aware true UTC."""
        if local.tzinfo is not None:
            raise ValueError("to_utc() expects a naive device-local datetime")
        return local.replace(tzinfo=self.tz, fold=0).astimezone(UTC) - self.skew

    def from_utc(self, when: datetime) -> datetime:
        """Aware instant -> naive device wall time (skew included)."""
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("from_utc() expects an aware datetime")
        return (when.astimezone(UTC) + self.skew).astimezone(self.tz).replace(tzinfo=None)
