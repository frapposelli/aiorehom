"""``aiorehom.logic.health``: controller heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aiorehom.logic import HeartbeatStatus, heartbeat_status

LIVE = datetime(2026, 9, 25, 10, 22, 6, tzinfo=UTC)
STALE = timedelta(seconds=180)


def _status(
    heartbeat_at: datetime | None, now: datetime, live: datetime | None = LIVE
) -> HeartbeatStatus:
    return heartbeat_status(heartbeat_at=heartbeat_at, live_since=live, now=now, stale_after=STALE)


def test_ws_not_live() -> None:
    assert _status(LIVE, LIVE, live=None) == HeartbeatStatus(ok=None, deadline=None)


def test_none_true_false_transitions() -> None:
    assert _status(None, LIVE + timedelta(seconds=10)) == HeartbeatStatus(None, LIVE + STALE)
    beat = LIVE + timedelta(seconds=30)
    assert _status(beat, beat) == HeartbeatStatus(True, beat + STALE)
    assert _status(beat, beat + STALE - timedelta(microseconds=1)) == HeartbeatStatus(
        True, beat + STALE
    )
    assert _status(beat, beat + STALE) == HeartbeatStatus(False, None)


def test_no_heartbeat_since_live_goes_stale() -> None:
    assert _status(None, LIVE + STALE) == HeartbeatStatus(False, None)


def test_heartbeat_older_than_live_since_is_ignored() -> None:
    old = LIVE - timedelta(seconds=5)
    assert _status(old, LIVE + timedelta(seconds=1)) == HeartbeatStatus(None, LIVE + STALE)


def test_heartbeat_at_live_since_counts() -> None:
    assert _status(LIVE, LIVE) == HeartbeatStatus(True, LIVE + STALE)
