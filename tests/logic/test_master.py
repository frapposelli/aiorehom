"""``aiorehom.logic.master``: preset, display temperature, effective setpoint, mismatch."""

from __future__ import annotations

import pytest

from aiorehom.enums import Level, MasterMode, MasterPreset, MasterSetPoint
from aiorehom.logic import (
    LEVEL_TEMPERATURE_KEYS,
    display_temperature,
    effective_setpoint,
    master_preset,
    preset_level,
    setpoint_mismatch,
)

from ._helpers import TEMPERATURES

M = MasterMode
SP = MasterSetPoint
MP = MasterPreset


def test_level_temperature_keys() -> None:
    assert dict(LEVEL_TEMPERATURE_KEYS) == {
        Level.OFF: "TEMP_OFF",
        Level.ECONOMY: "TEMP_MAN",
        Level.PRE_COMFORT: "TEMP_PRE",
        Level.COMFORT: "TEMP_COM",
    }
    assert "TEMP_ECO" not in LEVEL_TEMPERATURE_KEYS.values()


@pytest.mark.parametrize(
    ("mode", "set_point", "preset"),
    [
        (M.AUTO, SP.UNSET, MP.AUTO),
        (M.AUTO, SP.COMFORT, MP.AUTO),
        (M.OFF, SP.COMFORT, MP.OFF),
        (M.MANUAL, SP.ECONOMY, MP.ECONOMY),
        (M.MANUAL, SP.PRE_COMFORT, MP.PRE_COMFORT),
        (M.MANUAL, SP.COMFORT, MP.COMFORT),
        (M.MANUAL, SP.UNSET, None),  # deviation: the UI shows OFF
        (M.MANUAL, None, None),
        (None, SP.COMFORT, None),
    ],
)
def test_master_preset(
    mode: MasterMode | None, set_point: MasterSetPoint | None, preset: MasterPreset | None
) -> None:
    assert master_preset(mode, set_point) is preset


def test_preset_level_and_display_temperature() -> None:
    assert preset_level(MP.OFF) is Level.OFF
    assert preset_level(MP.ECONOMY) is Level.ECONOMY
    assert preset_level(MP.PRE_COMFORT) is Level.PRE_COMFORT
    assert preset_level(MP.COMFORT) is Level.COMFORT
    assert preset_level(MP.AUTO) is None
    assert preset_level(None) is None
    assert display_temperature(MP.COMFORT, TEMPERATURES) == 24.0
    assert display_temperature(MP.OFF, TEMPERATURES) == 38.0
    assert display_temperature(MP.ECONOMY, TEMPERATURES) == 29.0
    assert display_temperature(MP.AUTO, TEMPERATURES) is None
    assert display_temperature(None, TEMPERATURES) is None
    assert display_temperature(MP.COMFORT, {}) is None


def test_effective_setpoint() -> None:
    assert effective_setpoint(M.MANUAL, 26.0) == 26.0
    assert effective_setpoint(M.MANUAL, None) is None
    assert effective_setpoint(M.AUTO, 26.0) is None
    assert effective_setpoint(M.OFF, 26.0) is None
    assert effective_setpoint(None, 26.0) is None


def _mismatch(
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    comfort: float | None,
    controller: float | None,
) -> bool | None:
    temps = {**TEMPERATURES, Level.COMFORT: comfort}
    return setpoint_mismatch(
        mode=mode, set_point=set_point, temperatures=temps, controller_setpoint=controller
    )


@pytest.mark.parametrize(
    ("mode", "set_point", "comfort", "controller", "expected"),
    [
        (M.MANUAL, SP.COMFORT, 24.0, 26.0, True),  # the fixture at 10:22
        (M.MANUAL, SP.COMFORT, 24.1, 24.1, False),
        (M.MANUAL, SP.COMFORT, 24.0, 24.1, True),
        (M.MANUAL, SP.COMFORT, 24.0, 24.0, False),  # the fixture after 10:41:10
        (M.MANUAL, SP.COMFORT, 24.0, 24.04, False),  # within the 0.05 tolerance
        (M.AUTO, SP.UNSET, 24.0, 26.0, False),
        (M.OFF, SP.UNSET, 24.0, 26.0, False),
        (None, SP.COMFORT, 24.0, 26.0, None),
        (M.MANUAL, SP.UNSET, 24.0, 26.0, None),
        (M.MANUAL, None, 24.0, 26.0, None),
        (M.MANUAL, SP.COMFORT, None, 26.0, None),
        (M.MANUAL, SP.COMFORT, 24.0, None, None),
    ],
)
def test_setpoint_mismatch(
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    comfort: float | None,
    controller: float | None,
    expected: bool | None,
) -> None:
    assert _mismatch(mode, set_point, comfort, controller) is expected


def test_setpoint_mismatch_other_levels() -> None:
    temps = dict(TEMPERATURES)
    assert (
        setpoint_mismatch(
            mode=M.MANUAL, set_point=SP.ECONOMY, temperatures=temps, controller_setpoint=29.0
        )
        is False
    )
    assert (
        setpoint_mismatch(
            mode=M.MANUAL, set_point=SP.PRE_COMFORT, temperatures=temps, controller_setpoint=24.0
        )
        is True
    )
