"""The vendor ``constants.js`` values vs the library enums."""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from aiorehom.enums import (
    Level,
    MasterMode,
    MasterSetPoint,
    VmcMode,
    VmcScheduleLevel,
    ZoneSetp,
)
from aiorehom.logic import VMC_MODE_AVAILABILITY_KEYS

from ._vectors import cases, load

NAME = "constants.json"
#: JS DEUM_MODE name -> library VmcMode.
DEUM_MODE_NAMES_TO_ENUM = {
    "STOP": VmcMode.STOP,
    "DEUM_ARIA_NEUTRA": VmcMode.DEHUMIDIFY,
    "DEUM_CUM_INTEGRAZIONE_FREDDO": VmcMode.DEHUMIDIFY_COOL,
    "INTEGRAZIONE_FREDDO": VmcMode.COOL,
    "INTEGRAZIONE_CALDO": VmcMode.HEAT,
    "STAND_BY": VmcMode.STANDBY,
    "RINNOVO_RAPIDO": VmcMode.RAPID_RENEWAL,
    "RISCALDO_RAPIDO": VmcMode.RAPID_HEAT,
    "VENTILAZIONE": VmcMode.VENTILATE,
}


def _constants() -> dict[str, Any]:
    return {case["name"]: case["value"] for case in cases(NAME)}


def _as_js_strings(enum: type[IntEnum], rename: dict[str, str] | None = None) -> dict[str, str]:
    rename = rename or {}
    return {rename.get(member.name, member.name): str(member.value) for member in enum}


def test_meta() -> None:
    assert load(NAME)["meta"]["count"] == 8


def test_mode() -> None:
    assert _constants()["MODE"] == _as_js_strings(MasterMode)


def test_set_point() -> None:
    assert _constants()["SET_POINT"] == _as_js_strings(MasterSetPoint)


def test_zone_setp() -> None:
    expected = _as_js_strings(ZoneSetp, {"PROBE_OFF": "ANTIGELO_OFF_FORZATO"})
    assert _constants()["ZONE_SETP"] == expected


def test_zone_plan_mode() -> None:
    assert _constants()["ZONE_PLAN_MODE"] == _as_js_strings(Level)


def test_deum_mode() -> None:
    js = _constants()["DEUM_MODE"]
    assert set(js) == set(DEUM_MODE_NAMES_TO_ENUM)
    assert {name: DEUM_MODE_NAMES_TO_ENUM[name].value for name in js} == js
    assert sorted(js.values()) == [mode.value for mode in VmcMode]


def test_deum_cycle_mode() -> None:
    expected = {
        "MAX": VmcScheduleLevel.MAX,
        "LOW": VmcScheduleLevel.MIN,
        "OFF": VmcScheduleLevel.OFF,
    }
    assert _constants()["DEUM_CYCLE_MODE"] == {k: v.value for k, v in expected.items()}


def test_deum_enable_mode_matches_availability_keys() -> None:
    constants = _constants()
    js = constants["DEUM_ENABLE_MODE"]  # availability key -> DEUM_MODE name
    ours = {
        key: DEUM_MODE_NAMES_TO_ENUM[name]
        for key, name in js.items()
        if constants["DEUM_MODE"][name] == DEUM_MODE_NAMES_TO_ENUM[name].value
    }
    inverted = {key: mode for mode, key in VMC_MODE_AVAILABILITY_KEYS.items()}
    assert ours == inverted
    assert len(js) == len(VMC_MODE_AVAILABILITY_KEYS) == 9


def test_deum_mode_names_count() -> None:
    assert len(_constants()["DEUM_MODE_NAMES"]) == 9
