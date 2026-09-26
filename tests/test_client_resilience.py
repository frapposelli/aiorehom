"""RehomClient resilience: dead sockets, failed resyncs, fallbacks, device clock, API hygiene.

Regression tests from the 0.2 code review (half-open WebSocket, resync retry,
FALLBACK buffering, DST fold, skew flapping, ``last_synced_at``, ``dump()``
aliasing, caller-owned session, bounded request log).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import pytest

from aiorehom.client import ClientOptions, RehomClient
from aiorehom.enums import ConnectionState, UpdateReason
from aiorehom.exceptions import RehomConnectionError, RehomTimeoutError
from aiorehom.transport import ReadOnlyTransport

from .sync_fakes import ROME, T0, Harness, termo

CS = ConnectionState
MONO0 = 10_000.0  # VirtualClock's default monotonic start
SYNC_DONE = 2.7


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def advance_to(h: Harness, seconds: float) -> None:
    await h.clock.advance_to(t(seconds))


def interface_times(h: Harness) -> list[datetime]:
    return [when for name, when in h.transport.call_times if name == "get_interface"]


def zone_temperature(h: Harness) -> float | None:
    return h.client.state.zones["001"].temperature


async def closed_cleanly(h: Harness) -> None:
    await h.close()
    assert h.clock.pending_sleepers == 0
    assert len(h.client._tasks) == 0


# ---------------------------------------------------------------------------
# half-open WebSocket: the idle watchdog
# ---------------------------------------------------------------------------


async def test_a_silent_socket_is_dropped_and_reconnected_after_ws_idle_timeout() -> None:
    """Regression: a half-open socket kept the client CONNECTED forever (no FIN/RST ever)."""
    h = Harness(ws_idle_timeout=180.0)
    await h.connect()
    first = h.ws
    err = RehomTimeoutError("GET /api/alive/: timed out after 5 s")
    h.transport.fail("get_alive", err, err, err)  # the device is unreachable for ~90 s
    await advance_to(h, 100)
    assert h.client.connection_state is CS.UNAVAILABLE
    await advance_to(h, 179)
    assert len(h.connector.attempts) == 1  # alive recovered; the socket is still "live"
    # no frame since the socket went live at +0.1: dropped at +180.1, reconnected 1 s later
    await advance_to(h, 200)
    assert first.closed
    assert len(h.connector.attempts) == 2
    assert h.connector.attempts[1] == pytest.approx(MONO0 + 181.1, abs=1e-3)
    stats = h.client.stats
    assert (stats.ws_idle_timeouts, stats.reconnects, stats.resyncs) == (1, 1, 1)
    assert h.client.connection_state is CS.CONNECTED
    assert h.transitions == [
        CS.CONNECTING,
        CS.CONNECTED,
        CS.UNAVAILABLE,
        CS.CONNECTED,
        CS.DEGRADED,
        CS.CONNECTED,
    ]
    assert interface_times(h)[1] == pytest.approx(t(181.1), abs=timedelta(milliseconds=1))
    await closed_cleanly(h)


async def test_a_change_lost_on_a_dead_socket_arrives_with_the_reconnect_resync() -> None:
    h = Harness(ws_idle_timeout=180.0)
    await h.connect()
    await advance_to(h, 10)
    h.transport.set_value("ZONA", "001", "", "TEMP_AMBIENTE", "25")  # the frame never arrives
    await advance_to(h, 170)
    assert h.client.connection_state is CS.CONNECTED
    assert zone_temperature(h) == 21.5
    await advance_to(h, 190)
    assert zone_temperature(h) == 25.0
    assert h.updates[-1].reason is UpdateReason.RESYNC
    assert h.updates[-1].changed == {"ZONA.001..TEMP_AMBIENTE"}
    await closed_cleanly(h)


async def test_the_controller_heartbeat_keeps_the_socket() -> None:
    h = Harness(ws_idle_timeout=180.0)
    await h.connect()
    for minute in range(1, 11):  # the WATCHDOG_MASTER pulse, once a minute
        await advance_to(h, 60 * minute)
        await h.push(termo("PROC...WATCHDOG_MASTER", str(minute % 2)))
    await advance_to(h, 700)
    assert len(h.connector.attempts) == 1
    assert h.client.stats.ws_idle_timeouts == 0
    assert h.client.connection_state is CS.CONNECTED
    await closed_cleanly(h)


async def test_the_idle_watchdog_can_be_disabled() -> None:
    h = Harness(ws_idle_timeout=None)
    await h.connect()
    await advance_to(h, 3600)
    assert len(h.connector.attempts) == 1
    assert h.client.stats.ws_idle_timeouts == 0
    await closed_cleanly(h)


def test_ws_idle_timeout_option() -> None:
    assert ClientOptions().ws_idle_timeout == 180.0
    assert ClientOptions(ws_idle_timeout=None).ws_idle_timeout is None
    assert ClientOptions(ws_idle_timeout=120).ws_idle_timeout == 120
    for bad in (119.9, 0, float("nan"), float("inf"), True, "180"):
        with pytest.raises(ValueError, match="ws_idle_timeout"):
            ClientOptions(ws_idle_timeout=bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# failed syncs are retried early and are visible in the stats
# ---------------------------------------------------------------------------


async def test_a_failed_reconnect_resync_is_retried_after_min_resync_interval() -> None:
    """Regression: the deltas lost while the WS was down stayed missing for 10 minutes."""
    h = Harness()
    await h.connect()
    await advance_to(h, 100)
    h.ws.end()
    h.transport.set_value("ZONA", "001", "", "TEMP_AMBIENTE", "25")  # lost with the socket
    h.transport.fail("get_interface", RehomTimeoutError("GET /api/interface/: timed out"))
    await h.clock.settle()
    await advance_to(h, 110)  # reconnected at +101; its RESYNC failed at +103
    assert h.client.connection_state is CS.CONNECTED
    stats = h.client.stats
    assert (stats.sync_failures, stats.sync_failure_streak) == (1, 1)
    assert stats.last_sync_error == "RehomTimeoutError"
    assert zone_temperature(h) == 21.5
    await advance_to(h, 170)
    assert interface_times(h) == [t(0.1), t(101), t(163)]  # retried 60 s after the failure
    assert zone_temperature(h) == 25.0
    stats = h.client.stats
    assert (stats.resyncs, stats.sync_failures, stats.sync_failure_streak) == (1, 1, 0)
    assert stats.last_sync_error is None
    await closed_cleanly(h)


async def test_repeated_resync_failures_back_off_up_to_resync_interval() -> None:
    h = Harness()
    await h.connect()
    h.transport.interface = {"not": "a list"}  # an API change: every resync fails
    await advance_to(h, 70)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    with pytest.raises(Exception, match="unexpected payload"):
        await resync
    await advance_to(h, 3000)
    starts = [(when - T0).total_seconds() for when in interface_times(h)]
    # each attempt fails after the 2.0-s interface GET; the retry delay then doubles from
    # min_resync_interval (60, 120, 240, 480 s) up to resync_interval (600 s)
    assert starts == pytest.approx([0.1, 70, 132, 254, 496, 978, 1580, 2182, 2784])
    stats = h.client.stats
    assert stats.sync_failures == 8
    assert stats.sync_failure_streak == 8
    assert stats.last_sync_error == "RehomResponseError"
    assert h.client.connection_state is CS.CONNECTED  # the live stream still flows
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# a FALLBACK buffers frames like every other sync
# ---------------------------------------------------------------------------


async def test_frames_during_a_fallback_are_not_reverted_by_its_older_snapshot() -> None:
    """Regression: a WS coming back during a fallback had its frames overwritten."""
    h = Harness(options=ClientOptions(rest_fallback_after=48))
    await h.connect()
    await advance_to(h, 20)
    h.connector.fail_always = RehomConnectionError("GET /ws/: refused")
    h.ws.end()
    await h.clock.settle()
    assert h.client.connection_state is CS.DEGRADED
    await advance_to(h, 68.5)  # the fallback started at +68 (WS down + 48 s)
    assert interface_times(h)[-1] == t(68)
    h.connector.fail_always = None
    await advance_to(h, 69.3)  # the backoff reconnects at +69.26, mid-fallback
    assert h.client.connection_state is CS.CONNECTED
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "23"))
    await advance_to(h, 70.1)
    h.transport.set_value("ZONA", "001", "", "TEMP_AMBIENTE", "23")  # after its GET returned
    await advance_to(h, 75)  # the fallback replaced the stores at +70.6
    assert h.client.stats.fallbacks == 1
    assert zone_temperature(h) == 23.0
    temperatures = [u.state.zones["001"].temperature for u in h.updates]
    assert 23.0 in temperatures
    assert all(value == 23.0 for value in temperatures[temperatures.index(23.0) :])
    await advance_to(h, 140)  # the reconnect's RESYNC (deferred to +130.6) agrees
    assert zone_temperature(h) == 23.0
    assert h.client.stats.resyncs == 1
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# device clock: DST fold and skew hysteresis
# ---------------------------------------------------------------------------


async def test_an_exact_device_clock_in_the_repeated_hour_has_no_skew() -> None:
    """Regression: LOCAL_TIME read with fold=0 put the clock 1 h behind after DST ended."""
    start = datetime(2026, 10, 25, 1, 24, 47, tzinfo=UTC)  # 02:24:47 CET, the second 02:xx
    h = Harness(start=start)
    await h.connect()
    clock = h.client.device_clock
    assert clock is not None
    assert clock.skew == timedelta(0)
    await h.clock.advance_to(datetime(2026, 10, 25, 2, 3, tzinfo=UTC))
    clock = h.client.device_clock
    assert clock is not None
    assert clock.skew == timedelta(0)
    assert clock.local_now(datetime(2026, 10, 25, 2, 3, tzinfo=UTC)) == datetime(2026, 10, 25, 3, 3)
    await closed_cleanly(h)


async def test_a_constant_device_skew_of_two_to_three_seconds_does_not_flap() -> None:
    """Regression: the dead band ran before the hysteresis, so 0 <-> 2.x s flipped."""
    h = Harness()
    h.transport.skew = timedelta(seconds=2.4)
    await h.connect()
    seen = {h.client.device_clock}
    for step in range(1, 181):  # 3 h in 1-minute steps
        await advance_to(h, 60 * step)
        seen.add(h.client.device_clock)
    assert len(seen) == 1
    assert h.reasons() == ["sync"]  # no CONFIG/RESYNC publish for a clock that did not move
    assert h.transport.count("get_config") > 30
    # a real move (5 min) is still picked up
    h.transport.skew = timedelta(minutes=5)
    await advance_to(h, 60 * 181 + 310)
    clock = h.client.device_clock
    assert clock is not None
    assert abs(clock.skew - timedelta(minutes=5)) < timedelta(seconds=1)
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# public API hygiene
# ---------------------------------------------------------------------------


async def test_last_synced_at_follows_every_snapshot() -> None:
    """Regression: a no-diff resync publishes nothing, so state.synced_at lags."""
    h = Harness()
    assert h.client.last_synced_at is None
    await h.connect()
    assert h.client.last_synced_at == t(SYNC_DONE)
    await advance_to(h, 700)  # the periodic resync (+602.7..605.3) found no difference
    assert h.client.stats.resyncs == 1
    assert h.client.state.synced_at == t(SYNC_DONE)
    assert h.client.last_synced_at == t(SYNC_DONE + 600 + 2.6)
    assert h.client.dump()["synced_at"] == "2026-09-25T10:10:05.300000Z"
    await closed_cleanly(h)


async def test_dump_is_a_deep_copy() -> None:
    """Regression: nested /config/ values aliased the live store."""
    h = Harness()
    h.transport.config["__DEUM_HELP"] = ["a", "b"]
    h.transport.alive = {"version": "3.16.3", "extra": {"k": [1]}}
    await h.connect()
    dump = h.client.dump()
    dump["config"]["__DEUM_HELP"].append("MUTATED")
    dump["alive"]["extra"]["k"].append(2)
    again = h.client.dump()
    assert again["config"]["__DEUM_HELP"] == ["a", "b"]
    assert again["alive"]["extra"] == {"k": [1]}
    await closed_cleanly(h)


async def test_a_caller_session_is_not_modified_and_the_log_is_bounded() -> None:
    """Regression: HA's shared session lost its resend; the request log grew forever."""
    async with aiohttp.ClientSession() as session:
        client = RehomClient("rehomserver.local", username="u", password="p", session=session)
        assert session._retry_connection is True
        transport: Any = client._transport
        assert isinstance(transport, ReadOnlyTransport)
        assert transport._log.maxlen == 100
        await client.close()
        assert not session.closed


def test_the_harness_start_is_a_real_rome_fold() -> None:
    """Sanity check of the DST test's premise (02:24:47 local exists twice that night)."""
    first = datetime(2026, 10, 25, 2, 24, 47, tzinfo=ROME, fold=0).astimezone(UTC)
    second = datetime(2026, 10, 25, 2, 24, 47, tzinfo=ROME, fold=1).astimezone(UTC)
    assert second - first == timedelta(hours=1)
    assert second == datetime(2026, 10, 25, 1, 24, 47, tzinfo=UTC)
