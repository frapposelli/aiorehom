"""``aiorehom.logic.schedule``."""

from __future__ import annotations

import math
from datetime import datetime

import pytest

from aiorehom.enums import Level, ScheduleSource, Season
from aiorehom.logic import (
    CRONO_PRESET,
    SLOT_DURATION,
    SLOTS_PER_DAY,
    ScheduleRow,
    js_unary_plus,
    next_slot_boundary,
    program_index_map_compat,
    program_map_compat,
    season_suffix,
    slot_of,
    slot_start,
    split_override_program,
    split_program,
    week_programs,
    weekday_js,
)

from ._helpers import constant, csv, prog_rows, runs, with_slots

S = Season.SUMMER
W = Season.WINTER


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hour", "minute", "second", "slot"),
    [(0, 0, 0, 0), (0, 29, 59, 0), (0, 30, 0, 1), (12, 7, 13, 24), (23, 59, 59, 47)],
)
def test_slot_of(hour: int, minute: int, second: int, slot: int) -> None:
    assert slot_of(datetime(2026, 9, 25, hour, minute, second)) == slot


def test_weekday_js() -> None:
    assert weekday_js(datetime(2026, 9, 20)) == 0  # Sunday
    assert weekday_js(datetime(2026, 9, 21)) == 1
    assert weekday_js(datetime(2026, 9, 25, 23, 59)) == 5  # Friday
    assert weekday_js(datetime(2026, 9, 26)) == 6  # Saturday


def test_slot_start_and_boundary() -> None:
    assert slot_start(datetime(2026, 9, 25, 12, 44, 59, 999)) == datetime(2026, 9, 25, 12, 30)
    assert slot_start(datetime(2026, 9, 25, 12, 0)) == datetime(2026, 9, 25, 12, 0)
    assert next_slot_boundary(datetime(2026, 9, 25, 12, 0)) == datetime(2026, 9, 25, 12, 30)
    # Midnight and month/year rollovers are plain naive arithmetic.
    assert next_slot_boundary(datetime(2026, 9, 26, 23, 45)) == datetime(2026, 9, 27, 0, 0)
    assert next_slot_boundary(datetime(2026, 12, 31, 23, 30, 1)) == datetime(2027, 1, 1)


def test_dst_days_are_naive_wall_time() -> None:
    """Europe/Rome DST changes (2026-03-29, 2026-10-25) do not affect naive slot maths."""
    spring = datetime(2026, 3, 29, 1, 45)
    assert next_slot_boundary(spring) == datetime(2026, 3, 29, 2, 0)  # a wall time that is skipped
    assert slot_of(datetime(2026, 3, 29, 2, 30)) == 5
    autumn = datetime(2026, 10, 25, 2, 40)
    assert next_slot_boundary(autumn) == datetime(2026, 10, 25, 3, 0)
    assert weekday_js(autumn) == 0


def test_constants_and_suffix() -> None:
    assert SLOTS_PER_DAY == 48
    assert SLOT_DURATION.total_seconds() == 1800
    assert CRONO_PRESET == "99"
    assert season_suffix(S) == "ESTATE"
    assert season_suffix(W) == "INVERNO"


# ---------------------------------------------------------------------------
# Program strings
# ---------------------------------------------------------------------------


def test_split_program_valid() -> None:
    program = split_program(runs((0, 12), (1, 12), (2, 12), (3, 12)))
    assert program is not None
    assert len(program) == 48
    assert program[0] is Level.OFF
    assert program[47] is Level.COMFORT


@pytest.mark.parametrize("raw", [None, "", csv([1] * 47), csv([1] * 49), "1"])
def test_split_program_rejects(raw: str | None) -> None:
    assert split_program(raw) is None


def test_split_program_invalid_slots() -> None:
    values: list[str] = ["1"] * 48
    values[0], values[1], values[2], values[3] = "x", "4", "", "-1"
    program = split_program(",".join(values))
    assert program is not None
    assert program[:4] == (None, None, None, None)
    assert program[4] is Level.ECONOMY


def test_split_program_parses_numerically() -> None:
    """Library deviation: ``"1.0"``/``" 1"`` parse as ECONOMY (the JS compares strictly)."""
    values = ["1.0", " 2 ", "3"] + ["0"] * 45
    program = split_program(",".join(values))
    assert program is not None
    assert program[:3] == (Level.ECONOMY, Level.PRE_COMFORT, Level.COMFORT)


def test_split_override_program() -> None:
    assert split_override_program(None) is None
    assert split_override_program("") is None
    assert split_override_program(csv([2] * 47)) is None
    exact = split_override_program(csv([2] * 48))
    assert exact == (Level.PRE_COMFORT,) * 48
    long = split_override_program(csv([3] * 48 + [0, 1]))
    assert long == (Level.COMFORT,) * 48


# ---------------------------------------------------------------------------
# JS unary plus and the compat maps
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", 1.0),
        ("01", 1.0),
        ("1.0", 1.0),
        (" 1 ", 1.0),
        ("\t1\n", 1.0),
        ("\u00a01\ufeff", 1.0),
        ("+1", 1.0),
        ("1.", 1.0),
        (".5", 0.5),
        ("1e0", 1.0),
        ("0.1E1", 1.0),
        ("0x1", 1.0),
        ("0X1f", 31.0),
        ("0o7", 7.0),
        ("0b11", 3.0),
        ("", 0.0),
        ("   ", 0.0),
        ("-0", -0.0),
        ("Infinity", math.inf),
        ("-Infinity", -math.inf),
        (1, 1.0),
        (0, 0.0),
    ],
)
def test_js_unary_plus_numbers(value: str | int, expected: float) -> None:
    assert js_unary_plus(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, "abc", "1_0", "1,0", "-0x1", "infinity", "inf", "nan", ".", "1e", "\u0661", "0x"],
)
def test_js_unary_plus_nan(value: str | None) -> None:
    assert math.isnan(js_unary_plus(value))


