"""Effective zone level, target, mode, preset, source and action.

All times are naive device-local wall time.  :func:`zone_level_at` evaluates
the zone at any instant with the same store values, which is what the
next-change walker needs.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from ..enums import (
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
from ..values import parse_float
from .override import override_applicable, zone_override
from .parse import nonempty, parse_device_timestamp
from .running import program_source, running_program
from .schedule import slot_of, weekday_js
from .transitions import next_level_change
from .types import OverrideState, Program, ZoneEffective, ZoneInputs

__all__ = [
    "HUMIDITY_NO_SENSOR",
    "zone_effective",
    "zone_humidity",
    "zone_hvac_action",
    "zone_level_at",
    "zone_mode_preset_source",
]

#: ``ZONA.<z>..UMIDITA`` value meaning "no humidity sensor".
HUMIDITY_NO_SENSOR: Final = 255.0

_SCHEDULED: Final = frozenset({ProgramSource.CRONO, ProgramSource.SCHEDULE})
_ONE_SECOND: Final = timedelta(seconds=1)
#: Latest expiry that still has an ``expires_at + 1 s`` instant.
_LAST_EXPIRY: Final = datetime.max - _ONE_SECOND
_SET_POINT_PRESET: Final[Mapping[MasterSetPoint | None, ZonePreset]] = {
    MasterSetPoint.ECONOMY: ZonePreset.ECONOMY,
    MasterSetPoint.PRE_COMFORT: ZonePreset.PRE_COMFORT,
    MasterSetPoint.COMFORT: ZonePreset.COMFORT,
}
_SETP_PRESET: Final[Mapping[ZoneSetp | None, ZonePreset]] = {
    ZoneSetp.ECONOMY: ZonePreset.ECONOMY,
    ZoneSetp.PRE_COMFORT: ZonePreset.PRE_COMFORT,
    ZoneSetp.COMFORT: ZonePreset.COMFORT,
}

_Evaluation = tuple[ProgramSource | None, Program | None, OverrideState | None, Level | None]


def _evaluate(inputs: ZoneInputs, when_local: datetime) -> _Evaluation:
    source = program_source(
        mode=inputs.mode, set_point=inputs.set_point, setp=inputs.setp, is_crono=inputs.is_crono
    )
    week = inputs.week
    if source in _SCHEDULED and week is None:
        return source, None, None, None  # unknown season: no schedule (library policy)
    weekday = weekday_js(when_local)
    today = week.program_map.get(weekday) if week is not None else None
    today_preset = week.index_map.get(weekday) if week is not None else None
    override = zone_override(
        inputs.override_rows,
        season=inputs.season,
        today_preset=today_preset,
        mode=inputs.mode,
        setp=inputs.setp,
        is_crono=inputs.is_crono,
        now_local=when_local,
    )
    running = running_program(
        mode=inputs.mode,
        set_point=inputs.set_point,
        setp=inputs.setp,
        today=today,
        is_crono=inputs.is_crono,
        crono=week.crono if week is not None else None,
    )
    slot = slot_of(when_local)
    program = override.program if override is not None and override.applies else running
    level = program[slot] if program is not None else None
    return source, running, override, level


def zone_level_at(inputs: ZoneInputs, when_local: datetime) -> Level | None:
    """The zone's slot level at ``when_local`` (override first, else the running program)."""
    return _evaluate(inputs, when_local)[3]


def zone_mode_preset_source(
    inputs: ZoneInputs, *, override_applies: bool
) -> tuple[ZoneMode | None, ZonePreset | None, ControlSource | None]:
    """The (mode, preset, control_source) decision table, first matching row.

    Afterwards, ``forced is True`` turns a known control source into EXTERNAL.
    """
    mode, set_point, setp = inputs.mode, inputs.set_point, inputs.setp
    result: tuple[ZoneMode | None, ZonePreset | None, ControlSource | None]
    if setp is ZoneSetp.PROBE_OFF:
        result = (ZoneMode.OFF, ZonePreset.NONE, ControlSource.PROBE)
    elif mode is MasterMode.OFF:
        result = (ZoneMode.OFF, ZonePreset.NONE, ControlSource.HOUSE)
    elif mode is MasterMode.MANUAL and set_point in _SET_POINT_PRESET:
        result = (ZoneMode.MANUAL, _SET_POINT_PRESET[set_point], ControlSource.HOUSE)
    elif mode is MasterMode.AUTO and inputs.is_crono:
        result = (ZoneMode.AUTO, ZonePreset.NONE, ControlSource.CRONO)
    elif mode is MasterMode.AUTO and setp is ZoneSetp.UNSET and override_applies:
        result = (ZoneMode.AUTO, ZonePreset.TEMPORARY_COMFORT, ControlSource.OVERRIDE)
    elif mode is MasterMode.AUTO and setp is ZoneSetp.UNSET:
        result = (ZoneMode.AUTO, ZonePreset.NONE, ControlSource.SCHEDULE)
    elif mode is MasterMode.AUTO and setp is ZoneSetp.OFF:
        result = (ZoneMode.OFF, ZonePreset.NONE, ControlSource.ZONE)
    elif mode is MasterMode.AUTO and setp in _SETP_PRESET:
        result = (ZoneMode.MANUAL, _SETP_PRESET[setp], ControlSource.ZONE)
    else:
        return (None, None, None)
    if inputs.forced is True:
        return (result[0], result[1], ControlSource.EXTERNAL)
    return result


