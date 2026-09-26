"""Clocks: VirtualClock determinism, SystemClock, DeviceClock (design 4.8)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from aiorehom import clock as clock_module
from aiorehom.clock import (
    SKEW_DEADBAND,
    DeviceClock,
    SystemClock,
    VirtualClock,
    async_load_zone,
    measured_skew,
)

T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
ROME = ZoneInfo("Europe/Rome")


# ---------------------------------------------------------------------------
# VirtualClock
# ---------------------------------------------------------------------------


async def test_virtual_clock_wakes_sleepers_in_deadline_order_fifo_on_ties() -> None:
    clock = VirtualClock(T0)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(name: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((name, clock.utcnow()))

    tasks = [
        asyncio.create_task(sleeper(name, seconds))
        for name, seconds in (("c", 3.0), ("a1", 1.0), ("b", 2.0), ("a2", 1.0))
    ]
    await clock.settle()
    assert clock.pending_sleepers == 4
    assert clock.next_deadline == T0 + timedelta(seconds=1)
    await clock.advance(2.5)
    assert woke == [
        ("a1", T0 + timedelta(seconds=1)),
        ("a2", T0 + timedelta(seconds=1)),
        ("b", T0 + timedelta(seconds=2)),
    ]
    assert clock.utcnow() == T0 + timedelta(seconds=2.5)
    await clock.advance_to(T0 + timedelta(seconds=10))
    assert woke[-1] == ("c", T0 + timedelta(seconds=3))
    assert clock.utcnow() == T0 + timedelta(seconds=10)
    assert clock.next_deadline is None
    await asyncio.gather(*tasks)


async def test_virtual_clock_time_is_exact_and_monotonic_follows() -> None:
    clock = VirtualClock(T0, monotonic_start=500.0)
    assert clock.monotonic() == 500.0
    target = T0 + timedelta(minutes=29, seconds=51, microseconds=362_000)
    await clock.advance_to(target)
    assert clock.utcnow() == target
    assert clock.monotonic() == pytest.approx(500.0 + 29 * 60 + 51.362)
    await clock.advance(0)
    assert clock.utcnow() == target


async def test_virtual_clock_cancellation_removes_the_sleeper() -> None:
    clock = VirtualClock(T0)
    task = asyncio.create_task(clock.sleep(5))
    other = asyncio.create_task(clock.sleep(7))
    await clock.settle()
    assert clock.pending_sleepers == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock.pending_sleepers == 1
    await clock.advance(10)
    await other
    assert clock.pending_sleepers == 0


async def test_virtual_clock_zero_sleep_and_errors() -> None:
    clock = VirtualClock(T0)
    await clock.sleep(0)
    await clock.sleep(-1)
    assert clock.pending_sleepers == 0
    await clock.advance(1)
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance_to(T0)
    with pytest.raises(ValueError, match="backwards"):
        await clock.advance(-0.1)
    with pytest.raises(ValueError, match="aware"):
        await clock.advance_to(datetime(2026, 9, 25, 11))
    with pytest.raises(ValueError, match="aware"):
        VirtualClock(datetime(2026, 9, 25))
    rome_start = VirtualClock(datetime(2026, 9, 25, 12, tzinfo=ROME))
    assert rome_start.utcnow() == T0
    assert rome_start.utcnow().tzinfo is UTC


async def test_virtual_clock_tiny_sleeps_still_need_time_to_pass() -> None:
    clock = VirtualClock(T0)
    task = asyncio.create_task(clock.sleep(1e-9))
    await clock.settle()
    assert not task.done()
    await clock.advance(0.000001)
    await task


async def test_system_clock() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    now = clock.utcnow()
    assert now.tzinfo is UTC
    await clock.sleep(0)
    await clock.sleep(-5)
    assert clock.monotonic() >= first


# ---------------------------------------------------------------------------
# DeviceClock
# ---------------------------------------------------------------------------


def _local(dt: datetime, *, sep: str = " ") -> str:
    return dt.astimezone(ROME).strftime(f"%Y-%m-%d{sep}%H:%M:%S")


def test_from_config_zone_with_skew_inside_deadband_is_zero() -> None:
    received = T0 + timedelta(microseconds=900_000)
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": _local(T0 + timedelta(seconds=1))}, received
    )
    assert clock.tz == ROME
    assert clock.tz_name == "Europe/Rome"
    assert clock.skew == timedelta(0)
    behind = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": _local(T0 - timedelta(seconds=1))}, T0
    )
    assert behind.skew == timedelta(0)
    assert timedelta(seconds=2) == SKEW_DEADBAND


@pytest.mark.parametrize("sep", [" ", "T"])
def test_from_config_zone_with_real_skew(sep: str) -> None:
    ahead = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": _local(T0 + timedelta(minutes=5), sep=sep)}, T0
    )
    assert ahead.skew == timedelta(seconds=300)
    assert ahead.local_now(T0) == datetime(2026, 9, 25, 12, 5)
    assert ahead.to_utc(datetime(2026, 9, 25, 12, 5)) == T0
    behind = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": _local(T0 - timedelta(seconds=2))}, T0
    )
    assert behind.skew == timedelta(seconds=-2)


@pytest.mark.parametrize(
    ("received", "local"),
    [
        (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), "2026-10-25 02:30:00"),  # first 02:30 (CEST)
        (datetime(2026, 10, 25, 1, 30, tzinfo=UTC), "2026-10-25 02:30:00"),  # second 02:30 (CET)
        (datetime(2026, 10, 25, 1, 59, 59, tzinfo=UTC), "2026-10-25 02:59:59"),
        (datetime(2026, 3, 29, 1, 0, tzinfo=UTC), "2026-03-29 03:00:00"),  # after the gap
    ],
)
def test_an_exact_device_clock_around_dst_changes_has_no_skew(
    received: datetime, local: str
) -> None:
    """Regression: fold=0 alone read the second 02:xx of the fall-back night 1 h early."""
    config = {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": local}
    assert measured_skew(config, received) == timedelta(0)
    clock = DeviceClock.from_config(config, received)
    assert clock.skew == timedelta(0)
    assert clock.from_utc(received) == datetime.fromisoformat(local)


def test_repeated_hour_local_now_after_the_fold() -> None:
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "2026-10-25 02:30:00"},
        datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
    )
    assert clock.local_now(datetime(2026, 10, 25, 2, 10, tzinfo=UTC)) == datetime(
        2026, 10, 25, 3, 10
    )
    assert clock.to_utc(datetime(2026, 10, 25, 3, 0)) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)


def test_measured_skew_is_raw_and_none_without_a_zone_or_a_local_time() -> None:
    received = T0 + timedelta(microseconds=900_000)
    config = {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": _local(T0 + timedelta(seconds=1))}
    assert measured_skew(config, received) == timedelta(microseconds=100_000)  # no dead band
    assert measured_skew({"TIMEZONE": "Europe/Rome"}, T0) is None
    assert measured_skew({"LOCAL_TIME": _local(T0)}, T0) is None
    assert measured_skew(config, "nope") is None  # type: ignore[arg-type]
    far = {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "0001-01-01 00:00:00"}
    assert measured_skew(far, T0) is None  # out of range: never raises


def test_from_config_local_time_with_explicit_offset() -> None:
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "2026-09-25T10:05:00+00:00"}, T0
    )
    assert clock.skew == timedelta(minutes=5)
    fixed = DeviceClock.from_config({"LOCAL_TIME": "2026-09-25T12:00:00+02:00"}, T0)
    assert fixed.tz == timezone(timedelta(hours=2))
    assert fixed.tz_name is None


def test_from_config_invalid_zone_falls_back_to_a_fixed_offset() -> None:
    received = T0 + timedelta(seconds=7)  # 7 s of latency/skew: rounded away
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Mars/Olympus_Mons", "LOCAL_TIME": "2026-09-25 12:00:03"}, received
    )
    assert clock.tz == timezone(timedelta(hours=2))
    assert clock.tz_name is None
    assert clock.skew == timedelta(0)
    assert clock.local_now(T0) == datetime(2026, 9, 25, 12, 0)
    india = DeviceClock.from_config({"TIMEZONE": "", "LOCAL_TIME": "2026-09-25 15:31:00"}, T0)
    assert india.tz == timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"TIMEZONE": "../etc/passwd"},
        {"TIMEZONE": 5, "LOCAL_TIME": 5},
        {"TIMEZONE": "Nope/Nope", "LOCAL_TIME": "yesterday"},
        {"LOCAL_TIME": "2026-09-25"},  # a date alone is not a time
        {"LOCAL_TIME": "2026-13-45 99:99:99"},
        {"LOCAL_TIME": "2026-09-28 10:00:00"},  # offset >= 24 h is impossible
    ],
)
def test_from_config_without_anything_usable_is_utc(config: dict[str, object]) -> None:
    assert DeviceClock.from_config(config, T0) == DeviceClock.utc()


def test_from_config_never_raises_on_odd_inputs() -> None:
    assert DeviceClock.from_config("nope", T0) == DeviceClock.utc()  # type: ignore[arg-type]
    assert DeviceClock.from_config({}, "nope") == DeviceClock.utc()  # type: ignore[arg-type]
    naive = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "2026-09-25 12:05:00"},
        datetime(2026, 9, 25, 10, 0),
    )
    assert naive.skew == timedelta(minutes=5)
    no_local = DeviceClock.from_config({"TIMEZONE": "Europe/Rome"}, T0)
    assert (no_local.tz_name, no_local.skew) == ("Europe/Rome", timedelta(0))


def test_round_trips_and_dst() -> None:
    clock = DeviceClock.from_config({"TIMEZONE": "Europe/Rome"}, T0)
    assert clock.local_now(T0) == datetime(2026, 9, 25, 12)
    assert clock.to_utc(clock.local_now(T0)) == T0
    assert clock.from_utc(T0) == clock.local_now(T0)
    # 2026-10-25 03:00 CEST -> 02:00 CET: 02:30 happens twice; fold=0 is the first
    assert clock.to_utc(datetime(2026, 10, 25, 2, 30)) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 12, tzinfo=UTC)
    assert clock.local_now(winter) == datetime(2026, 12, 1, 13)
    with pytest.raises(ValueError, match="naive"):
        clock.to_utc(T0)
    with pytest.raises(ValueError, match="aware"):
        clock.from_utc(datetime(2026, 9, 25))
    utc = DeviceClock.utc()
    assert utc.local_now(T0) == datetime(2026, 9, 25, 10)
    assert utc.tz_name is None


def test_device_clock_is_frozen_and_comparable() -> None:
    a = DeviceClock.from_config({"TIMEZONE": "Europe/Rome"}, T0)
    b = DeviceClock.from_config({"TIMEZONE": "Europe/Rome"}, T0 + timedelta(hours=1))
    assert a == b
    with pytest.raises(AttributeError):
        a.skew = timedelta(1)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Time-zone loading (no tz-database I/O on the event loop)
# ---------------------------------------------------------------------------


@pytest.fixture
def zone_loads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, threading.Thread]]:
    """An empty zone cache, and every ``ZoneInfo(key)`` call with its thread."""
    monkeypatch.setattr(clock_module, "_zone_cache", {})
    loads: list[tuple[str, threading.Thread]] = []
    real = clock_module.ZoneInfo

    def recording(key: str) -> ZoneInfo:
        loads.append((key, threading.current_thread()))
        return real(key)

    monkeypatch.setattr(clock_module, "ZoneInfo", recording)
    return loads


async def test_async_load_zone_reads_the_tz_database_off_the_loop(
    zone_loads: list[tuple[str, threading.Thread]],
) -> None:
    loop_thread = threading.current_thread()
    await async_load_zone({"TIMEZONE": " Europe/Rome ", "LOCAL_TIME": "2026-09-25 12:00:00"})
    assert [key for key, _thread in zone_loads] == ["Europe/Rome"]
    assert zone_loads[0][1] is not loop_thread
    # DeviceClock and measured_skew now find it in the cache: no further load
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "2026-09-25 12:00:00"}, T0
    )
    assert clock.tz_name == "Europe/Rome"
    await async_load_zone({"TIMEZONE": "Europe/Rome"})
    assert len(zone_loads) == 1
    # a bad name is loaded (off the loop) once and cached as "no zone"
    await async_load_zone({"TIMEZONE": "Mars/Olympus_Mons"})
    await async_load_zone({"TIMEZONE": "Mars/Olympus_Mons"})
    assert [key for key, _thread in zone_loads] == ["Europe/Rome", "Mars/Olympus_Mons"]
    assert zone_loads[1][1] is not loop_thread
    assert DeviceClock.from_config({"TIMEZONE": "Mars/Olympus_Mons"}, T0) == DeviceClock.utc()
    # nothing to load
    for config in ({}, {"TIMEZONE": ""}, {"TIMEZONE": 5}, "nope"):
        await async_load_zone(config)  # type: ignore[arg-type]
    assert len(zone_loads) == 2


def test_zone_cache_is_bounded(zone_loads: list[tuple[str, threading.Thread]]) -> None:
    names = [f"Etc/GMT+{hours}" for hours in range(1, 13)]
    names += [f"Etc/GMT-{hours}" for hours in range(1, 13)]
    for name in names:
        assert DeviceClock.from_config({"TIMEZONE": name}, T0).tz_name == name
    assert list(clock_module._zone_cache) == names[-clock_module._ZONE_CACHE_SIZE :]
    # a cached zone is not loaded again; an evicted one is
    DeviceClock.from_config({"TIMEZONE": names[-1]}, T0)
    assert len(zone_loads) == len(names)
    DeviceClock.from_config({"TIMEZONE": names[0]}, T0)
    assert len(zone_loads) == len(names) + 1
    assert next(reversed(clock_module._zone_cache)) == names[0]
