"""Controller heartbeat.

The controller sends a ``PROC...WATCHDOG_MASTER`` frame periodically; its
arrival (whatever the value) is the heartbeat.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .types import HeartbeatStatus

__all__ = ["heartbeat_status"]


def heartbeat_status(
    *,
    heartbeat_at: datetime | None,
    live_since: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> HeartbeatStatus:
    """Heartbeat health (all times aware UTC).

    * WS not live (``live_since is None``): ``(None, None)``.
    * The reference is the last heartbeat if it arrived since ``live_since``,
      else ``live_since``; the deadline is ``reference + stale_after``.
    * Past the deadline: ``(False, None)``.  Otherwise ``(True, deadline)`` if a
      heartbeat was seen since ``live_since``, else ``(None, deadline)``.
    """
    if live_since is None:
        return HeartbeatStatus(ok=None, deadline=None)
    seen = heartbeat_at is not None and heartbeat_at >= live_since
    reference = heartbeat_at if seen and heartbeat_at is not None else live_since
    deadline = reference + stale_after
    if now >= deadline:
        return HeartbeatStatus(ok=False, deadline=None)
    return HeartbeatStatus(ok=True if seen else None, deadline=deadline)
