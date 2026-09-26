"""RehomClient: connect, availability, timers, callbacks, history, close (design 2, 4.5-4.7)."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from aiorehom.builder import StateBuilder
from aiorehom.client import ClientOptions, RehomClient
from aiorehom.enums import ConnectionState, HistorySeries, UpdateReason
from aiorehom.exceptions import (
    RehomAuthenticationError,
    RehomConnectionError,
    RehomError,
    RehomNotReadyError,
    RehomResponseError,
    RehomTimeoutError,
)
from aiorehom.models import StateUpdate
from aiorehom.websocket import open_receive_only

from .sync_fakes import T0, FakeTransport, FakeWsConnection, FakeWsConnector, Harness, bus, termo

SYNC_DONE = 2.7
CS = ConnectionState


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def advance_to(h: Harness, seconds: float) -> None:
    await h.clock.advance_to(t(seconds))


async def connected(**kwargs: Any) -> Harness:
    h = Harness(**kwargs)
    await h.connect()
    return h


async def closed_cleanly(h: Harness) -> None:
    await h.close()
    assert h.clock.pending_sleepers == 0
    assert h.client.connection_state is CS.CLOSED
    assert len(h.client._tasks) == 0


def interface_times(h: Harness) -> list[datetime]:
    return [when for name, when in h.transport.call_times if name == "get_interface"]


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------


async def test_connect_sequence_call_log_and_credentials() -> None:
    h = Harness(username="user", password="pw")
    assert h.client.connection_state is CS.DISCONNECTED
    await h.connect()
    assert h.log == [
        "get_alive",
        "login",
        "ws_connect",
        "get_interface",
        "get_overrides",
        "get_plant_conf",
        "get_config",
    ]
    assert h.transport.calls == [
        ("get_alive", 5.0),
        ("login", None),
        ("get_interface", 15.0),
        ("get_overrides", 10.0),
        ("get_plant_conf", 10.0),
        ("get_config", 10.0),
    ]
    assert h.transport.logins == [("user", "pw")]
    assert h.client._username is None
    assert h.client._password is None
    assert h.transitions == [CS.CONNECTING, CS.CONNECTED]
    assert h.client.available
    assert h.reasons() == ["sync"]
    assert h.updates[0].state.synced_at == t(SYNC_DONE)  # the fake login takes no time
    state = h.client.state
    assert state.hub.web_version == "3.16.3"
    assert h.client.next_change_at() is None
    assert h.client.device_clock is not None
    assert h.client.device_clock.tz_name == "Europe/Rome"
    await closed_cleanly(h)
    # the WS was never asked to send: the fake connection has no send method at all
    assert not any(hasattr(conn, "send_str") for conn in h.connector.connections)


async def test_no_login_without_credentials_or_with_a_token() -> None:
    h = await connected()
    assert "login" not in h.log
    await closed_cleanly(h)
    h = await connected(username="user", password="pw", has_token=True)
    assert "login" not in h.log
    assert h.client._password is None
    await closed_cleanly(h)


async def test_connect_twice_and_connect_after_close() -> None:
    h = await connected()
    with pytest.raises(RuntimeError, match="once"):
        await h.client.connect()
    await closed_cleanly(h)
    other = Harness()
    await other.close()
    with pytest.raises(RehomNotReadyError):
        await other.client.connect()


@pytest.mark.parametrize(
    ("method", "error"),
    [
        ("get_alive", RehomConnectionError("GET /api/alive/: connection failed")),
        ("login", RehomAuthenticationError(401, "POST /api/get-token/: HTTP 401")),
        ("get_interface", RehomTimeoutError("GET /api/interface/: timed out")),
        ("get_config", RehomResponseError(200, "GET /api/config/: not JSON")),
    ],
)
async def test_connect_failure_cleans_up_and_leaves_disconnected(
    method: str, error: RehomError
) -> None:
    h = Harness(username="user", password="pw")
    h.transport.fail(method, error)
    task = asyncio.create_task(h.client.connect())
    await h.clock.advance(5)
    with pytest.raises(type(error)):
        await task
    assert h.client.connection_state is CS.DISCONNECTED
    assert h.transitions == [CS.CONNECTING, CS.DISCONNECTED]
    assert h.client._password is None
    assert h.clock.pending_sleepers == 0
    assert len(h.client._tasks) == 0
    assert all(conn.closed for conn in h.connector.connections)
    assert h.transport.count("login") <= 1
    with pytest.raises(RehomNotReadyError):
        _ = h.client.state
    await h.close()
    assert h.client.connection_state is CS.CLOSED


async def test_connect_cancelled_cleans_up_and_reraises() -> None:
    h = Harness()
    task = asyncio.create_task(h.client.connect())
    await h.clock.advance(1.0)  # inside the interface GET
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.client.connection_state is CS.DISCONNECTED
    assert h.clock.pending_sleepers == 0
    assert h.connector.current.closed
    await h.close()


async def test_close_during_connect_aborts_it() -> None:
    h = Harness()
    task = asyncio.create_task(h.client.connect())
    await h.clock.advance(1.0)
    await h.client.close()
    await h.clock.advance(5)
    with pytest.raises(RehomNotReadyError):
        await task
    assert h.client.connection_state is CS.CLOSED
    assert h.clock.pending_sleepers == 0


async def test_close_right_after_the_ws_handshake_closes_the_new_connection() -> None:
    h = Harness()
    h.connector.latency = 1.0
    task = asyncio.create_task(h.client.connect())
    await h.clock.advance(0.5)  # inside the WS handshake
    await h.client.close()
    await h.clock.advance(2)
    with pytest.raises(RehomNotReadyError):
        await task
    assert h.connector.current.closed


async def test_builder_failure_on_sync_fails_connect() -> None:
    h = Harness()
    h.builder.fail_next = 1
    task = asyncio.create_task(h.client.connect())
    await h.clock.advance(5)
    with pytest.raises(RuntimeError, match="builder failure"):
        await task
    await h.close()
    assert h.clock.pending_sleepers == 0


# ---------------------------------------------------------------------------
# WebSocket: degraded connect, backoff, drops
# ---------------------------------------------------------------------------


async def test_ws_failure_at_connect_is_degraded_then_reconnect_and_resync() -> None:
    h = Harness()
    err = RehomConnectionError("GET /ws/: handshake failed")
    h.connector.failures.extend([err, err, err])  # at +0.1, +1.1 and +2.6
    await h.connect(advance=3.0)
    assert h.client.connection_state is CS.DEGRADED
    assert h.transitions == [CS.CONNECTING, CS.DEGRADED]
    assert h.client.available
    assert h.reasons() == ["sync"]
    assert h.builder.live_seen[-1] is None
    await advance_to(h, 4.85)  # 2.6 + backoff(2) = 2.25
    assert len(h.connector.connections) == 1
    assert h.client.connection_state is CS.CONNECTED
    assert h.transitions == [CS.CONNECTING, CS.DEGRADED, CS.CONNECTED]
    await advance_to(h, SYNC_DONE + 60 + 3)
    assert interface_times(h) == [t(0.1), t(SYNC_DONE + 60)]
    stats = h.client.stats
    assert (stats.reconnects, stats.resyncs, stats.syncs) == (1, 1, 1)
    await closed_cleanly(h)


async def test_ws_reconnected_during_the_first_sync_still_resyncs() -> None:
    h = Harness()
    h.connector.failures.append(RehomConnectionError("GET /ws/: handshake failed"))
    await h.connect()  # the retry at +1.1 succeeds while the snapshot is being fetched
    assert h.transitions == [CS.CONNECTING, CS.CONNECTED]
    await advance_to(h, SYNC_DONE + 60 + 3)
    assert interface_times(h) == [t(0.1), t(SYNC_DONE + 60)]
    await closed_cleanly(h)


async def test_backoff_sequence_with_zero_jitter() -> None:
    h = Harness()
    h.connector.fail_always = RehomConnectionError("GET /ws/: refused")
    await h.connect()
    await advance_to(h, 300)
    attempts = h.connector.attempts
    gaps = [round(b - a, 6) for a, b in itertools.pairwise(attempts)]
    # the virtual clock has 1-us resolution: 17.0859375 -> 17.085938
    expected = [1.0, 1.5, 2.25, 3.375, 5.0625, 7.59375, 11.390625, 17.085938, 25.628906]
    assert gaps[: len(expected)] == expected
    assert set(gaps[len(expected) :]) == {30.0}
    await closed_cleanly(h)


async def test_backoff_resets_after_a_stable_connection() -> None:
    h = await connected()
    await advance_to(h, 5)
    h.ws.end()  # up 4.9 s: not stable
    await advance_to(h, 6)  # backoff(0) = 1
    assert len(h.connector.connections) == 2
    await advance_to(h, 7)
    h.ws.end()  # up 1 s: not stable -> attempt 1
    await advance_to(h, 8.5)  # backoff(1) = 1.5
    assert len(h.connector.connections) == 3
    await advance_to(h, 20)
    h.ws.end()  # up 11.5 s: stable -> attempt resets
    await advance_to(h, 21)
    assert len(h.connector.connections) == 4
    gaps = [round(b - a, 6) for a, b in itertools.pairwise(h.connector.attempts)]
    assert gaps == [6.0 - 0.1, 2.5, 12.5]
    assert h.client.stats.reconnects == 3
    await closed_cleanly(h)


async def test_receive_error_is_a_drop_and_live_since_follows_the_socket() -> None:
    h = await connected()
    assert h.builder.live_seen[-1] == t(0.1)
    await advance_to(h, 30)
    h.ws.push(RuntimeError("socket exploded"))
    await h.clock.settle()
    assert h.client.connection_state is CS.DEGRADED
    # the heartbeat view changed (live_since None): a CLOCK rebuild is published
    assert h.updates[-1].reason is UpdateReason.CLOCK
    assert h.updates[-1].state.health.heartbeat_ok is None
    assert h.updates[-1].changed == frozenset()
    await advance_to(h, 31)
    assert h.client.connection_state is CS.CONNECTED
    assert h.updates[-1].state.health.heartbeat_ok is True
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# REST fallback and availability
# ---------------------------------------------------------------------------


async def test_rest_fallback_every_60_s_and_failure_is_unavailable() -> None:
    h = Harness()
    h.connector.fail_always = RehomConnectionError("GET /ws/: refused")
    await h.connect()
    assert h.client.connection_state is CS.DEGRADED
    # WS down since +0.1 -> fallback due at +60.1, but min_resync_interval -> +62.7
    await advance_to(h, 130)
    assert interface_times(h) == [t(0.1), t(SYNC_DONE + 60), t(SYNC_DONE + 60 + 2.6 + 60)]
    assert h.client.stats.fallbacks == 2
    assert h.client.connection_state is CS.DEGRADED
    h.transport.fail("get_interface", RehomConnectionError("GET /api/interface/: down"))
    await advance_to(h, 190)  # the third fallback (+188.0) fails
    assert h.client.connection_state is CS.UNAVAILABLE
    assert not h.client.available
    await advance_to(h, 260)  # the next one succeeds
    assert h.client.connection_state is CS.DEGRADED
    assert h.client.stats.fallbacks == 3
    h.connector.fail_always = None
    await advance_to(h, 300)
    assert h.client.connection_state is CS.CONNECTED
    assert h.transitions == [CS.CONNECTING, CS.DEGRADED, CS.UNAVAILABLE, CS.DEGRADED, CS.CONNECTED]
    fallbacks = h.client.stats.fallbacks
    await advance_to(h, 500)
    assert h.client.stats.fallbacks == fallbacks  # no fallback while the WS is live
    await closed_cleanly(h)


async def test_fallback_publishes_only_differences() -> None:
    h = Harness()
    h.connector.fail_always = RehomConnectionError("GET /ws/: refused")
    await h.connect()
    await advance_to(h, 70)
    assert h.reasons() == ["sync"]
    h.transport.set_value("REHOM", "", "", "TEMP_COM", "21")
    await advance_to(h, 130)
    assert h.reasons() == ["sync", "fallback"]
    assert h.updates[-1].changed == {"REHOM...TEMP_COM"}
    await closed_cleanly(h)


async def test_three_alive_failures_make_it_unavailable_until_one_succeeds() -> None:
    h = await connected()
    err = RehomTimeoutError("GET /api/alive/: timed out after 5 s")
    h.transport.fail("get_alive", err, err, err)
    await advance_to(h, SYNC_DONE + 30 + 1)
    await advance_to(h, SYNC_DONE + 60 + 1)
    assert h.client.connection_state is CS.CONNECTED
    await advance_to(h, SYNC_DONE + 90 + 1)
    assert h.client.connection_state is CS.UNAVAILABLE
    await advance_to(h, SYNC_DONE + 120 + 2)
    assert h.client.connection_state is CS.CONNECTED
    assert h.transitions == [CS.CONNECTING, CS.CONNECTED, CS.UNAVAILABLE, CS.CONNECTED]
    assert h.client.stats.alive_failures == 3
    await closed_cleanly(h)


async def test_alive_version_change_triggers_a_resync() -> None:
    h = await connected()
    h.transport.alive = {"version": "3.17.0"}
    await advance_to(h, 40)  # first alive poll at +32.7 sees the new version
    assert dict(h.client._stores.alive) == {"version": "3.17.0"}
    await advance_to(h, SYNC_DONE + 60 + 3)
    assert interface_times(h)[1] == t(SYNC_DONE + 60)
    update = h.updates[-1]
    assert update.reason is UpdateReason.RESYNC
    assert update.state.hub.web_version == "3.17.0"
    assert update.changed == frozenset()  # only the model moved
    # an alive body without a version never triggers anything
    h.transport.alive = ["odd"]
    await advance_to(h, 200)
    assert h.client.stats.resyncs == 1
    await closed_cleanly(h)


async def test_config_poll_updates_the_skew_and_publishes_config() -> None:
    options = ClientOptions(config_interval=60, resync_interval=3600)
    h = await connected(options=options)
    assert h.client.state.device_clock.skew == timedelta(0)
    h.transport.skew = timedelta(minutes=5)
    await advance_to(h, SYNC_DONE + 60 + 1)
    update = h.updates[-1]
    assert update.reason is UpdateReason.CONFIG
    assert update.config_changed == frozenset()  # LOCAL_TIME is never a change
    # LOCAL_TIME has 1-s resolution (truncated): the measured skew is 299.1 s here
    assert abs(update.state.device_clock.skew - timedelta(minutes=5)) < timedelta(seconds=1)
    count = len(h.updates)
    h.transport.skew = timedelta(minutes=5, seconds=1)  # within the dead band: kept
    await advance_to(h, SYNC_DONE + 120 + 2)
    assert len(h.updates) == count
    h.transport.config["VERSION"] = "3.17.0"
    await advance_to(h, SYNC_DONE + 180 + 2)
    assert h.updates[-1].reason is UpdateReason.CONFIG
    assert h.updates[-1].config_changed == {"VERSION"}
    h.transport.fail("get_config", RehomConnectionError("GET /api/config/: down"))
    await advance_to(h, SYNC_DONE + 240 + 2)
    assert h.client.connection_state is CS.CONNECTED
    await closed_cleanly(h)


async def test_config_poll_during_a_sync_is_left_to_the_sync() -> None:
    options = ClientOptions(config_interval=60, interface_timeout=15)
    h = await connected(options=options)
    h.transport.latency["get_interface"] = 30.0
    h.transport.config["VERSION"] = "9"
    resync = asyncio.create_task(h.client.resync())
    await advance_to(h, SYNC_DONE + 100)
    await resync
    assert h.updates[-1].reason is UpdateReason.RESYNC  # the sync brought the change
    assert h.updates[-1].config_changed == {"VERSION"}
    assert [u.reason for u in h.updates].count(UpdateReason.CONFIG) == 0
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


async def test_frames_50_ms_apart_are_one_update_150_ms_apart_are_two() -> None:
    h = await connected()
    await advance_to(h, 10)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "20.1"))
    await h.clock.advance(0.05)
    await h.push(termo("ZONA.002..TEMP_AMBIENTE", "20.2"))
    await h.clock.advance(0.2)
    assert h.reasons() == ["sync", "frames"]
    assert h.updates[-1].changed == {"ZONA.001..TEMP_AMBIENTE", "ZONA.002..TEMP_AMBIENTE"}
    assert h.updates[-1].at == t(10.1)
    await advance_to(h, 20)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "20.3"))
    await h.clock.advance(0.15)
    await h.push(termo("ZONA.002..TEMP_AMBIENTE", "20.4"))
    await h.clock.advance(0.2)
    assert h.reasons() == ["sync", "frames", "frames", "frames"]
    assert [u.changed for u in h.updates[-2:]] == [
        {"ZONA.001..TEMP_AMBIENTE"},
        {"ZONA.002..TEMP_AMBIENTE"},
    ]
    await closed_cleanly(h)


async def test_a_value_flapping_inside_one_batch_is_still_reported() -> None:
    h = await connected()
    await h.push(termo("ZONA.001..ATTIVA", "1"), termo("ZONA.001..ATTIVA", "0"))
    await h.clock.advance(0.2)
    update = h.updates[-1]
    assert update.changed == {"ZONA.001..ATTIVA"}
    assert update.state == update.previous  # changed paths are never netted out
    await closed_cleanly(h)


async def test_bus_and_commissioning_frames() -> None:
    h = await connected()
    await h.push(bus("stagione", "0"))
    await h.clock.advance(0.2)
    assert h.updates[-1].conf_changed == {"stagione"}
    assert h.updates[-1].state.plant_conf["stagione"] == "0"
    await h.push(bus("RIS_CONFIGURA", "1"))
    await h.clock.advance(0.2)
    update = h.updates[-1]
    assert update.reason is UpdateReason.FRAMES
    assert update.conf_changed == frozenset()
    assert update.state.plant.installer_activity_at is not None
    await closed_cleanly(h)


async def test_clock_tick_fires_at_next_change_at_plus_margin() -> None:
    h = Harness()
    half = t(30 * 60)

    def next_change(now: datetime) -> datetime | None:
        return half if now < half else half + timedelta(minutes=30)

    h.builder.next_change = next_change
    await h.connect()
    assert h.client.next_change_at() == half
    await advance_to(h, 30 * 60)
    assert h.reasons() == ["sync"]
    await h.clock.advance(0.05)
    update = h.updates[-1]
    assert update.reason is UpdateReason.CLOCK
    assert update.at == half + timedelta(seconds=0.05)
    assert update.changed == frozenset()
    assert update.state.next_change_at == half + timedelta(minutes=30)
    await closed_cleanly(h)


async def test_clock_tick_does_not_loop_on_a_stale_next_change_at() -> None:
    h = Harness()
    h.builder.next_change = lambda _now: t(100)
    await h.connect()
    await advance_to(h, 200)
    builds = len(h.builder.builds)
    assert h.reasons() == ["sync"]
    await advance_to(h, 250)
    assert len(h.builder.builds) == builds  # the tick is not re-armed for a past instant
    # an early wake-up (state unchanged, instant still ahead) re-arms the tick
    h.builder.next_change = lambda _now: t(400)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "19"))
    await h.clock.advance(0.2)
    assert h.client._tick.deadline is not None
    h.client._on_tick()  # simulate a wake-up before the instant
    assert h.client._tick.deadline is not None
    await advance_to(h, 401)
    assert h.client._tick.deadline is None
    await closed_cleanly(h)


async def test_builder_errors_in_background_are_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = await connected()
    h.builder.fail_next = 1
    with caplog.at_level(logging.ERROR, logger="aiorehom.client"):
        await h.push(termo("ZONA.001..TEMP_AMBIENTE", "19"))
        await h.clock.advance(0.2)
    assert "building the state failed" in caplog.text
    assert h.reasons() == ["sync"]
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "18"))
    await h.clock.advance(0.2)
    assert h.reasons() == ["sync", "frames"]
    await closed_cleanly(h)


async def test_subscriber_isolation_and_unsubscribe_during_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = Harness()
    calls: list[str] = []
    unsubscribe_b: list[Any] = []

    def a(_update: StateUpdate) -> None:
        calls.append("a")
        raise ValueError("subscriber bug (test)")

    def b(_update: StateUpdate) -> None:
        calls.append("b")
        unsubscribe_b[0]()
        unsubscribe_b[0]()  # idempotent

    def c(_update: StateUpdate) -> None:
        calls.append("c")
        unsubscribe_d()

    def d(_update: StateUpdate) -> None:
        calls.append("d")

    h.client.subscribe(a)
    unsubscribe_b.append(h.client.subscribe(b))
    h.client.subscribe(c)
    unsubscribe_d = h.client.subscribe(d)
    with caplog.at_level(logging.ERROR, logger="aiorehom.client"):
        await h.connect()
    assert calls == ["a", "b", "c"]  # d was removed during dispatch, before its turn
    assert "subscriber callback" in caplog.text
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "19"))
    await h.clock.advance(0.2)
    assert calls == ["a", "b", "c", "a", "c"]
    assert len(h.updates) == 2  # the harness subscriber still gets everything
    with pytest.raises(TypeError):
        h.client.subscribe("not callable")  # type: ignore[arg-type]
    await closed_cleanly(h)


async def test_connection_callbacks_fire_on_transitions_only() -> None:
    h = Harness()
    seen: list[ConnectionState] = []
    remove = h.client.on_connection_change(seen.append)
    await h.connect()
    await advance_to(h, 40)  # alive polls succeed: no repeats
    assert seen == [CS.CONNECTING, CS.CONNECTED]
    remove()
    await h.close()
    assert seen == [CS.CONNECTING, CS.CONNECTED]
    assert h.transitions[-1] is CS.CLOSED


# ---------------------------------------------------------------------------
# not ready, close, guards, options
# ---------------------------------------------------------------------------


async def test_not_ready_errors_before_connect_and_after_close() -> None:
    h = Harness()
    with pytest.raises(RehomNotReadyError):
        _ = h.client.state
    with pytest.raises(RehomNotReadyError):
        await h.client.resync()
    with pytest.raises(RehomNotReadyError):
        await h.client.get_history("TEMP_ESTERNA", start=t(-3600), end=T0)
    await h.connect()
    state = h.client.state
    await h.close()
    assert h.client.state is state  # still readable
    with pytest.raises(RehomNotReadyError):
        await h.client.resync()
    with pytest.raises(RehomNotReadyError):
        await h.client.get_history("TEMP_ESTERNA", start=t(-3600), end=T0)


async def test_close_is_idempotent_and_fails_pending_resyncs() -> None:
    h = await connected()
    await advance_to(h, 10)
    resync = asyncio.create_task(h.client.resync())  # waits for min_resync_interval
    await h.clock.settle()
    await asyncio.gather(h.client.close(), h.client.close())
    await h.client.close()
    with pytest.raises(RehomNotReadyError):
        await resync
    assert h.clock.pending_sleepers == 0
    assert h.transport.closed == 0  # an injected transport is not ours to close
    assert h.ws.closed
    assert h.transitions[-1] is CS.CLOSED
    assert h.transitions.count(CS.CLOSED) == 1


async def test_close_during_a_resync() -> None:
    h = await connected()
    await advance_to(h, 70)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(1.0)
    await h.client.close()
    with pytest.raises(RehomNotReadyError):
        await resync
    assert h.clock.pending_sleepers == 0


async def test_close_never_swallows_its_own_cancellation_and_can_be_repeated() -> None:
    h = await connected()

    class SlowClose(FakeWsConnection):
        async def close(self) -> None:
            await h.clock.sleep(10)
            await super().close()

    slow = SlowClose()

    async def connector() -> FakeWsConnection:
        return slow

    h.client._ws_connector = connector
    h.ws.end()
    await advance_to(h, 10)  # reconnected (backoff 1 s) to the slow connection
    assert h.client._ws_conn is slow
    closing = asyncio.create_task(h.client.close())
    await h.clock.settle()
    assert not closing.done()  # stuck in the slow close
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert h.client.connection_state is CS.CLOSED
    finishing = asyncio.create_task(h.client.close())
    await h.clock.advance(20)
    await finishing
    assert slow.closed
    assert h.clock.pending_sleepers == 0
    assert len(h.client._tasks) == 0


async def test_async_context_manager_closes() -> None:
    h = Harness()
    async with h.client as client:
        assert client is h.client
        task = asyncio.create_task(client.connect())
        await h.clock.advance(5)
        await task
    assert h.client.connection_state is CS.CLOSED
    assert h.clock.pending_sleepers == 0
    assert "closed" in repr(h.client)


def test_constructor_guards() -> None:
    clock_h = Harness()
    transport = clock_h.transport
    connector = clock_h.connector
    with pytest.raises(ValueError, match="ws_connector"):
        RehomClient("replay.invalid", transport=transport)
    with pytest.raises(ValueError, match="session"):
        RehomClient(
            "replay.invalid",
            transport=transport,
            ws_connector=connector,
            session=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="username and password"):
        RehomClient("rehom.test")
    with pytest.raises(ValueError, match="username and password"):
        RehomClient("rehom.test", username="user")
    with pytest.raises(ValueError, match="host"):
        RehomClient("http://rehom.test", transport=transport, ws_connector=connector)
    with pytest.raises(ValueError, match="port"):
        RehomClient("rehom.test", port=0, transport=transport, ws_connector=connector)
    with pytest.raises(ValueError, match="ws_port"):
        RehomClient("rehom.test", ws_port=True, transport=transport, ws_connector=connector)
    with pytest.raises(ValueError, match="options"):
        RehomClient(
            "rehom.test",
            transport=transport,
            ws_connector=connector,
            options={"alive_interval": 30},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="username"):
        RehomClient("rehom.test", username=1, password="x")  # type: ignore[arg-type]


async def test_default_construction_owns_its_transport_and_connector() -> None:
    client = RehomClient("rehom.test", username="user", password="pw")
    assert isinstance(client._builder, StateBuilder)
    connector = client._ws_connector
    assert isinstance(connector, partial)
    assert connector.func is open_receive_only
    assert connector.args == ("rehom.test", 8000)
    assert connector.keywords["connect_timeout"] == 10.0
    assert connector.keywords["gate"] == client._transport.pacing_slot  # type: ignore[attr-defined]
    assert connector.keywords["connector"] is None  # a private, owned connector per socket
    assert client.host == "rehom.test"
    assert client.options == ClientOptions()
    await client.close()
    await client.close()


async def test_injected_session_lends_its_connector_to_the_websocket() -> None:
    """REST and WebSocket resolve the host alike: the WS session borrows the connector."""
    import aiohttp

    session = aiohttp.ClientSession()
    try:
        client = RehomClient("rehom.test", username="user", password="pw", session=session)
        connector = client._ws_connector
        assert isinstance(connector, partial)
        assert connector.keywords["connector"] is session.connector
        await client.close()
        assert not session.closed  # the caller's session is never closed
        assert session.connector is not None
        assert not session.connector.closed
    finally:
        await session.close()


async def test_owned_transport_end_to_end_with_mocked_http() -> None:
    from aioresponses import aioresponses

    from .conftest import BASE
    from .sync_fakes import config_payload, interface_rows

    harness = Harness()  # only for its clock and fake connector
    clock, connector = harness.clock, harness.connector
    config = config_payload()
    config["LOCAL_TIME"] = "2026-09-25 12:00:00"
    with aioresponses() as mocked:
        mocked.get(f"{BASE}/api/alive/", payload={"version": "1"}, repeat=True)
        mocked.post(f"{BASE}/api/get-token/", payload={"token": "tok-abcdef0123456789"})
        mocked.get(f"{BASE}/api/interface/", payload=interface_rows())
        mocked.get(f"{BASE}/api/overrides/", payload=[])
        mocked.get(f"{BASE}/api/plant/conf/", payload={"stagione": "1"})
        mocked.get(f"{BASE}/api/config/", payload=config)
        client = RehomClient(
            "rehom.test",
            username="user",
            password="pw",
            ws_connector=connector,
            clock=clock,
            builder=harness.builder,
            rng=lambda: 0.0,
        )
        task = asyncio.create_task(client.connect())
        for _ in range(20):
            await clock.advance(1)
            await asyncio.sleep(0.01)  # aioresponses completes on the real loop
            if task.done():
                break
        await task
        methods = [
            (method, url.path) for (method, url), calls in mocked.requests.items() for _ in calls
        ]
        assert ("POST", "/api/get-token/") in methods
        assert [m for m, _p in methods].count("POST") == 1
        assert {m for m, _p in methods} == {"GET", "POST"}
        assert client.connection_state is CS.CONNECTED
        assert client.state.hub.controller_version == "3.1.0.12"
        await client.close()
    assert clock.pending_sleepers == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alive_interval", 14.9),
        ("alive_interval", 301),
        ("alive_timeout", 4),
        ("alive_failures_unavailable", 0),
        ("alive_failures_unavailable", 1.0),
        ("alive_failures_unavailable", True),
        ("config_interval", 59),
        ("resync_interval", 59),
        ("min_resync_interval", 30),
        ("interface_timeout", 10),
        ("request_timeout", 4.9),
        ("request_min_interval", 0.5),
        ("ws_connect_timeout", 0),
        ("rest_fallback_after", 29),
        ("rest_fallback_interval", 59),
        ("remove_coalesce_window", 0),
        ("remove_coalesce_window", 5.1),
        ("notify_batch_window", -0.1),
        ("notify_batch_window", 1.1),
        ("alarm_debounce", 29),
        ("heartbeat_stale_after", 119),
        ("forecast_burst_gap", 0),
        ("max_buffered_frames", 99),
        ("max_buffered_frames", 100.0),
        ("history_chunk_spacing", 4),
        ("clock_tick_margin", -0.01),
        ("clock_tick_margin", 1.5),
        ("alive_interval", float("nan")),
        ("alive_interval", "30"),
        ("alive_interval", True),
    ],
)
def test_client_options_validation(field: str, value: Any) -> None:
    with pytest.raises(ValueError, match=field):
        ClientOptions(**{field: value})


def test_client_options_boundaries_are_accepted() -> None:
    options = ClientOptions(
        alive_interval=15,
        alive_timeout=5,
        alive_failures_unavailable=1,
        remove_coalesce_window=5,
        notify_batch_window=0,
        clock_tick_margin=1,
        max_buffered_frames=100,
        ws_connect_timeout=0.1,
    )
    assert options.notify_batch_window == 0
    with pytest.raises(AttributeError):
        options.alive_interval = 20  # type: ignore[misc]


async def test_zero_batch_window_flushes_on_the_next_loop_turn() -> None:
    h = await connected(options=ClientOptions(notify_batch_window=0))
    await advance_to(h, 10)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "19"))
    assert h.updates[-1].at == t(10)
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def _history(_key: str, _unita: str, gte: str, _lte: str) -> list[Any]:
    if gte.startswith("2026-09-24"):
        return [
            {"rowid": 2, "Valore": 24.5, "Tempo": "2026-09-24T18:00:00"},
            {"rowid": 1, "Valore": "24.0", "Tempo": "2026-09-24 12:00:00"},
            {"rowid": 3, "Valore": 25, "Tempo": "2026-09-25T06:00:00"},  # on the boundary
            {"Valore": 1.0, "Tempo": "2026-09-24 20:00:00"},  # no rowid
            {"Valore": 1.0, "Tempo": "2026-09-24 20:00:00"},  # dup on Tempo
            {"rowid": 9, "Valore": "abc", "Tempo": "2026-09-24 10:00:00"},
            {"rowid": 10, "Valore": True, "Tempo": "2026-09-24 10:00:00"},
            {"rowid": 11, "Valore": float("inf"), "Tempo": "2026-09-24 10:00:00"},
            {"rowid": 12, "Valore": None, "Tempo": "2026-09-24 10:00:00"},
            {"rowid": "13", "Valore": 1, "Tempo": "2026-09-24 10:00:00"},
            {"rowid": 14, "Valore": 1, "Tempo": "24/09/2026 10:00"},
            {"rowid": 15, "Valore": 1, "Tempo": None},
            {"rowid": 16, "Valore": 1, "Tempo": "2026-02-30 10:00:00"},
            "not a row",
        ]
    return [
        {"rowid": 3, "Valore": 25, "Tempo": "2026-09-25T06:00:00"},
        {"rowid": 4, "Valore": 23.5, "Tempo": "2026-09-25 11:30:00.5"},
    ]


async def test_get_history_chunks_parses_dedupes_and_sorts() -> None:
    h = await connected()
    h.transport.history = _history
    await advance_to(h, 100)
    task = asyncio.create_task(
        h.client.get_history(
            HistorySeries.TEMP_AMBIENTE, "001", start=T0 - timedelta(hours=30), end=T0
        )
    )
    await h.clock.advance(20)
    samples = await task
    calls = h.transport.history_calls
    assert [c[:4] for c in calls] == [
        ("TEMP_AMBIENTE", "001", "2026-09-24 06:00:00", "2026-09-25 06:00:00"),
        ("TEMP_AMBIENTE", "001", "2026-09-25 06:00:00", "2026-09-25 12:00:00"),
    ]
    assert calls[1][4] - calls[0][4] == pytest.approx(0.5 + 5.0)  # >= 5 s after the first ended
    assert h.transport.calls[-1] == ("get_history", 10.0)
    assert [(s.rowid, s.value) for s in samples] == [
        (1, 24.0),
        (2, 24.5),
        (None, 1.0),
        (3, 25.0),
        (4, 23.5),
    ]
    first = samples[0]
    assert first.local_time == datetime(2026, 9, 24, 12)
    assert first.time == datetime(2026, 9, 24, 10, tzinfo=UTC)
    await closed_cleanly(h)


async def test_get_history_single_chunk_and_string_series() -> None:
    h = await connected()
    task = asyncio.create_task(
        h.client.get_history(
            "TEMP_ESTERNA",
            start=T0 - timedelta(hours=1, microseconds=5),
            end=T0 + timedelta(microseconds=1),
        )
    )
    await h.clock.advance(2)
    assert await task == ()
    (call,) = h.transport.history_calls
    assert call[:4] == ("TEMP_ESTERNA", "", "2026-09-25 10:59:59", "2026-09-25 12:00:01")
    await closed_cleanly(h)


@pytest.mark.parametrize(
    ("series", "unit", "start", "end", "match"),
    [
        ("TEMP_AMBIENTE", "1", t(-60), T0, "zone id"),
        ("TEMP_AMBIENTE", "", t(-60), T0, "zone id"),
        ("TEMP_ESTERNA", "001", t(-60), T0, "no unit"),
        ("NOPE", "", t(-60), T0, "NOPE"),
        ("TEMP_ESTERNA", "", datetime(2026, 9, 25), T0, "aware"),
        ("TEMP_ESTERNA", "", t(-60), "x", "aware"),
        ("TEMP_ESTERNA", "", T0, T0, "after"),
        ("TEMP_ESTERNA", "", T0 - timedelta(days=7, seconds=1), T0, "7 days"),
    ],
)
async def test_get_history_validation(
    series: str, unit: str, start: Any, end: Any, match: str
) -> None:
    h = await connected()
    with pytest.raises(ValueError, match=match):
        await h.client.get_history(series, unit, start=start, end=end)
    assert h.transport.history_calls == []
    await closed_cleanly(h)


async def test_get_history_bad_payload_and_close_between_chunks() -> None:
    h = await connected()
    h.transport.history = lambda *_a: {"not": "a list"}
    task = asyncio.create_task(h.client.get_history("TEMP_ESTERNA", start=t(-3600), end=T0))
    await h.clock.advance(2)
    with pytest.raises(RehomResponseError, match="history"):
        await task
    h.transport.history = lambda *_a: []
    task = asyncio.create_task(h.client.get_history("TEMP_ESTERNA", start=t(-40 * 3600), end=T0))
    await h.clock.advance(1)  # first chunk done, waiting for the spacing
    await h.client.close()
    await h.clock.advance(10)
    with pytest.raises(RehomNotReadyError):
        await task
    assert len(h.transport.history_calls) == 2  # the bad-payload call + one chunk


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


async def test_dump_and_stats() -> None:
    h = Harness()
    assert h.client.dump()["synced_at"] is None
    h.transport.overrides = [
        {
            "Gruppo": "PROG_OVERRIDE",
            "Unita": "001",
            "SubUni": "1",
            "Key": "PROG_GIORNO_ESTATE",
            "Valore": "1",
            "Impostazione": "2026-09-25 12:00:00",
            "Scadenza": "2026-09-25 13:00:00",
        }
    ]
    await h.connect()
    await h.push(termo("METEO_DATA...dt1790337600", '{"dt": 1790337600}'))
    await h.clock.advance(0.2)
    dump = h.client.dump()
    text = json.dumps(dump)  # JSON-serialisable
    assert "not-a-real-password" not in text
    assert "0123456789abcdef" not in text
    assert set(dump) == {
        "interface",
        "overrides",
        "plant_conf",
        "config",
        "alive",
        "forecast",
        "stats",
        "connection_state",
        "synced_at",
    }
    assert dump["overrides"][0]["Scadenza"] == "2026-09-25 13:00:00"
    assert dump["forecast"]["items"] == {"1790337600": '{"dt": 1790337600}'}
    assert dump["forecast"]["received_at"] == "2026-09-25T10:00:05Z"  # pushed after connect
    assert dump["connection_state"] == "connected"
    assert dump["synced_at"] == "2026-09-25T10:00:02.700000Z"
    assert dump["stats"]["frames_received"] == 1
    assert dump["stats"]["forecast_frames"] == 1
    assert dump["stats"]["notifications"] == 2
    await closed_cleanly(h)


async def test_the_client_uses_only_the_read_transport_protocol() -> None:
    """Invariant 1: FakeTransport has only protocol methods, so anything else would fail."""
    h = await connected(username="user", password="pw")
    await advance_to(h, 1300)
    names = set(h.transport.names())
    assert names <= {
        "get_alive",
        "login",
        "get_interface",
        "get_overrides",
        "get_plant_conf",
        "get_config",
        "get_history",
    }
    assert h.transport.count("login") == 1
    await closed_cleanly(h)
    assert isinstance(h.transport, FakeTransport)
    assert isinstance(h.connector, FakeWsConnector)


async def test_sync_loads_the_controller_time_zone_off_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ZoneInfo`` reads the tz database on a first load: never on the event loop."""
    import threading

    from aiorehom import clock as clock_module

    monkeypatch.setattr(clock_module, "_zone_cache", {})
    loads: list[tuple[str, threading.Thread]] = []
    real = clock_module.ZoneInfo

    def recording(key: str) -> Any:
        loads.append((key, threading.current_thread()))
        return real(key)

    monkeypatch.setattr(clock_module, "ZoneInfo", recording)
    loop_thread = threading.current_thread()
    h = await connected(options=ClientOptions(config_interval=60, resync_interval=3600))
    assert h.client.device_clock is not None
    assert h.client.device_clock.tz_name == "Europe/Rome"
    assert [key for key, _thread in loads] == ["Europe/Rome"]
    assert all(thread is not loop_thread for _key, thread in loads)

    # a config poll that brings a new zone loads it off the loop too
    h.transport.config["TIMEZONE"] = "America/New_York"
    await advance_to(h, SYNC_DONE + 60 + 1)
    for _ in range(200):  # the worker thread runs in real time
        if h.client.device_clock.tz_name == "America/New_York":
            break
        await asyncio.sleep(0.005)
    assert h.client.device_clock.tz_name == "America/New_York"
    assert [key for key, _thread in loads] == ["Europe/Rome", "America/New_York"]
    assert all(thread is not loop_thread for _key, thread in loads)
    await closed_cleanly(h)