def zone_hvac_action(
    *, calling: bool | None, season: Season | None, mode: ZoneMode | None
) -> HvacAction | None:
    """HEATING/COOLING while calling (by season), else OFF (zone mode OFF) or IDLE."""
    if calling is None:
        return None
    if calling:
        if season is Season.WINTER:
            return HvacAction.HEATING
        if season is Season.SUMMER:
            return HvacAction.COOLING
        return None
    return HvacAction.OFF if mode is ZoneMode.OFF else HvacAction.IDLE


def zone_humidity(raw: str | None) -> tuple[float | None, bool | None]:
    """``(humidity, has_sensor)`` from ``UMIDITA``.

    255 means no sensor: ``(None, False)``.  A number: ``(value, True)``.
    Missing or unparseable: ``(None, None)`` (unknown).
    """
    value = parse_float(raw)
    if value is None or not math.isfinite(value):
        return None, None
    if value == HUMIDITY_NO_SENSOR:
        return None, False
    return value, True


def _offset_for_target(offset_raw: str | None) -> float | None:
    """Missing or blank counts as 0.0; non-empty garbage (or a non-finite value) gives ``None``."""
    if nonempty(offset_raw) is None:
        return 0.0
    offset = parse_float(offset_raw)
    return offset if offset is not None and math.isfinite(offset) else None


def _override_instants(inputs: ZoneInputs) -> list[datetime]:
    out: list[datetime] = []
    for row in inputs.override_rows:
        set_at = parse_device_timestamp(row.set_at_raw)
        expires_at = parse_device_timestamp(row.expires_at_raw)
        if set_at is not None:
            out.append(set_at)
        if expires_at is not None and expires_at < _LAST_EXPIRY:
            out.append(expires_at + _ONE_SECOND)  # 9999-12-31 23:59:59 ("forever") has no +1 s
    return out


def _next_change(inputs: ZoneInputs, source: ProgramSource | None) -> datetime | None:
    can_override = override_applicable(mode=inputs.mode, setp=inputs.setp, is_crono=inputs.is_crono)
    if source not in _SCHEDULED and not can_override:
        return None
    return next_level_change(
        lambda when: zone_level_at(inputs, when),
        now_local=inputs.now_local,
        extra_instants=_override_instants(inputs),
    )


def zone_effective(inputs: ZoneInputs) -> ZoneEffective:
    """Everything the zone model needs, evaluated at ``inputs.now_local``.

    * ``target = round(base + trunc(offset), 1)`` where ``base`` is the forced
      setpoint when ``forced is True``, else the level's plant temperature;
      ``level is None`` gives ``base = target = None``.
    * ``next_change_local`` is the first instant the level changes (7-day walk).
    """
    now = inputs.now_local
    source, running, override, level = _evaluate(inputs, now)
    offset = parse_float(inputs.offset_raw)
    base: float | None
    target: float | None
    if level is None:
        base = target = None
    else:
        base = inputs.forced_setpoint if inputs.forced is True else inputs.temperatures.get(level)
        offset_for_target = _offset_for_target(inputs.offset_raw)
        if base is None or offset_for_target is None:
            target = None
        else:
            target = round(base + math.trunc(offset_for_target), 1)
    override_applies = override is not None and override.applies
    mode, preset, control_source = zone_mode_preset_source(
        inputs, override_applies=override_applies
    )
    return ZoneEffective(
        source=source,
        running=running,
        override=override,
        weekday=weekday_js(now),
        slot=slot_of(now),
        level=level,
        base=base,
        offset=offset,
        target=target,
        mode=mode,
        preset=preset,
        control_source=control_source,
        hvac_action=zone_hvac_action(calling=inputs.calling, season=inputs.season, mode=mode),
        next_change_local=_next_change(inputs, source),
    )
