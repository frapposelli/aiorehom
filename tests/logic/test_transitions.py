"""``aiorehom.logic.transitions``: the compat port and the level-change walker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from aiorehom.enums import Level, Season
from aiorehom.logic import (
    DEFAULT_HORIZON,
    NextTransitionCompat,
    ScheduleRow,
    next_level_change,
    next_transition_compat,
)

from ._helpers import constant, csv, prog_rows, runs

S = Season.SUMMER


def _compat(
    rows: tuple[ScheduleRow, ...], day: int, slot: int, season: str = "1"
) -> tuple[object, ...]:
    r = next_transition_compat(season, rows, day, slot)
    return (r.day, r.last_slot, r.wrap, r.running, r.slots_minus_one)


def test_compat_basic() -> None:
    rows = prog_rows(S, {"1": runs((1, 24), (3, 20), (1, 4))}, dict.fromkeys(range(7), "1"))
    assert _compat(rows, 5, 24) == (5, 43, False, True, 19)
    assert _compat(rows, 5, 45) == (6, 23, False, True, 26)  # through midnight into Saturday


def test_compat_not_running_and_wrap() -> None:
    rows = prog_rows(S, {"1": constant(2)}, dict.fromkeys(range(7), "1"))
    assert _compat(rows, 3, 10) == (3, 9, True, True, 335)
    unbound = prog_rows(S, {"1": constant(2)}, {0: "1"})
    assert _compat(unbound, 3, 10) == (None, None, None, False, None)
    assert next_transition_compat("1", unbound, 3, 10) == NextTransitionCompat(
        None, None, None, False, None
    )


def test_compat_stops_on_unbound_day() -> None:
    rows = prog_rows(S, {"1": constant(3)}, {6: "1"})
    assert _compat(rows, 6, 47) == (6, 47, False, True, 0)


def test_compat_short_program_undefined_slots() -> None:
    """JS reads past the end of a short array as ``undefined``, and ``undefined === undefined``."""
    rows = prog_rows(S, {"1": "1,1"}, {0: "1"})
    assert _compat(rows, 0, 0) == (0, 1, False, True, 1)
    assert _compat(rows, 0, 10) == (0, 47, False, True, 37)


def test_compat_raw_string_compare() -> None:
    rows = prog_rows(S, {"1": csv(["1.0"] + ["1"] * 47)}, {0: "1"})
    assert _compat(rows, 0, 0) == (0, 0, False, True, 0)


def test_compat_dangling_anywhere_raises() -> None:
    """``mapValues`` splits every value: a dangling binding off the walked path still throws."""
    rows = prog_rows(S, {"1": constant(1)}, {0: "1", 4: "9"})
    with pytest.raises(TypeError):
        next_transition_compat("1", rows, 0, 0)


def test_compat_season_uses_js_semantics() -> None:
    rows = prog_rows(S, {"1": runs((1, 24), (3, 24))}, dict.fromkeys(range(7), "1"))
    assert _compat(rows, 1, 0, season="0x1")[3] is True
    assert _compat(rows, 1, 0, season="0")[3] is False


@pytest.mark.parametrize(("day", "slot"), [(-1, 0), (7, 0), (0, -1), (0, 48)])
def test_compat_rejects_out_of_range(day: int, slot: int) -> None:
    with pytest.raises(ValueError, match="curr_day"):
        next_transition_compat("1", (), day, slot)


# ---------------------------------------------------------------------------
# next_level_change
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 25, 12, 7, 13)


def _step(at: datetime, before: Level, after: Level) -> Callable[[datetime], Level | None]:
    return lambda t: before if t < at else after


def test_current_none() -> None:
    assert next_level_change(lambda t: None, now_local=NOW) is None


def test_first_boundary_with_a_change() -> None:
    level_at = _step(datetime(2026, 9, 25, 14, 0), Level.ECONOMY, Level.COMFORT)
    assert next_level_change(level_at, now_local=NOW) == datetime(2026, 9, 25, 14, 0)


def test_constant_is_none() -> None:
    calls: list[datetime] = []

    def level_at(t: datetime) -> Level:
        calls.append(t)
        return Level.OFF

    assert next_level_change(level_at, now_local=NOW) is None
    assert len(calls) == 1 + 336
    assert calls[-1] == datetime(2026, 10, 2, 12, 0)  # slot_start(now) + 7 days
    assert timedelta(days=7) == DEFAULT_HORIZON


def test_extra_instants() -> None:
    change = datetime(2026, 9, 25, 12, 45, 0)
    level_at = _step(change, Level.PRE_COMFORT, Level.COMFORT)
    got = next_level_change(level_at, now_local=NOW, extra_instants=[change, change])
    assert got == change


def test_extra_instants_window() -> None:
    """Instants at ``now`` or beyond the horizon are ignored; ``now + horizon`` is included."""
    horizon = timedelta(hours=1)
    end = NOW + horizon
    level_at = _step(end, Level.OFF, Level.COMFORT)
    got = next_level_change(level_at, now_local=NOW, extra_instants=[NOW, end], horizon=horizon)
    assert got == end
    beyond = end + timedelta(seconds=1)
    level_at = _step(beyond, Level.OFF, Level.COMFORT)
    assert (
        next_level_change(level_at, now_local=NOW, extra_instants=[beyond], horizon=horizon) is None
    )


def test_change_to_none_counts() -> None:
    """An unbound day ahead is a change (the JS walk stops on it)."""
    midnight = datetime(2026, 9, 26)

    def level_at(t: datetime) -> Level | None:
        return Level.COMFORT if t < midnight else None

    assert next_level_change(level_at, now_local=NOW) == midnight


def test_custom_horizon_limits_boundaries() -> None:
    calls: list[datetime] = []

    def level_at(t: datetime) -> Level:
        calls.append(t)
        return Level.ECONOMY

    assert next_level_change(level_at, now_local=NOW, horizon=timedelta(hours=2)) is None
    assert len(calls) == 1 + 4
