"""Next transition: the web UI-compatible calculation and the library's walker.

:func:`next_transition_compat` reproduces the controller web UI's
next-transition calculation (a behaviour-compatible reimplementation, checked
against golden vectors).  The library itself uses :func:`next_level_change`,
which walks slot boundaries (plus extra instants such as override
start/expiry) on any level function.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta

from ..enums import Level
from .schedule import SLOT_DURATION, SLOTS_PER_DAY, program_map_compat, slot_start
from .types import NextTransitionCompat, ScheduleRow

__all__ = ["DEFAULT_HORIZON", "next_level_change", "next_transition_compat"]

#: How far :func:`next_level_change` looks ahead.
DEFAULT_HORIZON = timedelta(days=7)

_NOT_RUNNING = NextTransitionCompat(None, None, None, False, None)


def next_transition_compat(
    season_raw: str | int | None,
    rows: Sequence[ScheduleRow],
    curr_day: int,
    curr_slot: int,
) -> NextTransitionCompat:
    """The web UI's next transition (raw string compare of slot values).

    Walks forward from ``(curr_day, curr_slot)`` while the slot value stays the
    same, wrapping at 48 into the next day and stopping on an unbound day.
    As in the web UI, which splits every bound program of the week map before
    walking, any dangling binding in the map raises :class:`TypeError`,
    whether or not it is on the walked path.  Compat only: the library never
    calls this on store data.

    Raises :class:`ValueError` for ``curr_day`` outside 0..6 or ``curr_slot``
    outside 0..47, where the web UI may loop forever.
    """
    if not 0 <= curr_day <= 6 or not 0 <= curr_slot < SLOTS_PER_DAY:
        raise ValueError("curr_day must be 0..6 and curr_slot 0..47")
    schedule: dict[str, list[str]] = {}
    for key, value in program_map_compat(season_raw, rows).items():
        if value is None:
            raise TypeError("Cannot read properties of undefined (reading 'split')")
        schedule[key] = value.split(",")

    def at(day: int, index: int) -> str | None:  # None = past the end of the day
        items = schedule[str(day)]
        return items[index] if index < len(items) else None

    day, index = curr_day, curr_slot
    slices = 0
    wrap = False
    running = False
    while str(day) in schedule and at(day, index) == at(curr_day, curr_slot):
        running = True
        index += 1
        slices += 1
        if index == SLOTS_PER_DAY:
            index = 0
            day = (day + 1) % 7
        if day == curr_day and index == curr_slot:
            wrap = True
            break
    if not running:
        return _NOT_RUNNING
    return NextTransitionCompat(
        (day + 6) % 7 if index == 0 else day,
        (index + 47) % 48,
        wrap,
        True,
        slices - 1,
    )


def next_level_change(
    level_at: Callable[[datetime], Level | None],
    *,
    now_local: datetime,
    extra_instants: Iterable[datetime] = (),
    horizon: timedelta = DEFAULT_HORIZON,
) -> datetime | None:
    """First instant after ``now_local`` at which ``level_at`` differs from its value now.

    Candidates are the slot boundaries within ``horizon`` (336 for 7 days) plus
    every extra instant ``x`` with ``now_local < x <= now_local + horizon``,
    in ascending order.  ``None`` when the current level is ``None`` (the web
    UI's "not executing a program") or when nothing changes within the horizon
    (the web UI's ``wrap``).  All times are naive device-local wall time.
    """
    current = level_at(now_local)
    if current is None:
        return None
    start = slot_start(now_local)
    end = now_local + horizon
    candidates = {start + k * SLOT_DURATION for k in range(1, horizon // SLOT_DURATION + 1)}
    candidates.update(x for x in extra_instants if now_local < x <= end)
    for when in sorted(candidates):
        if level_at(when) != current:
            return when
    return None
