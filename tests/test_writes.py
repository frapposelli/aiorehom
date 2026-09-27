"""Write planning against the live-capture fixture (pure; no I/O)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aiorehom.enums import MasterPreset, VmcMode, ZoneSetp
from aiorehom.exceptions import RehomWriteRefusedError
from aiorehom.models import RehomState
from aiorehom.transport import INTERFACE_WRITE_PATH, OVERRIDES_WRITE_PATH, check_write_body
from aiorehom.writes import (
    format_temperature,
    plan_comfort_temperature,
    plan_house_preset,
    plan_predictive,
    plan_temporary_comfort,
    plan_vmc_fan,
    plan_vmc_mode,
    plan_zone_mode,
    plan_zone_offset,
)

from .test_builder import build, override_row, stores

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"
NOON = datetime(2026, 9, 25, 10, 22, tzinfo=UTC)  # 12:22 local: every zone in scheduled comfort
EVENING = datetime(2026, 9, 25, 20, 5, tzinfo=UTC)  # 22:05 local: schedules at OFF


def _load(name: str) -> Any:
    return json.loads((FIXTURE / name).read_text())


def state(
    changes: dict[str, str] | None = None,
    *,
    at: datetime = NOON,
    overrides: list[dict[str, Any]] | None = None,
    extra_rows: list[dict[str, Any]] | None = None,
) -> RehomState:
    rows = [dict(r) for r in _load("interface.json")]
    for row in rows:
        path = f"{row['Gruppo']}.{row['Unita']}.{row['SubUni']}.{row['Key']}"
        if changes and path in changes:
            row["Valore"] = changes[path]
    rows.extend(extra_rows or [])
    config = dict(_load("config.json"))
    config["LOCAL_TIME"] = (
        at.astimezone(ZoneInfo("Europe/Rome")).replace(tzinfo=None).isoformat(timespec="seconds")
    )
    store = stores(
        rows, overrides=overrides, plant_conf=_load("plant_conf.json"), config=config, at=at
    )
    return build(store, now=at)


AUTO = {"REHOM...MODO": "2", "REHOM...SET_POINT": "0"}
PROGRAM_PATH = "PROG.001.1.PROG_GIORNO_ESTATE"
OVERRIDE_PATH = "PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"


def program() -> list[str]:
    """Zone 001's summer day program 1 (bound to every weekday in the fixture)."""
    return next(
        str(r["Valore"])
        for r in _load("interface.json")
        if f"{r['Gruppo']}.{r['Unita']}.{r['SubUni']}.{r['Key']}" == PROGRAM_PATH
    ).split(",")


def refused(reason: str, fn: Any, *args: Any) -> None:
    with pytest.raises(RehomWriteRefusedError) as info:
        fn(*args)
    assert info.value.reason == reason


def body_ok(plan: Any) -> None:
    assert check_write_body(plan.path, list(plan.records)) == list(plan.records)


# -- whole house -------------------------------------------------------------


def test_fixture_is_manual_comfort_summer() -> None:
    s = state()
    assert s.plant.temperature_comfort == 24.0
    assert set(s.zones) == {"001", "002", "003", "009", "010", "011"}


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (MasterPreset.AUTO, [("MODO", "2"), ("SET_POINT", "0")]),
        (MasterPreset.ECONOMY, [("MODO", "1"), ("SET_POINT", "1"), ("SET_POINT_TEMP", "29")]),
        (MasterPreset.PRE_COMFORT, [("MODO", "1"), ("SET_POINT", "2"), ("SET_POINT_TEMP", "26")]),
        # Comfort always carries the setpoint (the official client omits it).
        (MasterPreset.COMFORT, [("MODO", "1"), ("SET_POINT", "3"), ("SET_POINT_TEMP", "24")]),
    ],
)
def test_house_presets(preset: MasterPreset, expected: list[tuple[str, str]]) -> None:
    plan = plan_house_preset(state(), preset)
    assert plan.path == INTERFACE_WRITE_PATH
    assert [(r["Key"], r["Valore"]) for r in plan.records] == expected
    assert plan.expect == {f"REHOM...{k}": v for k, v in expected}
    body_ok(plan)


def test_house_off_not_offered() -> None:
    refused("unsupported_preset", plan_house_preset, state(), MasterPreset.OFF)