def _rows(*triples: tuple[str, str, str]) -> tuple[ScheduleRow, ...]:
    return tuple(ScheduleRow(s, k, v) for s, k, v in triples)


def test_program_index_map_compat() -> None:
    rows = _rows(
        ("0", "PROG_SETT_ESTATE", "1"),
        ("1", "PROG_SETT_INVERNO", "2"),
        ("0", "PROG_SETT_ESTATE", "3"),  # last wins
        ("00", "PROG_SETT_ESTATE", "4"),
        ("2", "PROG_SETT", "5"),
        ("3", "prog_sett_estate", "6"),
    )
    assert program_index_map_compat("1", rows) == {"0": "3", "00": "4"}
    assert program_index_map_compat(1, rows) == {"0": "3", "00": "4"}
    assert program_index_map_compat("01", rows) == {"0": "3", "00": "4"}
    for winter in ("0", 0, "", "2", None, "x"):
        assert program_index_map_compat(winter, rows) == {"1": "2"}


def test_program_map_compat() -> None:
    rows = _rows(
        ("0", "PROG_SETT_INVERNO", "1"),
        ("1", "PROG_SETT_INVERNO", "7"),  # dangling
        ("1", "PROG_GIORNO_INVERNO", "a"),
        ("1", "PROG_GIORNO_INVERNO", "b"),  # last wins
        ("7", "PROG_GIORNO_ESTATE", "c"),  # other season
    )
    assert program_map_compat("0", rows) == {"0": "b", "1": None}
    assert program_map_compat("1", rows) == {}


# ---------------------------------------------------------------------------
# week_programs (library policy)
# ---------------------------------------------------------------------------

P1 = runs((1, 24), (3, 20), (1, 4))
P2 = constant(2)


def test_week_programs_from_prog() -> None:
    rows = prog_rows(S, {"1": P1, "99": P2}, {0: "1", 3: "1"})
    week = week_programs(rows, (), S)
    assert week.source is ScheduleSource.PROG
    assert dict(week.index_map) == {0: "1", 3: "1"}
    assert week.program_map[0] == split_program(P1)
    assert week.crono == (Level.PRE_COMFORT,) * 48


def test_prog_preferred_over_mirror() -> None:
    prog = prog_rows(S, {"1": P1}, {0: "1"})
    mirror = prog_rows(S, {"1": P2}, {0: "1", 1: "1"})
    week = week_programs(prog, mirror, S)
    assert week.source is ScheduleSource.PROG
    assert dict(week.index_map) == {0: "1"}
    assert week.program_map[0] == split_program(P1)


def test_mirror_used_only_when_prog_has_no_season_keys() -> None:
    prog = prog_rows(W, {"1": P1}, {0: "1"})  # other season only
    mirror = prog_rows(S, {"2": P2}, {5: "2"})
    week = week_programs(prog, mirror, S)
    assert week.source is ScheduleSource.ZONA_MIRROR
    assert dict(week.index_map) == {5: "2"}
    assert week.program_map[5] == (Level.PRE_COMFORT,) * 48


def test_sources_are_never_mixed() -> None:
    prog = prog_rows(S, {}, {0: "1"})  # binding only: the program lives in the mirror
    mirror = prog_rows(S, {"1": P1}, {})
    week = week_programs(prog, mirror, S)
    assert week.source is ScheduleSource.PROG
    assert week.program_map[0] is None  # dangling in PROG; never filled from the mirror


def test_no_rows() -> None:
    week = week_programs((), prog_rows(W, {"1": P1}, {0: "1"}), S)
    assert week.source is ScheduleSource.NONE
    assert dict(week.index_map) == {}
    assert dict(week.program_map) == {}
    assert week.crono is None


def test_sunday_zero_binds_but_double_zero_does_not() -> None:
    rows = prog_rows(S, {"1": P1, "2": P2}, {"00": "2", "0": "1", "7": "2", "-1": "2"})
    week = week_programs(rows, (), S)
    assert dict(week.index_map) == {0: "1"}


def test_preset_ids_are_exact_strings() -> None:
    rows = prog_rows(S, {"1": P1, "01": P2}, {0: "01", 1: "1.0"})
    week = week_programs(rows, (), S)
    assert week.program_map[0] == (Level.PRE_COMFORT,) * 48
    assert week.program_map[1] is None  # "1.0" does not resolve preset "1"


def test_dangling_and_malformed_programs() -> None:
    rows = prog_rows(S, {"1": csv([1] * 47), "2": P2}, {0: "1", 1: "3", 2: "2"})
    week = week_programs(rows, (), S)
    assert week.program_map[0] is None  # wrong length
    assert week.program_map[1] is None  # dangling
    assert week.program_map[2] is not None


def test_crono_preset_99() -> None:
    crono = with_slots(0, range(10, 20), 3)
    week = week_programs(prog_rows(W, {"99": crono}, {}), (), W)
    assert week.crono == split_program(crono)
    assert dict(week.index_map) == {}
    missing = week_programs(prog_rows(W, {"1": P1}, {}), (), W)
    assert missing.crono is None
    malformed = week_programs(prog_rows(W, {"99": "3,3"}, {}), (), W)
    assert malformed.crono is None


def test_week_maps_are_read_only() -> None:
    week = week_programs(prog_rows(S, {"1": P1}, {0: "1"}), (), S)
    with pytest.raises(TypeError):
        week.index_map[1] = "1"  # type: ignore[index]
