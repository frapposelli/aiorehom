"""``aiorehom.logic.zone``: effective level, target, mode/preset/source, action, next change."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

import aiorehom.logic.zone as zone_module
from aiorehom.enums import (
    ControlSource,
    HvacAction,
    Level,
    MasterMode,
    MasterSetPoint,
    ProgramSource,
    Season,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)
from aiorehom.logic import (
    HUMIDITY_NO_SENSOR,
    zone_effective,
    zone_humidity,
    zone_hvac_action,
    zone_level_at,
)

from ._helpers import (
    TEMPERATURES,
    constant,
    every_day,
    override_row,
    runs,
    week,
    with_slots,
    zone_inputs,
)

M = MasterMode
SP = MasterSetPoint
Z = ZoneSetp
CS = ControlSource
S = Season.SUMMER
BOOST = with_slots(1, (24, 25), 2)  # preset 1 with slots 24-25 = PRE_COMFORT
SET_AT = "2026-09-25 12:10:00"
EXPIRES = "2026-09-25 12:44:59"


def _rome() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Rome")
    except ZoneInfoNotFoundError:  # pragma: no cover - depends on the host tz database
        pytest.skip("Europe/Rome not in the tz database")


# ---------------------------------------------------------------------------
# mode / preset / control source table
# ---------------------------------------------------------------------------

BOOSTED = {
    "override_rows": (override_row("1", BOOST, SET_AT, EXPIRES),),
    "now_local": datetime(2026, 9, 25, 12, 30),
}


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"setp": Z.PROBE_OFF}, (ZoneMode.OFF, ZonePreset.NONE, CS.PROBE)),
        ({"setp": Z.PROBE_OFF, "mode": M.MANUAL}, (ZoneMode.OFF, ZonePreset.NONE, CS.PROBE)),
        ({"mode": M.OFF}, (ZoneMode.OFF, ZonePreset.NONE, CS.HOUSE)),
        (
            {"mode": M.MANUAL, "set_point": SP.ECONOMY},
            (ZoneMode.MANUAL, ZonePreset.ECONOMY, CS.HOUSE),
        ),
        (
            {"mode": M.MANUAL, "set_point": SP.PRE_COMFORT},
            (ZoneMode.MANUAL, ZonePreset.PRE_COMFORT, CS.HOUSE),
        ),
        (
            {"mode": M.MANUAL, "set_point": SP.COMFORT, "setp": Z.COMFORT},
            (ZoneMode.MANUAL, ZonePreset.COMFORT, CS.HOUSE),
        ),
        ({"is_crono": True}, (ZoneMode.AUTO, ZonePreset.NONE, CS.CRONO)),
        ({"is_crono": True, "setp": Z.COMFORT}, (ZoneMode.AUTO, ZonePreset.NONE, CS.CRONO)),
        (BOOSTED, (ZoneMode.AUTO, ZonePreset.TEMPORARY_COMFORT, CS.OVERRIDE)),
        ({}, (ZoneMode.AUTO, ZonePreset.NONE, CS.SCHEDULE)),
        ({"setp": Z.OFF}, (ZoneMode.OFF, ZonePreset.NONE, CS.ZONE)),
        ({"setp": Z.ECONOMY}, (ZoneMode.MANUAL, ZonePreset.ECONOMY, CS.ZONE)),
        ({"setp": Z.PRE_COMFORT}, (ZoneMode.MANUAL, ZonePreset.PRE_COMFORT, CS.ZONE)),
        ({"setp": Z.COMFORT}, (ZoneMode.MANUAL, ZonePreset.COMFORT, CS.ZONE)),
        ({"mode": M.MANUAL, "set_point": SP.UNSET}, (None, None, None)),
        ({"mode": None}, (None, None, None)),
        ({"setp": None}, (None, None, None)),
        ({"forced": True}, (ZoneMode.AUTO, ZonePreset.NONE, CS.EXTERNAL)),
        ({"forced": True, "mode": M.OFF}, (ZoneMode.OFF, ZonePreset.NONE, CS.EXTERNAL)),
        ({"forced": True, "setp": None}, (None, None, None)),
        ({"forced": None, "setp": Z.OFF}, (ZoneMode.OFF, ZonePreset.NONE, CS.ZONE)),
    ],
)
def test_mode_preset_source_table(changes: dict[str, Any], expected: tuple[object, ...]) -> None:
    eff = zone_effective(zone_inputs(**changes))
    assert (eff.mode, eff.preset, eff.control_source) == expected


def test_override_state_exposed() -> None:
    eff = zone_effective(zone_inputs(**BOOSTED))
    assert eff.override is not None
    assert eff.override.applies
    assert eff.level is Level.PRE_COMFORT
    assert eff.target == 26.0
    assert eff.source is ProgramSource.SCHEDULE
    assert eff.running is not None
    assert eff.running[24] is Level.COMFORT  # the base program is kept in `running`


# ---------------------------------------------------------------------------
# Level, base, offset and target
# ---------------------------------------------------------------------------


def test_fixture_zone_002_target() -> None:
    """MANUAL COMFORT with DELTA_SETP_CORRENTE -1: 24 + trunc(-1) = 23.0."""
    eff = zone_effective(zone_inputs(mode=M.MANUAL, set_point=SP.COMFORT, offset_raw="-1"))
    assert eff.level is Level.COMFORT
    assert eff.base == 24.0
    assert eff.offset == -1.0
    assert eff.target == 23.0
    assert eff.source is ProgramSource.HOUSE_MANUAL
    assert (eff.weekday, eff.slot) == (5, 24)


@pytest.mark.parametrize(
    ("offset_raw", "offset", "target"),
    [
        ("-1.5", -1.5, 23.0),
        ("1.9", 1.9, 25.0),
        ("-0.9", -0.9, 24.0),
        ("3", 3.0, 27.0),
        ("-1.0", -1.0, 23.0),
        (None, None, 24.0),
        ("", None, 24.0),
        (" ", None, 24.0),
        ("abc", None, None),
        ("9" * 400, float("inf"), None),
    ],
)
def test_offset_truncation(
    offset_raw: str | None, offset: float | None, target: float | None
) -> None:
    eff = zone_effective(zone_inputs(offset_raw=offset_raw))
    assert eff.level is Level.COMFORT
    assert eff.offset == offset
    assert eff.target == target


def test_target_is_rounded() -> None:
    temps = {**TEMPERATURES, Level.COMFORT: 24.1}
    eff = zone_effective(zone_inputs(temperatures=temps, offset_raw="-1"))
    assert eff.target == 23.1


def test_forced_setpoint() -> None:
    eff = zone_effective(zone_inputs(forced=True, forced_setpoint=21.5, offset_raw="1"))
    assert eff.base == 21.5
    assert eff.target == 22.5
    assert eff.control_source is CS.EXTERNAL
    missing = zone_effective(zone_inputs(forced=True, forced_setpoint=None))
    assert missing.base is None
    assert missing.target is None
    not_forced = zone_effective(zone_inputs(forced=False, forced_setpoint=21.5))
    assert not_forced.base == 24.0


def test_level_none_means_no_target() -> None:
    eff = zone_effective(
        zone_inputs(mode=M.MANUAL, set_point=SP.UNSET, forced=True, forced_setpoint=20.0)
    )
    assert eff.level is None
    assert eff.base is None
    assert eff.target is None


def test_missing_level_temperature() -> None:
    temps = {**TEMPERATURES, Level.COMFORT: None}
    eff = zone_effective(zone_inputs(temperatures=temps))
    assert eff.base is None
    assert eff.target is None
    assert zone_effective(zone_inputs(temperatures={})).target is None


def test_probe_off_uses_off_temperature() -> None:
    eff = zone_effective(zone_inputs(setp=Z.PROBE_OFF))
    assert eff.level is Level.OFF
    assert eff.target == 38.0


def test_unknown_season_has_no_schedule() -> None:
    eff = zone_effective(zone_inputs(season=None, week=None))
    assert eff.source is ProgramSource.SCHEDULE
    assert eff.level is None
    assert eff.target is None
    assert eff.next_change_local is None
    crono = zone_effective(zone_inputs(season=None, week=None, is_crono=True))
    assert crono.level is None
    manual = zone_effective(zone_inputs(season=None, week=None, setp=Z.ECONOMY))
    assert manual.level is Level.ECONOMY  # zone manual needs no schedule


def test_unbound_or_dangling_today() -> None:
    unbound = week(S, {"1": constant(3)}, {0: "1"})
    eff = zone_effective(zone_inputs(week=unbound))
    assert eff.level is None
    dangling = week(S, {"1": constant(3)}, {5: "7"})
    assert zone_effective(zone_inputs(week=dangling)).level is None


def test_invalid_slot_value() -> None:
    bad = week(S, {"1": with_slots(3, (24,), 9)}, every_day("1"))
    eff = zone_effective(zone_inputs(week=bad))
    assert eff.level is None
    assert eff.target is None


# ---------------------------------------------------------------------------
# HVAC action and humidity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("calling", "season", "mode", "action"),
    [
        (None, S, ZoneMode.AUTO, None),
        (True, Season.WINTER, ZoneMode.AUTO, HvacAction.HEATING),
        (True, S, ZoneMode.MANUAL, HvacAction.COOLING),
        (True, None, ZoneMode.AUTO, None),
        (True, S, ZoneMode.OFF, HvacAction.COOLING),
        (False, S, ZoneMode.OFF, HvacAction.OFF),
        (False, S, ZoneMode.AUTO, HvacAction.IDLE),
        (False, None, None, HvacAction.IDLE),
    ],
)
def test_hvac_action(
    calling: bool | None, season: Season | None, mode: ZoneMode | None, action: HvacAction | None
) -> None:
    assert zone_hvac_action(calling=calling, season=season, mode=mode) is action


def test_hvac_action_in_effective() -> None:
    assert zone_effective(zone_inputs(calling=True)).hvac_action is HvacAction.COOLING
    assert zone_effective(zone_inputs(calling=False, mode=M.OFF)).hvac_action is HvacAction.OFF


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("53", (53.0, True)),
        ("0", (0.0, True)),
        ("255", (None, False)),
        ("255.0", (None, False)),
        (None, (None, None)),
        ("", (None, None)),
        ("NaN", (None, None)),
        ("9" * 400, (None, None)),
    ],
)
def test_zone_humidity(raw: str | None, expected: tuple[float | None, bool | None]) -> None:
    assert HUMIDITY_NO_SENSOR == 255.0
    assert zone_humidity(raw) == expected


# ---------------------------------------------------------------------------
# Next change
# ---------------------------------------------------------------------------


def test_next_change_schedule() -> None:
    """Preset 1 has slots 24-43 = COMFORT: from 12:07 the next change is 22:00."""
    eff = zone_effective(zone_inputs())
    assert eff.level is Level.COMFORT
    assert eff.next_change_local == datetime(2026, 9, 25, 22, 0)


def test_next_change_override_expiry_plus_one_second() -> None:
    inputs = zone_inputs(**{**BOOSTED, "now_local": datetime(2026, 9, 25, 12, 44, 59)})
    eff = zone_effective(inputs)
    assert eff.control_source is CS.OVERRIDE
    assert eff.next_change_local == datetime(2026, 9, 25, 12, 45, 0)
    after = zone_effective(
        zone_inputs(**{**BOOSTED, "now_local": datetime(2026, 9, 25, 12, 45, 1)})
    )
    assert after.control_source is CS.SCHEDULE
    assert after.level is Level.COMFORT
    assert after.target == 24.0


def test_next_change_future_set_at() -> None:
    inputs = zone_inputs(**{**BOOSTED, "now_local": datetime(2026, 9, 25, 12, 0, 30)})
    eff = zone_effective(inputs)
    assert eff.control_source is CS.SCHEDULE
    assert eff.override is not None
    assert not eff.override.applies
    assert eff.next_change_local == datetime(2026, 9, 25, 12, 10)


def test_next_change_crono() -> None:
    crono = week(S, {"99": with_slots(2, range(26, 30), 3), "1": constant(0)}, every_day("1"))
    eff = zone_effective(zone_inputs(is_crono=True, week=crono))
    assert eff.level is Level.PRE_COMFORT
    assert eff.next_change_local == datetime(2026, 9, 25, 13, 0)
    flat = week(S, {"99": constant(2)}, {})
    assert zone_effective(zone_inputs(is_crono=True, week=flat)).next_change_local is None
    missing = zone_effective(zone_inputs(is_crono=True, week=week(S, {}, {})))
    assert missing.level is Level.OFF  # zeros default
    assert missing.next_change_local is None


def test_next_change_unbound_tomorrow() -> None:
    friday_only = week(S, {"1": constant(3)}, {5: "1"})
    eff = zone_effective(zone_inputs(week=friday_only))
    assert eff.next_change_local == datetime(2026, 9, 26, 0, 0)


def test_next_change_whole_week_constant() -> None:
    flat = week(S, {"1": constant(3)}, every_day("1"))
    assert zone_effective(zone_inputs(week=flat)).next_change_local is None


def test_next_change_midnight_weekday_wrap() -> None:
    """Saturday (6) ends in COMFORT; Sunday (0) starts in ECONOMY."""
    presets = {"1": constant(3), "2": runs((1, 16), (3, 32))}
    bindings: dict[int | str, str] = {**every_day("1"), 0: "2"}
    inputs = zone_inputs(week=week(S, presets, bindings), now_local=datetime(2026, 9, 26, 23, 40))
    assert zone_effective(inputs).next_change_local == datetime(2026, 9, 27, 0, 0)


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": M.MANUAL, "set_point": SP.COMFORT},
        {"mode": M.OFF},
        {"setp": Z.COMFORT},
        {"setp": Z.PROBE_OFF},
        {"mode": None},
    ],
)
def test_next_change_shortcut(changes: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("the walker must not run")

    monkeypatch.setattr(zone_module, "next_level_change", fail)
    assert zone_effective(zone_inputs(**changes)).next_change_local is None


def test_zone_level_at_matches_effective() -> None:
    inputs = zone_inputs(**BOOSTED)
    assert zone_level_at(inputs, inputs.now_local) is zone_effective(inputs).level
    assert zone_level_at(inputs, datetime(2026, 9, 25, 23, 0)) is Level.ECONOMY
    assert zone_level_at(inputs, datetime(2026, 9, 25, 12, 20)) is Level.PRE_COMFORT


def test_dst_spring_forward_in_rome() -> None:
    """Naive wall time across 2026-03-29 (02:00 -> 03:00 CEST); UTC is the caller's job."""
    rome = _rome()
    presets = {"1": constant(1), "2": runs((1, 4), (3, 44))}  # Sunday: COMFORT from 02:00
    bindings: dict[int | str, str] = {**every_day("1"), 0: "2"}
    inputs = zone_inputs(week=week(S, presets, bindings), now_local=datetime(2026, 3, 28, 23, 50))
    change = zone_effective(inputs).next_change_local
    assert change == datetime(2026, 3, 29, 2, 0)  # a skipped wall time
    assert change.replace(tzinfo=rome).astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_dst_fall_back_in_rome() -> None:
    """On 2026-10-25 the 02:00-03:00 wall hour repeats; slots are still wall-time slots."""
    rome = _rome()
    presets = {"1": constant(1), "2": runs((1, 6), (3, 42))}  # Sunday: COMFORT from 03:00
    bindings: dict[int | str, str] = {**every_day("1"), 0: "2"}
    now = datetime(2026, 10, 25, 2, 15)
    inputs = zone_inputs(week=week(S, presets, bindings), now_local=now)
    change = zone_effective(inputs).next_change_local
    assert change == datetime(2026, 10, 25, 3, 0)
    assert change.replace(tzinfo=rome).astimezone(UTC) == datetime(2026, 10, 25, 2, 0, tzinfo=UTC)


def test_next_change_with_half_parsed_override_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows with one unparseable timestamp still contribute their other instant."""
    rows = (
        override_row("1", BOOST, "garbage", "2026-09-25 12:20:00"),
        override_row("2", BOOST, "2026-09-25 12:15:00", None),
    )
    calls: list[datetime] = []

    def spy(level_at: Any, *, now_local: datetime, extra_instants: Any) -> None:
        calls.extend(extra_instants)

    monkeypatch.setattr(zone_module, "next_level_change", spy)
    zone_effective(zone_inputs(override_rows=rows, now_local=datetime(2026, 9, 25, 12, 0)))
    assert calls == [datetime(2026, 9, 25, 12, 20, 1), datetime(2026, 9, 25, 12, 15)]