@pytest.mark.parametrize("raw", ["45", "40.1", "4.5", "0", "-2"])
def test_house_level_temperature_outside_the_write_band_is_refused(raw: str) -> None:
    """The planner refuses what the transport gate would (5.0..40.0 °C), with a reason."""
    s = state({"REHOM...TEMP_MAN": raw})  # the ECONOMY level temperature
    refused("level_temperature_out_of_range", plan_house_preset, s, MasterPreset.ECONOMY)


def test_unknown_level_temperature_is_refused() -> None:
    """No level temperature: nothing to write for the house level, no target for an offset."""
    s = state({"REHOM...TEMP_MAN": "x", "REHOM...TEMP_COM": "x"})
    refused("level_temperature_unknown", plan_house_preset, s, MasterPreset.ECONOMY)
    assert s.zones["001"].base is None  # the fixture's zones follow house COMFORT
    refused("no_active_target", plan_zone_offset, s, "001", 1)


@pytest.mark.parametrize(("raw", "sent"), [("40.04", "40"), ("5", "5"), ("39.96", "40")])
def test_house_level_temperature_at_the_band_edges(raw: str, sent: str) -> None:
    plan = plan_house_preset(state({"REHOM...TEMP_MAN": raw}), MasterPreset.ECONOMY)
    assert plan.records[2]["Valore"] == sent
    body_ok(plan)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"REHOM...WEBSERVER": "0"}, "crono_mode"),
        ({"REHOM...WEBSERVER": "2"}, "bus_down"),
        ({"REHOM...TERMO_READONLY": "1"}, "read_only"),
    ],
)
def test_plant_guards_block_every_write(change: dict[str, str], reason: str) -> None:
    s = state({**AUTO, **change})
    refused(reason, plan_house_preset, s, MasterPreset.AUTO)
    refused(reason, plan_zone_offset, s, "001", 1)
    refused(reason, plan_vmc_fan, s, "001", 2)
    refused(reason, plan_predictive, s, True)


def test_comfort_temperature() -> None:
    plan = plan_comfort_temperature(state(), 25.94)
    assert [(r["Key"], r["Valore"]) for r in plan.records] == [
        ("TEMP_COM", "25.9"),
        ("SET_POINT_TEMP", "25.9"),
    ]
    body_ok(plan)
    refused("temperature_out_of_range", plan_comfort_temperature, state(), 17.9)
    refused("temperature_out_of_range", plan_comfort_temperature, state(), 35.6)
    refused("house_not_comfort", plan_comfort_temperature, state(AUTO), 25.0)
    refused("invalid_temperature", plan_comfort_temperature, state(), float("nan"))


def test_comfort_temperature_needs_the_season() -> None:
    refused("season_unknown", plan_comfort_temperature, state({"REHOM...STAGIONE": "9"}), 25.0)


def test_predictive_only_in_auto() -> None:
    refused("house_not_auto", plan_predictive, state(), False)
    plan = plan_predictive(state(AUTO), False)
    assert [(r["Key"], r["Valore"]) for r in plan.records] == [("ALG_ATTIVO", "0")]


@pytest.mark.parametrize(("value", "text"), [(26.0, "26"), (25.9, "25.9"), (24.04, "24")])
def test_format_temperature(value: float, text: str) -> None:
    assert format_temperature(value) == text


# -- zones ---------------------------------------------------------------------


def test_zone_offset_any_house_mode() -> None:
    for s in (state(), state(AUTO)):
        plan = plan_zone_offset(s, "002", -1)
        assert plan.records[0]["Valore"] == "-1.0"
        assert plan.records[0]["path"] == "ZONA.002..DELTA_SETP_CORRENTE"
        body_ok(plan)
    refused("offset_out_of_range", plan_zone_offset, state(), "001", 4)
    refused("invalid_offset", plan_zone_offset, state(), "001", 1.5)
    refused("unknown_zone", plan_zone_offset, state(), "004", 1)


def test_zone_offline_refused() -> None:
    refused(
        "zone_offline",
        plan_zone_offset,
        state({"REHOM...STATO_SONDE": "0,1,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"}),
        "001",
        1,
    )


def test_zone_mode_rules() -> None:
    refused("house_not_auto", plan_zone_mode, state(), "001", ZoneSetp.ECONOMY)
    plan = plan_zone_mode(state(AUTO), "001", ZoneSetp.ECONOMY)
    assert plan.records[0]["Valore"] == "2"
    body_ok(plan)
    assert plan_zone_mode(state(AUTO), "011", ZoneSetp.UNSET).records[0]["Valore"] == "0"
    refused("unsupported_zone_mode", plan_zone_mode, state(AUTO), "001", ZoneSetp.OFF)
    refused("unsupported_zone_mode", plan_zone_mode, state(AUTO), "001", ZoneSetp.PROBE_OFF)
    forced = state({**AUTO, "ZONA.001..SET_FORZ": "1"})
    refused("setpoint_forced", plan_zone_mode, forced, "001", ZoneSetp.COMFORT)


