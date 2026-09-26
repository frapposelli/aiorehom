"""RehomClient edge cases: bugs surfacing in background loops, races, defensive paths."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta, tzinfo

import aiohttp
import pytest

from aiorehom.clock import DeviceClock
from aiorehom.enums import ConnectionState, UpdateReason
from aiorehom.exceptions import ForbiddenRequestError, RehomNotReadyError
from aiorehom.state import ChangeSet
from aiorehom.websocket import ReceiveOnlyConnection

from .sync_fakes import T0, FakeWsConnection, Harness, termo

SYNC_DONE = 2.7


def t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


async def connected() -> Harness:
    h = Harness()
    await h.connect()
    return h


async def test_forbidden_request_in_a_resync_is_raised_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = await connected()
    h.transport.fail("get_interface", ForbiddenRequestError("GET /api/x/ refused: bug (test)"))
    await h.clock.advance_to(t(70))
    with caplog.at_level(logging.ERROR, logger="aiorehom.sync"):
        resync = asyncio.create_task(h.client.resync())
        await h.clock.advance(5)
        with pytest.raises(ForbiddenRequestError):
            await resync
    assert "background task" in caplog.text  # the sync loop stopped loudly
    await h.close()
    assert h.clock.pending_sleepers == 0


@pytest.mark.parametrize("method", ["get_alive", "get_config"])
async def test_forbidden_request_in_a_poll_stops_that_loop_loudly(
    caplog: pytest.LogCaptureFixture, method: str
) -> None:
    h = await connected()
    h.transport.fail(method, ForbiddenRequestError("refused: bug (test)"))
    with caplog.at_level(logging.ERROR, logger="aiorehom.sync"):
        await h.clock.advance(400)
    assert "failed" in caplog.text
    await h.close()


async def test_a_bug_in_a_sync_is_logged_and_does_not_stop_later_syncs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = await connected()
    h.transport.fail("get_overrides", ValueError("transport bug (test)"))
    await h.clock.advance_to(t(70))
    with caplog.at_level(logging.ERROR, logger="aiorehom.client"):
        resync = asyncio.create_task(h.client.resync())
        await h.clock.advance(5)
        with pytest.raises(ValueError, match="transport bug"):
            await resync
    assert "resync failed" in caplog.text
    stats = h.client.stats
    assert (stats.sync_failures, stats.sync_failure_streak) == (1, 1)
    assert stats.last_sync_error == "ValueError"
    # the failed resync is retried by itself after min_resync_interval
    await h.clock.advance(60)
    stats = h.client.stats
    assert stats.resyncs == 1
    assert (stats.sync_failures, stats.sync_failure_streak, stats.last_sync_error) == (1, 0, None)
    again = asyncio.create_task(h.client.resync())
    await h.clock.advance(65)
    await again
    assert h.client.stats.resyncs == 2
    await h.close()


async def test_a_cancelled_resync_caller_does_not_break_the_others() -> None:
    h = await connected()
    await h.clock.advance_to(t(10))
    first = asyncio.create_task(h.client.resync())
    second = asyncio.create_task(h.client.resync())
    await h.clock.settle()
    first.cancel()
    await h.clock.advance(60)
    await second
    assert first.cancelled()
    await h.close()


async def test_defensive_paths_of_publish_tick_and_sync_requests() -> None:
    h = Harness()
    client = h.client
    assert client._publish(UpdateReason.CLOCK, ChangeSet()) is False  # no device clock yet
    client._on_tick()  # no state: nothing to do
    await client._wait_min_interval()  # no sync yet: returns at once
    await h.connect()
    client._request_sync(UpdateReason.FALLBACK)
    client._request_sync(UpdateReason.FALLBACK)
    assert client._sync_pending is UpdateReason.FALLBACK
    client._request_sync(UpdateReason.RESYNC)
    client._request_sync(UpdateReason.FALLBACK)
    assert client._sync_pending is UpdateReason.RESYNC
    client._sync_pending = None
    client._sync_wakeup.clear()
    client._on_fallback_due()  # the WS is live: no fallback
    assert client._sync_pending is None
    await h.close()
    assert client._safe_publish(UpdateReason.CLOCK, ChangeSet()) is False
    client._request_sync(UpdateReason.RESYNC)
    assert client._sync_pending is None
    client._refresh()  # closed: no publish
    assert h.reasons() == ["sync"]


async def test_a_failing_websocket_close_is_swallowed() -> None:
    h = Harness()

    class BadClose(FakeWsConnection):
        async def close(self) -> None:
            await super().close()
            raise aiohttp.ClientConnectionError("close failed (test)")

    bad = BadClose()

    async def connector() -> FakeWsConnection:
        h.connector.connections.append(bad)
        return bad

    h.client._ws_connector = connector
    await h.connect()
    bad.end()
    await h.clock.advance(0.5)
    assert h.client.connection_state is ConnectionState.DEGRADED
    await h.close()


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


async def test_history_edge_cases() -> None:
    h = await connected()
    with pytest.raises(ValueError, match="aware"):
        await h.client.get_history(
            "TEMP_ESTERNA", start=datetime(2026, 9, 25, tzinfo=_NoOffset()), end=T0
        )
    h.transport.latency["get_history"] = 6.0  # slower than the chunk spacing
    h.transport.history = lambda *_a: [
        {"rowid": 1, "Valore": 1, "Tempo": "0001-01-01 00:00:00"},  # before UTC can represent
        {"rowid": 2, "Valore": 2, "Tempo": "2026-09-24 12:00:00"},
    ]
    task = asyncio.create_task(
        h.client.get_history("TEMP_ESTERNA", start=T0 - timedelta(hours=30), end=T0)
    )
    await h.clock.advance(30)
    samples = await task
    assert [s.rowid for s in samples] == [2]
    starts = [call[4] for call in h.transport.history_calls]
    assert starts[1] - starts[0] == pytest.approx(6.0 + 5.0)  # spaced from the previous end
    await h.close()


async def test_receive_only_connection_skips_control_messages() -> None:
    messages = [
        aiohttp.WSMessage(aiohttp.WSMsgType.PONG, b"", None),
        aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, '{"domain": "bus", "key": "k"}', None),
        aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1000, None),
    ]

    class Scripted:
        closed = False
        close_code = 1000

        async def receive(self) -> aiohttp.WSMessage:
            return messages.pop(0)

        async def close(self) -> bool:
            return True

    class Session:
        async def close(self) -> None:
            return None

    conn = ReceiveOnlyConnection(Session(), Scripted())  # type: ignore[arg-type]
    assert await conn.receive() == {"domain": "bus", "key": "k"}
    assert await conn.receive() is None
    assert conn.stats.frames == 1


def test_device_clock_survives_an_unrepresentable_local_time() -> None:
    clock = DeviceClock.from_config(
        {"TIMEZONE": "Europe/Rome", "LOCAL_TIME": "0001-01-01 00:00:00"}, T0
    )
    assert clock.skew == timedelta(0)
    assert clock.tz_name == "Europe/Rome"
    assert datetime.now(UTC).tzinfo is UTC  # sanity: the test itself runs on real UTC


async def test_frames_after_close_are_not_published() -> None:
    h = await connected()
    await h.push(termo("ZONA.001..TEMP_AMBIENTE", "19"))
    await h.close()
    await h.clock.advance(1)
    assert h.reasons() == ["sync"]  # the open batch was discarded
    with pytest.raises(RehomNotReadyError):
        await h.client.resync()
