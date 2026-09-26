"""Sync engine through the real client, with fakes and a VirtualClock (design 4.3-4.7).

Harness timing (FakeTransport latencies): alive 0.1 s, interface 2.0 s, overrides,
plant conf and config 0.2 s each.  ``connect()`` at T0 therefore sends the
interface GET at T0+0.1 and publishes SYNC at T0+2.7 (``last_sync_done``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from aiorehom.client import ClientOptions
from aiorehom.clock import VirtualClock
from aiorehom.enums import ConnectionState, UpdateReason
from aiorehom.exceptions import RehomConnectionError, RehomResponseError
from aiorehom.state import StoreSet
from aiorehom.sync import fetch_snapshot

from .sync_fakes import T0, FakeTransport, Harness, termo

SYNC_DONE = 2.7  # alive 0.1 + snapshot 2.6
RESYNC_TAKES = 2.6  # a resync has no alive GET


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def advance_to(h: Harness, seconds: float) -> None:
    await h.clock.advance_to(t(seconds))


async def connected(**kwargs: object) -> Harness:
    h = Harness(**kwargs)  # type: ignore[arg-type]
    await h.connect()
    return h


async def closed_cleanly(h: Harness) -> None:
    await h.close()
    assert h.clock.pending_sleepers == 0
    assert h.client.connection_state is ConnectionState.CLOSED


# ---------------------------------------------------------------------------
# WS-first buffering
# ---------------------------------------------------------------------------


async def test_frames_received_during_the_snapshot_are_replayed_after_it_in_order() -> None:
    h = Harness()
    task = asyncio.create_task(h.client.connect())
    await h.clock.settle()
    await advance_to(h, 1.0)
    assert h.transport.names() == ["get_alive", "get_interface"]
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "21.7"))
    await advance_to(h, 1.5)
    await h.push(
        termo("ZONA.001..TEMP_AMBIENTE", "21.9"),
        termo("ZONA.002..TEMP_AMBIENTE", "22.0"),  # equal to the snapshot's "22"
    )
    await advance_to(h, 2.0)
    assert h.updates == []  # nothing is applied while the snapshot is being fetched
    await advance_to(h, 5.0)
    await task
    sync, frames = h.updates
    assert sync.reason is UpdateReason.SYNC
    assert sync.previous is None
    assert sync.at == t(SYNC_DONE)
    assert sync.state.zones["001"].temperature == 21.5  # the snapshot, not the buffer
    assert frames.reason is UpdateReason.FRAMES
    assert frames.at == t(SYNC_DONE + 0.1)
    assert frames.changed == {"ZONA.001..TEMP_AMBIENTE"}
    assert frames.previous is sync.state
    assert frames.state.zones["001"].temperature == 21.9  # arrival order kept
    row = h.client._stores.row("ZONA", "001", "", "TEMP_AMBIENTE")
    assert row is not None
    assert row.frame_at == t(1.5)  # received_at, not the replay time
    stats = h.client.stats
    assert (stats.frames_received, stats.buffered_max, stats.noop_updates) == (3, 3, 1)
    assert h.client.connection_state is ConnectionState.CONNECTED
    await closed_cleanly(h)


async def test_open_frames_batch_is_published_before_a_sync_starts() -> None:
    h = await connected()
    await advance_to(h, 70)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "23"))
    h.transport.set_value("ZONA", "001", "", "TEMP_AMBIENTE", "23")  # the device agrees
    resync = asyncio.create_task(h.client.resync())
    await h.clock.settle()
    # the open batch was flushed at the start of the sync (old stores), not 100 ms later
    assert h.reasons() == ["sync", "frames"]
    assert h.updates[-1].at == t(70)
    await h.clock.advance(5)
    await resync
    assert h.reasons() == ["sync", "frames"]  # the resync found nothing new
    await closed_cleanly(h)


async def test_frames_during_a_resync_are_applied_after_the_replace() -> None:
    h = await connected()
    await advance_to(h, 100)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.settle()
    await h.clock.advance(1.0)  # inside the interface GET
    await h.push(termo("REHOM...TEMP_COM", "25"))
    assert h.reasons() == ["sync"]
    await h.clock.advance(5)
    await resync
    assert h.reasons() == ["sync", "frames"]
    frames = h.updates[-1]
    assert frames.changed == {"REHOM...TEMP_COM"}
    assert frames.at == t(100 + RESYNC_TAKES + 0.1)
    assert frames.state.plant.temperature_comfort == 25.0
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# resync
# ---------------------------------------------------------------------------


async def test_resync_keeps_the_socket_open_and_publishes_nothing_without_differences() -> None:
    h = await connected()
    await advance_to(h, 61)
    before = h.client.state
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    await resync
    assert h.transport.count("get_interface") == 2
    assert len(h.connector.attempts) == 1
    assert h.ws.close_calls == 0
    assert h.reasons() == ["sync"]  # invariant 4
    assert h.client.state is before
    assert h.client.stats.resyncs == 1
    # the socket is still live after the resync
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "20"))
    await h.clock.advance(0.2)
    assert h.reasons() == ["sync", "frames"]
    await closed_cleanly(h)


async def test_resync_with_differences_publishes_resync_with_the_changed_paths() -> None:
    h = await connected()
    h.transport.set_value("REHOM", "", "", "TEMP_COM", "24.5")
    h.transport.set_value("ZONA", "003", "", "TEMP_AMBIENTE", "19")
    h.transport.interface = [row for row in h.transport.interface if row["Key"] != "VER_SOFT"]
    h.transport.plant_conf["stagione"] = "0"
    await advance_to(h, 61)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    await resync
    update = h.updates[-1]
    assert update.reason is UpdateReason.RESYNC
    assert update.changed == {
        "REHOM...TEMP_COM",
        "ZONA.003..TEMP_AMBIENTE",
        "REHOM...VER_SOFT",
    }
    assert update.removed == {"REHOM...VER_SOFT"}
    assert update.conf_changed == {"stagione"}
    assert update.previous is h.updates[0].state
    assert update.state.plant.temperature_comfort == 24.5
    assert update.state.hub.controller_version is None
    assert update.state.synced_at == t(SYNC_DONE + 60 + RESYNC_TAKES)  # min interval first
    await closed_cleanly(h)


async def test_resync_waits_for_min_resync_interval() -> None:
    h = await connected()
    await advance_to(h, 10)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(40)
    assert not resync.done()
    assert h.transport.count("get_interface") == 1
    await advance_to(h, SYNC_DONE + 60 + 5)
    await resync
    (_first, second) = [when for name, when in h.transport.call_times if name == "get_interface"]
    assert second == t(SYNC_DONE + 60)
    await closed_cleanly(h)


async def test_concurrent_resync_requests_coalesce() -> None:
    h = await connected()
    await advance_to(h, 61)
    first = asyncio.create_task(h.client.resync())
    second = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    await asyncio.gather(first, second)
    assert h.transport.count("get_interface") == 2
    assert h.client.stats.resyncs == 1
    await closed_cleanly(h)


async def test_periodic_resync_every_resync_interval() -> None:
    h = await connected()
    await advance_to(h, 1300)
    times = [when for name, when in h.transport.call_times if name == "get_interface"]
    # each resync takes 2.7 s; the next one is due 600 s after the previous completed
    assert times == [t(0.1), t(SYNC_DONE + 600), t(SYNC_DONE + RESYNC_TAKES + 1200)]
    assert h.client.stats.resyncs == 2
    assert h.reasons() == ["sync"]
    assert h.ws.close_calls == 0
    await closed_cleanly(h)


async def test_failed_resync_keeps_the_stores_and_the_live_stream() -> None:
    h = await connected()
    before = h.client.state
    h.transport.set_value("REHOM", "", "", "TEMP_COM", "30")
    h.transport.fail("get_overrides", RehomConnectionError("GET /api/overrides/: down"))
    await advance_to(h, 61)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    with pytest.raises(RehomConnectionError):
        await resync
    assert h.client.state is before
    assert h.client._stores.value("REHOM", "", "", "TEMP_COM") == "24"
    assert h.client.connection_state is ConnectionState.CONNECTED  # a resync is not a fallback
    assert h.client.stats.resyncs == 0
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "20"))
    await h.clock.advance(0.2)
    assert h.reasons() == ["sync", "frames"]  # processing resumed
    await closed_cleanly(h)


async def test_bad_payload_fails_the_resync_with_a_response_error() -> None:
    h = await connected()
    h.transport.plant_conf = ["not", "a", "mapping"]
    await advance_to(h, 61)
    resync = asyncio.create_task(h.client.resync())
    await h.clock.advance(5)
    with pytest.raises(RehomResponseError, match="plant/conf"):
        await resync
    await closed_cleanly(h)


async def test_reconnect_within_min_interval_applies_frames_live_and_defers_the_resync() -> None:
    h = await connected()
    await advance_to(h, 10)
    first = h.ws
    first.end()
    await h.clock.settle()
    assert h.client.connection_state is ConnectionState.DEGRADED
    await advance_to(h, 11)  # backoff 1 s
    assert len(h.connector.connections) == 2
    assert h.client.connection_state is ConnectionState.CONNECTED
    await advance_to(h, 20)
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "22.5"))
    await h.clock.advance(0.1)
    assert h.updates[-1].reason is UpdateReason.FRAMES  # applied live, not buffered
    assert h.updates[-1].state.zones["001"].temperature == 22.5
    assert h.transport.count("get_interface") == 1
    await advance_to(h, SYNC_DONE + 60 + 5)
    times = [when for name, when in h.transport.call_times if name == "get_interface"]
    assert times[1] == t(SYNC_DONE + 60)  # last_sync_done + min_resync_interval
    assert h.client.stats.reconnects == 1
    assert h.client.stats.resyncs == 1
    assert first.closed
    await closed_cleanly(h)


async def test_buffer_overflow_during_a_sync_forces_a_reconnect() -> None:
    h = await connected(options=ClientOptions(max_buffered_frames=100))
    await advance_to(h, 70)  # past min_resync_interval: the resync starts at once
    first = h.ws
    resync = asyncio.create_task(h.client.resync())
    await h.clock.settle()
    await h.clock.advance(1.0)
    first.push(*(termo("ZONA.001..TEMP_AMBIENTE", str(20 + i / 10)) for i in range(101)))
    await h.clock.settle()
    assert first.closed
    assert h.client._processor.backlog == 0  # the queue was cleared
    assert h.client.stats.buffered_max == 101
    await h.clock.advance(5)
    await resync  # the resync itself still completes
    assert h.client.state.zones["001"].temperature == 21.5
    assert len(h.connector.connections) == 2
    # the reconnect asks for another resync, 60 s after the last one
    await h.clock.advance(70)
    assert h.transport.count("get_interface") == 3
    assert h.client.stats.frames_received == 101
    await closed_cleanly(h)


# ---------------------------------------------------------------------------
# no-ops, coalescing, forecast
# ---------------------------------------------------------------------------


async def test_semantically_equal_frames_never_notify() -> None:
    h = await connected()
    await h.push(
        termo("ZONA.002..TEMP_AMBIENTE", "22.0"),
        termo("REHOM...TEMP_COM", "24.00"),
        {"domotica": {"objects": []}},
    )
    await h.clock.advance(1)
    assert h.reasons() == ["sync"]
    stats = h.client.stats
    assert (stats.noop_updates, stats.frames_ignored, stats.frames_received) == (2, 1, 3)
    await closed_cleanly(h)


async def test_remove_then_update_within_a_second_never_notifies() -> None:
    h = await connected()
    await h.push(termo("METEO.0..VENTO", kind="remove"))
    await h.clock.advance(0.3)
    await h.push(termo("METEO.0..VENTO", "3.5,180"))
    await h.clock.advance(5)
    assert h.reasons() == ["sync"]
    assert h.client.stats.removes_coalesced == 1
    assert h.client.stats.removes_applied == 0
    await closed_cleanly(h)


async def test_a_lone_remove_is_applied_by_the_expiry_timer() -> None:
    h = await connected()
    await advance_to(h, 10)
    await h.push(termo("METEO.0..VENTO", kind="remove"))
    await h.clock.advance(0.99)
    assert h.reasons() == ["sync"]
    await h.clock.advance(0.2)
    update = h.updates[-1]
    assert update.reason is UpdateReason.FRAMES
    assert update.removed == {"METEO.0..VENTO"}
    assert update.changed == {"METEO.0..VENTO"}
    assert update.at == t(11.1)  # expired at +1.0, published after the batch window
    assert h.client.stats.removes_applied == 1
    await closed_cleanly(h)


async def test_meteo_data_updates_the_forecast_only() -> None:
    h = await connected()
    await h.push(termo("METEO_DATA...dt1790337600", '{"dt": 1790337600}'))
    await h.clock.advance(0.2)
    update = h.updates[-1]
    assert update.forecast_changed
    assert update.changed == frozenset()
    forecast = update.state.forecast
    assert forecast is not None
    assert len(forecast.entries) == 1
    assert list(h.client._stores.rows("METEO_DATA")) == []
    await closed_cleanly(h)


async def test_fetch_snapshot_order_timeouts_and_received_at() -> None:
    clock = VirtualClock(T0)
    transport = FakeTransport(clock)
    task = asyncio.create_task(
        fetch_snapshot(transport, clock, interface_timeout=15.0, request_timeout=10.0)
    )
    await clock.settle()
    await clock.advance(5)
    snap = await task
    assert transport.calls == [
        ("get_interface", 15.0),
        ("get_overrides", 10.0),
        ("get_plant_conf", 10.0),
        ("get_config", 10.0),
    ]
    assert snap.config_received_at == t(2.6)
    assert snap.config["LOCAL_TIME"] == "2026-09-25 12:00:02"
    assert snap.config["METEO_KEY"].startswith("<redacted")
    stores = StoreSet()
    stores.replace(snap, clock.utcnow())
    assert stores.value("UTEN", "", "", "someone") == "<redacted len=19>"
