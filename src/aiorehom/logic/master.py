"""Master (plant) preset, effective setpoint and setpoint mismatch.

In MANUAL the plant
regulates to ``REHOM.SET_POINT_TEMP``, which can disagree with the level
temperature the UI shows (the fixture ran at 26 degC while showing 24 degC).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ..enums import Level, MasterMode, MasterPreset, MasterSetPoint
from ..values import temps_equal

__all__ = [
    "LEVEL_TEMPERATURE_KEYS",
    "display_temperature",
    "effective_setpoint",
    "master_preset",
    "preset_level",
    "setpoint_mismatch",
]

#: Plant-wide ``REHOM`` temperature key of each level.  ``TEMP_ECO`` is **not** used (N8).
LEVEL_TEMPERATURE_KEYS: Final[Mapping[Level, str]] = {
    Level.OFF: "TEMP_OFF",
    Level.ECONOMY: "TEMP_MAN",
    Level.PRE_COMFORT: "TEMP_PRE",
    Level.COMFORT: "TEMP_COM",
}

_SET_POINT_PRESET: Final[Mapping[MasterSetPoint | None, MasterPreset]] = {
    MasterSetPoint.ECONOMY: MasterPreset.ECONOMY,
    MasterSetPoint.PRE_COMFORT: MasterPreset.PRE_COMFORT,
    MasterSetPoint.COMFORT: MasterPreset.COMFORT,
}
_SET_POINT_LEVEL: Final[Mapping[MasterSetPoint | None, Level]] = {
    MasterSetPoint.ECONOMY: Level.ECONOMY,
    MasterSetPoint.PRE_COMFORT: Level.PRE_COMFORT,
    MasterSetPoint.COMFORT: Level.COMFORT,
}
_PRESET_LEVEL: Final[Mapping[MasterPreset | None, Level]] = {
    MasterPreset.OFF: Level.OFF,
    MasterPreset.ECONOMY: Level.ECONOMY,
    MasterPreset.PRE_COMFORT: Level.PRE_COMFORT,
    MasterPreset.COMFORT: Level.COMFORT,
}


def master_preset(mode: MasterMode | None, set_point: MasterSetPoint | None) -> MasterPreset | None:
    """AUTO/OFF from the mode; MANUAL with set point 1/2/3 -> ECONOMY/PRE/COMFORT.

    Anything else is ``None``.  Deviation: the UI shows OFF in that case.
    """
    if mode is MasterMode.AUTO:
        return MasterPreset.AUTO
    if mode is MasterMode.OFF:
        return MasterPreset.OFF
    if mode is MasterMode.MANUAL:
        return _SET_POINT_PRESET.get(set_point)
    return None


def preset_level(preset: MasterPreset | None) -> Level | None:
    """The slot level a preset stands for; ``None`` for AUTO (no single level)."""
    return _PRESET_LEVEL.get(preset)


def display_temperature(
    preset: MasterPreset | None, temperatures: Mapping[Level, float | None]
) -> float | None:
    """The level temperature the UI shows for ``preset``; ``None`` in AUTO."""
    level = preset_level(preset)
    return None if level is None else temperatures.get(level)


def effective_setpoint(mode: MasterMode | None, controller_setpoint: float | None) -> float | None:
    """What the plant regulates to: ``SET_POINT_TEMP`` in MANUAL, else ``None``."""
    return controller_setpoint if mode is MasterMode.MANUAL else None


def setpoint_mismatch(
    *,
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    temperatures: Mapping[Level, float | None],
    controller_setpoint: float | None,
) -> bool | None:
    """Whether ``SET_POINT_TEMP`` disagrees with the MANUAL level temperature.

    ``None`` when the mode is unknown or a MANUAL input is missing; ``False``
    outside MANUAL.  Temperatures compare with :func:`aiorehom.values.temps_equal`.
    """
    if mode is None:
        return None
    if mode is not MasterMode.MANUAL:
        return False
    level = _SET_POINT_LEVEL.get(set_point)
    if level is None:
        return None
    level_temperature = temperatures.get(level)
    if level_temperature is None or controller_setpoint is None:
        return None
    return not temps_equal(controller_setpoint, level_temperature)