def test_probe_off_can_be_cleared_in_any_house_mode() -> None:
    s = state({"ZONA.001..SETP_CORRENTE": "5"})
    assert plan_zone_mode(s, "001", ZoneSetp.UNSET).records[0]["Valore"] == "0"
    refused("house_not_auto", plan_zone_mode, s, "001", ZoneSetp.ECONOMY)


# -- temporary comfort -----------------------------------------------------------


def test_temporary_comfort_refused_when_already_comfort() -> None:
    refused("already_comfort", plan_temporary_comfort, state(AUTO), "001", 60, NOON)


def test_temporary_comfort_in_the_evening() -> None:
    s = state(AUTO, at=EVENING)
    plan = plan_temporary_comfort(s, "001", 60, EVENING)
    assert plan.path == OVERRIDES_WRITE_PATH
    (record,) = plan.records
    assert record["Impostazione"] == "2026-09-25 22:05:00"
    assert record["Scadenza"] == "2026-09-25 23:04:59"
    levels = str(record["Valore"]).split(",")
    assert len(levels) == 48
    assert levels[44:47] == ["3", "3", "3"]  # 22:00-23:30 slots covering the hour
    original = _load("interface.json")
    program = next(
        r["Valore"]
        for r in original
        if r["Gruppo"] == "PROG"
        and r["Unita"] == "001"
        and r["SubUni"] == "1"
        and r["Key"] == "PROG_GIORNO_ESTATE"
    ).split(",")
    assert levels[:44] == program[:44]  # slots before now untouched
    assert levels[47] == program[47]  # slots after the window untouched
    assert record["Key"] == "PROG_GIORNO_ESTATE"
    # Confirmation needs the window too: an extension may keep the same Valore.
    assert plan.expect == {OVERRIDE_PATH: record["Valore"]}
    assert plan.expect_window == {OVERRIDE_PATH: ("2026-09-25 22:05:00", "2026-09-25 23:04:59")}
    body_ok(plan)


@pytest.mark.parametrize(
    ("minutes", "reason"),
    [
        (0, "invalid_duration"),
        (45, "invalid_duration"),
        (1470, "invalid_duration"),
        (180, "crosses_midnight"),
    ],
)
def test_temporary_comfort_duration_rules(minutes: int, reason: str) -> None:
    refused(reason, plan_temporary_comfort, state(AUTO, at=EVENING), "001", minutes, EVENING)


def test_temporary_comfort_needs_auto_and_schedule() -> None:
    refused("house_not_auto", plan_temporary_comfort, state(at=EVENING), "001", 60, EVENING)
    refused(
        "zone_not_scheduled",
        plan_temporary_comfort,
        state(AUTO, at=EVENING),
        "011",  # manual COMFORT in the fixture
        60,
        EVENING,
    )


# -- VMC -------------------------------------------------------------------------


def test_vmc_fan_discrete() -> None:
    plan = plan_vmc_fan(state(), "001", 2)
    assert plan.records[0]["Valore"] == "2"
    body_ok(plan)
    refused("invalid_fan_value", plan_vmc_fan, state(), "001", 0)
    refused("invalid_fan_value", plan_vmc_fan, state(), "001", 5)
    refused("invalid_fan_value", plan_vmc_fan, state(), "001", 2.0)
    refused("invalid_fan_value", plan_vmc_fan, state(), "001", True)
    refused("unknown_vmc", plan_vmc_fan, state(), "003", 2)


def test_vmc_fan_locked_by_mode_and_availability() -> None:
    refused("fan_locked_by_mode", plan_vmc_fan, state({"DEUM.001..ST_MODE": "0"}), "001", 2)
    refused("fan_not_writable", plan_vmc_fan, state({"DEUM.001..ABILITA_VENTOLA": "1"}), "001", 2)
    refused("vmc_offline", plan_vmc_fan, state({"REHOM...STATO_DEUM": "0,1,0"}), "001", 2)


@pytest.mark.parametrize("vmc_state", ["2", "3"])  # ERROR, FORCED
def test_vmc_fan_refused_while_busy(vmc_state: str) -> None:
    s = state({"DEUM.001..ST_STATO_DEUM": vmc_state})
    refused("vmc_busy", plan_vmc_fan, s, "001", 2)
    refused("vmc_busy", plan_vmc_mode, s, "001", VmcMode.VENTILATE)


