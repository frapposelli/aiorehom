"""``aiorehom.logic.override``: the library override rule (stricter than the web UI)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

import pytest

from aiorehom.enums import Level, MasterMode, Season, ZoneSetp
from aiorehom.logic import OverrideRow, OverrideState, override_applicable, zone_override

from ._helpers import csv, override_row, with_slots

BOOST = with_slots(1, (24, 25), 2)
SET_AT = "2026-09-25 12:10:00"
EXPIRES = "2026-09-25 12:44:59"
INSIDE = datetime(2026, 9, 25, 12, 30)


@dataclass
class Case:
    rows: tuple[OverrideRow, ...] = (override_row("1", BOOST, SET_AT, EXPIRES),)
    kwargs: dict[str, Any] = field(
        default_factory=lambda: {
            "season": Season.SUMMER,
            "today_preset": "1",
            "mode": MasterMode.AUTO,
            "setp": ZoneSetp.UNSET,
            "is_crono": False,
            "now_local": INSIDE,
        }
    )

    def run(self, **changes: Any) -> OverrideState | None:
        return zone_override(self.rows, **{**self.kwargs, **changes})


def test_applies() -> None:
    state = Case().run()
    assert state is not None
    assert state.applies
    assert state.row.subuni == "1"
    assert state.program is not None
    assert state.program[24] is Level.PRE_COMFORT
    assert state.set_at == datetime(2026, 9, 25, 12, 10)
    assert state.expires_at == datetime(2026, 9, 25, 12, 44, 59)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": MasterMode.MANUAL},
        {"mode": MasterMode.OFF},
        {"mode": None},
        {"setp": ZoneSetp.COMFORT},
        {"setp": ZoneSetp.OFF},
        {"setp": None},
        {"is_crono": True},
        {"today_preset": "2"},  # today is bound to another preset
        {"today_preset": None},  # today unbound
        {"now_local": datetime(2026, 9, 25, 12, 9, 59)},
        {"now_local": datetime(2026, 9, 25, 12, 45, 0)},
        {"now_local": datetime(2026, 9, 26, 12, 30)},  # the same time tomorrow
    ],
)
def test_each_condition_falsified(changes: dict[str, Any]) -> None:
    state = Case().run(**changes)
    assert state is not None
    assert not state.applies


def test_inclusive_bounds() -> None:
    case = Case()
    at_start = case.run(now_local=datetime(2026, 9, 25, 12, 10))
    at_end = case.run(now_local=datetime(2026, 9, 25, 12, 44, 59))
    assert at_start is not None
    assert at_start.applies
    assert at_end is not None
    assert at_end.applies


def test_the_last_second_is_whole_and_sub_second_now_is_compared_exactly() -> None:
    """``Scadenza`` is inclusive as a whole second: ``set_at <= now < expires_at + 1 s``."""
    case = Case()
    inside_last_second = case.run(now_local=datetime(2026, 9, 25, 12, 44, 59, 999_999))
    assert inside_last_second is not None
    assert inside_last_second.applies
    before_start = case.run(now_local=datetime(2026, 9, 25, 12, 9, 59, 999_999))
    assert before_start is not None
    assert not before_start.applies
    after_end = case.run(now_local=datetime(2026, 9, 25, 12, 45, 0))
    assert after_end is not None
    assert not after_end.applies
    forever = Case(rows=(override_row("1", BOOST, SET_AT, "9999-12-31 23:59:59"),)).run(
        now_local=datetime(9999, 12, 31, 23, 59, 59, 999_999)
    )
    assert forever is not None
    assert forever.applies


@pytest.mark.parametrize(
    ("set_at", "expires_at"),
    [
        (None, EXPIRES),
        (SET_AT, None),
        ("garbage", EXPIRES),
        (SET_AT, "2026-09-25 12:44"),
        (SET_AT, "2026-09-25 12:44:59+02:00"),
    ],
)
def test_unparseable_timestamps(set_at: str | None, expires_at: str | None) -> None:
    state = Case(rows=(override_row("1", BOOST, set_at, expires_at),)).run()
    assert state is not None
    assert not state.applies


@pytest.mark.parametrize("value", ["", csv([3] * 47), "3,3,3"])
def test_invalid_program(value: str) -> None:
    state = Case(rows=(override_row("1", value, SET_AT, EXPIRES),)).run()
    assert state is not None
    assert state.program is None
    assert not state.applies


def test_long_program_is_truncated() -> None:
    state = Case(rows=(override_row("1", csv([3] * 50), SET_AT, EXPIRES),)).run()
    assert state is not None
    assert state.program == (Level.COMFORT,) * 48
    assert state.applies


def test_no_season_or_no_candidates() -> None:
    assert Case().run(season=None) is None
    assert Case().run(season=Season.WINTER) is None  # the row is PROG_GIORNO_ESTATE
    assert Case(rows=()).run() is None
    lock = OverrideRow("", "WEBSERVER", "1", None, None)
    assert Case(rows=(lock,)).run() is None


def test_tomorrow_row_does_not_apply_today() -> None:
    """A boost past midnight writes tomorrow's preset row; it is shown but does not apply."""
    tomorrow = override_row("2", BOOST, "2026-09-25 23:10:00", "2026-09-26 01:09:59")
    state = Case(rows=(tomorrow,)).run(now_local=datetime(2026, 9, 25, 23, 30))
    assert state is not None
    assert state.row is tomorrow
    assert not state.applies


def test_today_row_wins_over_later_expiry() -> None:
    today = override_row("1", BOOST, SET_AT, EXPIRES)
    other = override_row("2", BOOST, SET_AT, "2026-09-26 12:00:00")
    state = Case(rows=(other, today)).run()
    assert state is not None
    assert state.row is today


def test_fallback_latest_expiry_then_lowest_subuni() -> None:
    early = override_row("3", BOOST, SET_AT, "2026-09-25 13:00:00")
    late_10 = override_row("10", BOOST, SET_AT, "2026-09-25 14:00:00")
    late_2 = override_row("2", BOOST, SET_AT, "2026-09-25 14:00:00")
    no_expiry = override_row("0", BOOST, SET_AT, None)
    case = Case(rows=(early, no_expiry, late_10, late_2))
    state = case.run(today_preset="7")
    assert state is not None
    assert state.row is late_2  # numeric order: "2" < "10"
    only_unknown = Case(rows=(override_row("b", BOOST, None, None), no_expiry))
    state = only_unknown.run(today_preset=None)
    assert state is not None
    assert state.row is no_expiry  # integers before other strings


def test_duplicate_today_rows_last_wins() -> None:
    first = override_row("1", BOOST, SET_AT, EXPIRES)
    second = replace(first, value=csv([3] * 48))
    state = Case(rows=(first, second)).run()
    assert state is not None
    assert state.row is second


def test_override_applicable() -> None:
    assert override_applicable(mode=MasterMode.AUTO, setp=ZoneSetp.UNSET, is_crono=False)
    assert not override_applicable(mode=MasterMode.AUTO, setp=ZoneSetp.UNSET, is_crono=True)
    assert not override_applicable(mode=MasterMode.MANUAL, setp=ZoneSetp.UNSET, is_crono=False)
    assert not override_applicable(mode=MasterMode.AUTO, setp=ZoneSetp.OFF, is_crono=False)
