"""``next_transition_compat`` vs ``getNextTransitionTime``, and the library walker cross-check.

(a) The compat port equals the JS output exactly.
(b) For canonical seasons, the library path (``week_programs`` + ``zone_effective``
    in AUTO/SCHEDULE) finds the same transition instant:
    ``slot_start(now) + (slots_minus_one + 1) x 30 min`` while running and not
    wrapping, else ``None``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from aiorehom.enums import Level, MasterMode, MasterSetPoint, ZoneSetp
from aiorehom.logic import (
    ZoneInputs,
    next_transition_compat,
    parse_season,
    slot_start,
    week_programs,
    zone_effective,
)

from ._vectors import cases, load, mismatches, report, schedule_rows

NAME = "next_transition.json"
DANGLING = "next_transition_dangling.json"
SUNDAY = datetime(2026, 9, 20)  # a Sunday: weekday_js == 0
TEMPERATURES = {Level.OFF: 7.0, Level.ECONOMY: 17.0, Level.PRE_COMFORT: 19.0, Level.COMFORT: 21.0}


def _now_local(day: int, slot: int) -> datetime:
    return SUNDAY + timedelta(days=day, minutes=30 * slot + 7, seconds=13)


def _library_inputs(case: dict[str, Any]) -> ZoneInputs:
    season = parse_season(case["season"])
    assert season is not None
    return ZoneInputs(
        mode=MasterMode.AUTO,
        set_point=MasterSetPoint.UNSET,
        season=season,
        is_crono=False,
        setp=ZoneSetp.UNSET,
        forced=None,
        forced_setpoint=None,
        offset_raw=None,
        calling=None,
        temperatures=TEMPERATURES,
        week=week_programs(schedule_rows(case["rows"]), (), season),
        override_rows=(),
        now_local=_now_local(case["day"], case["slot"]),
    )


def _check_compat(case: dict[str, Any]) -> str | None:
    result = next_transition_compat(
        case["season"], schedule_rows(case["rows"]), case["day"], case["slot"]
    )
    got = [result.day, result.last_slot, result.wrap, result.running, result.slots_minus_one]
    if got != case["out"]:
        where = f"season={case['season']!r} day={case['day']} slot={case['slot']}"
        return f"{where}: {got} != {case['out']}"
    return None


def _check_library(case: dict[str, Any]) -> str | None:
    if case["season"] not in ("0", "1"):
        return None
    _, _, wrap, running, slots_minus_one = case["out"]
    inputs = _library_inputs(case)
    effective = zone_effective(inputs)
    if running and not wrap:
        expected = slot_start(inputs.now_local) + (slots_minus_one + 1) * timedelta(minutes=30)
    else:
        expected = None
    if effective.next_change_local != expected:
        return f"day={case['day']} slot={case['slot']}: {effective.next_change_local} != {expected}"
    if (effective.level is not None) != running:
        return (
            f"day={case['day']} slot={case['slot']}: level {effective.level} vs running {running}"
        )
    return None


def test_meta() -> None:
    meta = load(NAME)["meta"]
    assert meta["function"] == "getNextTransitionTime"
    assert meta["count"] == len(cases(NAME)) >= 3000


def test_compat_matches_vendor_js() -> None:
    failures = mismatches(cases(NAME), _check_compat)
    assert not failures, report(failures)


def test_library_walker_agrees_with_vendor_js() -> None:
    checked = [case for case in cases(NAME) if case["season"] in ("0", "1")]
    assert len(checked) > 2500
    failures = mismatches(checked, _check_library)
    assert not failures, report(failures)


def test_vectors_cover_wrap_and_not_running() -> None:
    outs = [case["out"] for case in cases(NAME)]
    assert any(out[3] and out[2] for out in outs)  # wrap
    assert any(not out[3] for out in outs)  # today unbound
    assert any(out[3] and not out[2] and out[1] == 47 for out in outs)  # change at midnight


def test_dangling_meta() -> None:
    meta = load(DANGLING)["meta"]
    assert meta["count"] == len(cases(DANGLING)) == 20


@pytest.mark.parametrize("index", range(20))
def test_dangling_binding(index: int) -> None:
    """The JS throws on a dangling binding; the library treats it as unbound."""
    case = cases(DANGLING)[index]
    assert case["out"] == {"js_error": "TypeError"}
    rows = schedule_rows(case["rows"])
    with pytest.raises(TypeError):
        next_transition_compat(case["season"], rows, case["day"], case["slot"])
    effective = zone_effective(_library_inputs(case))
    assert effective.level is None
    assert effective.next_change_local is None