def test_vmc_fan_continuous_range() -> None:
    s = state({"DEUM...STEP": "-1"})  # continuous fans, 0..100 in the fixture
    plan = plan_vmc_fan(s, "001", 55)
    assert plan.records[0]["Valore"] == "55"
    body_ok(plan)
    refused("invalid_fan_value", plan_vmc_fan, s, "001", 101)
    refused("invalid_fan_value", plan_vmc_fan, s, "001", -1)


def test_vmc_fan_continuous_range_is_capped_to_the_write_gate() -> None:
    """A reported range wider than 0..100 is capped: the gate refuses COM_VENTILA outside it."""
    s = state({"DEUM...STEP": "-1", "DEUM.001..STEP_MAX": "150", "DEUM.002..STEP_MIN": "-5"})
    assert (s.vmcs["001"].fan.step_max, s.vmcs["002"].fan.step_min) == (150, -5)
    body_ok(plan_vmc_fan(s, "001", 100))
    refused("invalid_fan_value", plan_vmc_fan, s, "001", 101)
    refused("invalid_fan_value", plan_vmc_fan, s, "001", 150)
    body_ok(plan_vmc_fan(s, "002", 0))
    refused("invalid_fan_value", plan_vmc_fan, s, "002", -1)


def test_vmc_mode() -> None:
    s = state()
    assert VmcMode.VENTILATE in s.vmcs["001"].selectable_modes
    plan = plan_vmc_mode(s, "001", VmcMode.VENTILATE)
    assert plan.records[0]["Valore"] == "8"
    body_ok(plan)
    refused("mode_not_available", plan_vmc_mode, s, "001", VmcMode.HEAT)
    refused(
        "vmc_busy", plan_vmc_mode, state({"DEUM.001..ST_STATO_DEUM": "2"}), "001", VmcMode.VENTILATE
    )


@pytest.mark.parametrize("mode", [VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT])
def test_vmc_rapid_modes_are_never_planned(mode: VmcMode) -> None:
    """Rapid modes are refused with a reason even when selectable (the gate never sends 6/7)."""
    s = state()
    assert VmcMode.RAPID_RENEWAL in s.vmcs["001"].selectable_modes  # as in the fixture
    refused("unsupported_vmc_mode", plan_vmc_mode, s, "001", mode)


def test_temporary_comfort_extension_keeps_the_start() -> None:
    first = plan_temporary_comfort(state(AUTO, at=EVENING), "001", 30, EVENING)
    (rec,) = first.records
    later = EVENING + timedelta(minutes=10)
    row = override_row(
        "001", "1", str(rec["Valore"]), str(rec["Impostazione"]), str(rec["Scadenza"])
    )
    s = state(AUTO, at=later, overrides=[row])
    assert s.zones["001"].override is not None and s.zones["001"].override.applies
    extended = plan_temporary_comfort(s, "001", 60, later)
    (ext,) = extended.records
    assert ext["Impostazione"] == "2026-09-25 22:05:00"  # original start kept
    assert ext["Scadenza"] == "2026-09-25 23:14:59"
    assert extended.expect_window == {OVERRIDE_PATH: ("2026-09-25 22:05:00", "2026-09-25 23:14:59")}
    refused("temporary_comfort_active", plan_zone_mode, s, "001", ZoneSetp.ECONOMY)


def test_temporary_comfort_extension_with_the_same_slots_moves_only_the_window() -> None:
    first = plan_temporary_comfort(state(AUTO, at=EVENING), "001", 30, EVENING)
    (rec,) = first.records
    later = EVENING + timedelta(minutes=15)  # 22:20: +30 min still ends in slot 45
    row = override_row(
        "001", "1", str(rec["Valore"]), str(rec["Impostazione"]), str(rec["Scadenza"])
    )
    extended = plan_temporary_comfort(state(AUTO, at=later, overrides=[row]), "001", 30, later)
    (ext,) = extended.records
    assert ext["Valore"] == rec["Valore"]
    assert extended.expect == first.expect
    assert extended.expect_window == {OVERRIDE_PATH: ("2026-09-25 22:05:00", "2026-09-25 22:49:59")}


def test_temporary_comfort_expired_row_does_not_keep_its_start() -> None:
    """A same-day row that already expired must not stretch the new window back."""
    levels = program()
    levels[12:14] = ["3", "3"]
    row = override_row("001", "1", ",".join(levels), "2026-09-25 06:00:00", "2026-09-25 06:29:59")
    s = state(AUTO, at=EVENING, overrides=[row])
    assert s.zones["001"].override is not None and not s.zones["001"].override.applies
    (record,) = plan_temporary_comfort(s, "001", 60, EVENING).records
    assert record["Impostazione"] == "2026-09-25 22:05:00"
    assert record["Scadenza"] == "2026-09-25 23:04:59"
    new_levels = str(record["Valore"]).split(",")
    assert new_levels[12:14] == program()[12:14]  # the old boost is not carried over
    assert new_levels[44:47] == ["3", "3", "3"]


def test_temporary_comfort_active_override_from_another_day_gets_a_new_start() -> None:
    levels = program()
    levels[44:48] = ["3"] * 4
    row = override_row("001", "1", ",".join(levels), "2026-09-24 22:00:00", "2026-09-25 23:59:59")
    s = state(AUTO, at=EVENING, overrides=[row])
    assert s.zones["001"].override is not None and s.zones["001"].override.applies
    (record,) = plan_temporary_comfort(s, "001", 30, EVENING).records
    assert record["Impostazione"] == "2026-09-25 22:05:00"  # same day only


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"ZONA.001..SET_FORZ": "1"}, "setpoint_forced"),
        ({"REHOM...STAGIONE": "9"}, "season_unknown"),
        ({PROGRAM_PATH: ",".join(["0"] * 47)}, "not_scheduled"),  # not 48 slots
        ({PROGRAM_PATH: ",".join(["0"] * 47 + ["7"])}, "not_scheduled"),  # invalid level
    ],
)
def test_temporary_comfort_refusals(changes: dict[str, str], reason: str) -> None:
    s = state({**AUTO, **changes}, at=EVENING)
    refused(reason, plan_temporary_comfort, s, "001", 60, EVENING)


@pytest.mark.parametrize("preset", ["01", "0", "100"])
def test_temporary_comfort_program_id_the_gate_cannot_carry(preset: str) -> None:
    """A day bound to a program id outside 1..99 (canonical) is refused, not sent to the gate."""
    bind = {f"PROG.001.{day}.PROG_SETT_ESTATE": preset for day in range(7)}
    row = {
        "Gruppo": "PROG",
        "Unita": "001",
        "SubUni": preset,
        "Key": "PROG_GIORNO_ESTATE",
        "Valore": ",".join(program()),
        "path": f"PROG.001.{preset}.PROG_GIORNO_ESTATE",
    }
    s = state({**AUTO, **bind}, at=EVENING, extra_rows=[row])
    for day in s.zones["001"].schedule.summer.days:  # bound and resolved every day
        assert (day.preset, day.levels is not None) == (preset, True)
    refused("not_scheduled", plan_temporary_comfort, s, "001", 60, EVENING)


_VARIANTS = {
    "manual": {},
    "auto": AUTO,
    "continuous": {"DEUM...STEP": "-1"},
    "winter-auto": {**AUTO, "REHOM...STAGIONE": "0"},
}


@pytest.mark.parametrize("variant", sorted(_VARIANTS))
def test_every_plan_passes_the_write_gate(variant: str) -> None:
    """Across the fixture, a planner either refuses or returns a body the gate sends as is."""
    checked = 0
    for at in (NOON, EVENING):
        s = state(dict(_VARIANTS[variant]), at=at)
        calls: list[tuple[Any, ...]] = [(plan_house_preset, s, p) for p in MasterPreset]
        calls += [(plan_comfort_temperature, s, t) for t in (16.0, 24.0, 28.5, 35.5)]
        calls += [(plan_predictive, s, on) for on in (True, False)]
        for zid in s.zones:
            calls += [(plan_zone_offset, s, zid, off) for off in range(-3, 4)]
            calls += [(plan_zone_mode, s, zid, setp) for setp in ZoneSetp]
            calls += [(plan_temporary_comfort, s, zid, m, at) for m in (30, 60, 90)]
        for vid in s.vmcs:
            calls += [(plan_vmc_fan, s, vid, v) for v in (-1, 0, 1, 2, 3, 4, 5, 50, 100, 101)]
            calls += [(plan_vmc_mode, s, vid, mode) for mode in VmcMode]
        for fn, *args in calls:
            try:
                plan = fn(*args)
            except RehomWriteRefusedError:
                continue
            body_ok(plan)
            checked += 1
    assert checked > 50
