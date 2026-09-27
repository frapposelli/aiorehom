"""Write planning: turn a typed request into the exact records to send, or refuse it.

Pure functions, no I/O.  Each ``plan_*`` function reads the current
:class:`~aiorehom.models.RehomState`, checks the guards and preconditions, and
returns a :class:`WritePlan` (the bulk-write path, the records, and the values
the controller is expected to report back).  A refused request raises
:class:`~aiorehom.exceptions.RehomWriteRefusedError` with a stable ``reason``.

Only record shapes observed from the official client are produced, and every
value is pre-checked against the transport's write gate
(:func:`~aiorehom.transport.check_write_body`): unusual controller data (a fan
range beyond 0..100, a level temperature outside the gate's band, a program id
the override path cannot carry) gives a refusal, never a body the gate rejects.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from .enums import (
    Availability,
    FanKind,
    FanSpeed,
    Level,
    LockState,
    MasterMode,
    MasterPreset,
    MasterSetPoint,
    Season,
    VmcMode,
    VmcState,
    ZoneSetp,
)
from .exceptions import RehomWriteRefusedError
from .logic.schedule import slot_of, weekday_js
from .models import Plant, RehomState, Vmc, Zone
from .transport import INTERFACE_WRITE_PATH, OVERRIDES_WRITE_PATH, WRITE_TEMPERATURE_RANGE

__all__ = [
    "COMFORT_RANGE",
    "TEMPORARY_COMFORT_STEP_MINUTES",
    "WritePlan",
    "format_temperature",
    "plan_comfort_temperature",
    "plan_house_preset",
    "plan_predictive",
    "plan_temporary_comfort",
    "plan_vmc_fan",
    "plan_vmc_mode",
    "plan_zone_mode",
    "plan_zone_offset",
]

#: Comfort temperature limits per season (the official client's clamps).
COMFORT_RANGE: Final[Mapping[Season, tuple[float, float]]] = {
    Season.WINTER: (16.0, 28.5),
    Season.SUMMER: (18.0, 35.5),
}
ZONE_OFFSET_LIMIT: Final = 3
TEMPORARY_COMFORT_STEP_MINUTES: Final = 30
TEMPORARY_COMFORT_MAX_MINUTES: Final = 24 * 60
#: VMC modes in which the fan speed is fixed by the controller.
_FAN_LOCKED_MODES: Final = frozenset(
    {VmcMode.STOP, VmcMode.STANDBY, VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT}
)
#: VMC modes never written: timed rapid cycles (the write gate refuses them too).
_VMC_UNWRITABLE_MODES: Final = frozenset({VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT})
#: ``COM_VENTILA`` values the write gate accepts; a continuous fan's own range may be wider.
_FAN_VALUE_LIMITS: Final = (0, 100)
_ZONE_WRITABLE_SETP: Final = frozenset(
    {ZoneSetp.UNSET, ZoneSetp.ECONOMY, ZoneSetp.PRE_COMFORT, ZoneSetp.COMFORT}
)
_OVERRIDE_TIME: Final = "%Y-%m-%d %H:%M:%S"
#: Program ids an override record may carry (the write gate's ``SubUni`` rule).
_OVERRIDE_PRESET_RE: Final = re.compile(r"[1-9][0-9]?")


@dataclass(frozen=True, slots=True, kw_only=True)
class WritePlan:
    """One bulk write and how to confirm it.

    Attributes:
        path: the bulk-write endpoint.
        records: the records to send, in order.
        expect: record path -> value the controller should report afterwards
            (compared numerically where both sides are numbers).
        expect_window: override path -> ``(Impostazione, Scadenza)`` the
            override row must also report (device-local timestamps, compared
            as instants).  An extension can keep the same ``Valore`` and move
            only the window, so the value alone does not confirm it.
        description: a short, non-personal summary for logs.
    """

    path: str
    records: tuple[Mapping[str, object], ...]
    expect: Mapping[str, str]
    expect_window: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    description: str


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _refuse(reason: str, message: str) -> RehomWriteRefusedError:
    return RehomWriteRefusedError(reason, message)


def format_temperature(value: float) -> str:
    """Format like the controller: one decimal, trailing ``.0`` dropped (``26``, ``25.9``)."""
    text = f"{round(value, 1):.1f}"
    return text[:-2] if text.endswith(".0") else text


def _record(gruppo: str, unita: str, key: str, valore: str) -> dict[str, object]:
    return {
        "Gruppo": gruppo,
        "Unita": unita,
        "SubUni": "",
        "Key": key,
        "Valore": valore,
        "Stato": 1,
        "Flusso": 0,
        "path": f"{gruppo}.{unita}..{key}",
    }


def _interface_plan(records: Sequence[dict[str, object]], description: str) -> WritePlan:
    return WritePlan(
        path=INTERFACE_WRITE_PATH,
        records=tuple(records),
        expect={str(r["path"]): str(r["Valore"]) for r in records},
        description=description,
    )


def _check_plant(plant: Plant) -> None:
    if plant.lock is LockState.CRONO:
        raise _refuse("crono_mode", "the master panel is in control (crono mode)")
    if plant.lock is LockState.SERIAL_DOWN:
        raise _refuse("bus_down", "the controller's bus is disconnected")
    if plant.read_only is True:
        raise _refuse("read_only", "the controller is in read-only mode")


def _zone(state: RehomState, zone_id: str) -> Zone:
    zone = state.zones.get(zone_id)
    if zone is None:
        raise _refuse("unknown_zone", f"zone {zone_id!r} is not present")
    if zone.online is False:
        raise _refuse("zone_offline", f"zone {zone_id} is not responding")
    return zone


def _vmc(state: RehomState, vmc_id: str) -> Vmc:
    vmc = state.vmcs.get(vmc_id)
    if vmc is None:
        raise _refuse("unknown_vmc", f"VMC {vmc_id!r} is not present")
    if vmc.online is False:
        raise _refuse("vmc_offline", f"VMC {vmc_id} is not responding")
    if vmc.state in (VmcState.ERROR, VmcState.FORCED):
        raise _refuse("vmc_busy", f"VMC {vmc_id} is in error or a forced mode")
    return vmc


# ---------------------------------------------------------------------------
# whole house
# ---------------------------------------------------------------------------

_PRESET_LEVEL: Final[Mapping[MasterPreset, tuple[MasterSetPoint, str]]] = {
    MasterPreset.ECONOMY: (MasterSetPoint.ECONOMY, "temperature_economy"),
    MasterPreset.PRE_COMFORT: (MasterSetPoint.PRE_COMFORT, "temperature_pre_comfort"),
    MasterPreset.COMFORT: (MasterSetPoint.COMFORT, "temperature_comfort"),
}


def plan_house_preset(state: RehomState, preset: MasterPreset) -> WritePlan:
    """House AUTO, or MANUAL at a level.

    A MANUAL level always carries ``SET_POINT_TEMP`` = that level's temperature,
    including COMFORT, so the regulated setpoint can never be left stale; a
    level temperature outside the write gate's band is refused.  OFF is not
    offered.
    """
    plant = state.plant
    _check_plant(plant)
    if preset is MasterPreset.AUTO:
        records = [
            _record("REHOM", "", "MODO", str(int(MasterMode.AUTO))),
            _record("REHOM", "", "SET_POINT", str(int(MasterSetPoint.UNSET))),
        ]
        return _interface_plan(records, "house -> auto")
    if preset not in _PRESET_LEVEL:
        raise _refuse("unsupported_preset", f"house preset {preset.value!r} is not supported")
    set_point, attr = _PRESET_LEVEL[preset]
    temperature = getattr(plant, attr)
    if temperature is None:
        raise _refuse("level_temperature_unknown", f"the {preset.value} temperature is unknown")
    text = format_temperature(temperature)
    low, high = WRITE_TEMPERATURE_RANGE
    if not math.isfinite(temperature) or not low <= Decimal(text) <= high:
        raise _refuse(
            "level_temperature_out_of_range",
            f"the {preset.value} temperature {text} is outside {low}..{high} °C",
        )
    records = [
        _record("REHOM", "", "MODO", str(int(MasterMode.MANUAL))),
        _record("REHOM", "", "SET_POINT", str(int(set_point))),
        _record("REHOM", "", "SET_POINT_TEMP", text),
    ]
    return _interface_plan(records, f"house -> {preset.value}")


def plan_comfort_temperature(state: RehomState, temperature: float) -> WritePlan:
    """Change the comfort temperature while the house is MANUAL/COMFORT (0.1 °C steps)."""
    plant = state.plant
    _check_plant(plant)
    if plant.mode is not MasterMode.MANUAL or plant.set_point is not MasterSetPoint.COMFORT:
        raise _refuse(
            "house_not_comfort", "the comfort temperature can be set only in house COMFORT"
        )
    if plant.season is None:
        raise _refuse("season_unknown", "the season is unknown")
    if not isinstance(temperature, (int, float)) or not math.isfinite(temperature):
        raise _refuse("invalid_temperature", "temperature must be a finite number")
    low, high = COMFORT_RANGE[plant.season]
    value = round(float(temperature), 1)
    if not low <= value <= high:
        raise _refuse(
            "temperature_out_of_range", f"comfort temperature must be {low:g}..{high:g} °C"
        )
    text = format_temperature(value)
    records = [
        _record("REHOM", "", "TEMP_COM", text),
        _record("REHOM", "", "SET_POINT_TEMP", text),
    ]
    return _interface_plan(records, "house comfort temperature")


def plan_predictive(state: RehomState, enabled: bool) -> WritePlan:
    """Switch the predictive algorithm (only while the house is in AUTO)."""
    plant = state.plant
    _check_plant(plant)
    if plant.mode is not MasterMode.AUTO:
        raise _refuse("house_not_auto", "the predictive algorithm needs the house in AUTO")
    records = [_record("REHOM", "", "ALG_ATTIVO", "1" if enabled else "0")]
    return _interface_plan(records, f"predictive -> {'on' if enabled else 'off'}")


# ---------------------------------------------------------------------------
# zones
# ---------------------------------------------------------------------------


def plan_zone_offset(state: RehomState, zone_id: str, offset: int) -> WritePlan:
    """Set a zone's offset (-3..+3 °C, whole degrees).  Allowed in any house mode."""
    _check_plant(state.plant)
    zone = _zone(state, zone_id)
    if isinstance(offset, bool) or not isinstance(offset, int):
        raise _refuse("invalid_offset", "offset must be a whole number")
    if not -ZONE_OFFSET_LIMIT <= offset <= ZONE_OFFSET_LIMIT:
        raise _refuse("offset_out_of_range", f"offset must be within ±{ZONE_OFFSET_LIMIT}")
    if zone.base is None:
        raise _refuse("no_active_target", f"zone {zone_id} has no active target")
    records = [_record("ZONA", zone_id, "DELTA_SETP_CORRENTE", f"{offset}.0")]
    return _interface_plan(records, f"zone {zone_id} offset")


def plan_zone_mode(state: RehomState, zone_id: str, setp: ZoneSetp) -> WritePlan:
    """Set a zone to follow its schedule (``UNSET``) or to a manual level.

    Level changes need the house in AUTO and no externally forced setpoint;
    returning to the schedule from a probe-forced OFF needs only the plant guards.
    """
    plant = state.plant
    _check_plant(plant)
    zone = _zone(state, zone_id)
    if setp not in _ZONE_WRITABLE_SETP:
        raise _refuse("unsupported_zone_mode", f"zone mode {setp.name} is not supported")
    clearing_probe_off = setp is ZoneSetp.UNSET and zone.setp is ZoneSetp.PROBE_OFF
    if not clearing_probe_off:
        if plant.mode is not MasterMode.AUTO:
            raise _refuse("house_not_auto", "zone modes can be changed only with the house in AUTO")
        if zone.forced is True:
            raise _refuse("setpoint_forced", f"zone {zone_id} has an externally forced setpoint")
    if zone.override is not None and zone.override.applies:
        raise _refuse("temporary_comfort_active", f"zone {zone_id} has a temporary comfort running")
    records = [_record("ZONA", zone_id, "SETP_CORRENTE", str(int(setp)))]
    return _interface_plan(records, f"zone {zone_id} mode -> {setp.name.lower()}")


def plan_temporary_comfort(
    state: RehomState, zone_id: str, minutes: int, now_utc: datetime
) -> WritePlan:
    """Force COMFORT on a zone for ``minutes`` from now (30-minute steps, same day).

    An override still in force on the same preset is extended (same start
    time); an expired one is replaced (new start).  Refused when the zone would
    already be in COMFORT for the whole window: the controller discards such
    overrides.  The plan expects the new window as well as the new value (see
    :attr:`WritePlan.expect_window`).
    """
    plant = state.plant
    _check_plant(plant)
    zone = _zone(state, zone_id)
    if plant.mode is not MasterMode.AUTO:
        raise _refuse("house_not_auto", "temporary comfort needs the house in AUTO")
    if zone.setp is not ZoneSetp.UNSET:
        raise _refuse("zone_not_scheduled", f"zone {zone_id} is not following its schedule")
    if zone.forced is True:
        raise _refuse("setpoint_forced", f"zone {zone_id} has an externally forced setpoint")
    if plant.season is None:
        raise _refuse("season_unknown", "the season is unknown")
    if (
        isinstance(minutes, bool)
        or not isinstance(minutes, int)
        or minutes <= 0
        or minutes % TEMPORARY_COMFORT_STEP_MINUTES
        or minutes > TEMPORARY_COMFORT_MAX_MINUTES
    ):
        raise _refuse("invalid_duration", "duration must be 30..1440 minutes in 30-minute steps")
    local_now = state.device_clock.local_now(now_utc).replace(tzinfo=None, microsecond=0)
    schedule = zone.schedule.summer if plant.season is Season.SUMMER else zone.schedule.winter
    today = schedule.days[weekday_js(local_now)]
    if today.preset is None or today.levels is None:
        raise _refuse("not_scheduled", f"zone {zone_id} has no program bound for today")
    if _OVERRIDE_PRESET_RE.fullmatch(today.preset) is None:
        raise _refuse(
            "not_scheduled", f"zone {zone_id}'s program id for today cannot be overridden"
        )
    start_slot = slot_of(local_now)
    end = local_now + timedelta(minutes=minutes) - timedelta(seconds=1)
    if end.date() != local_now.date():
        raise _refuse("crosses_midnight", "temporary comfort must end before midnight")
    end_slot = slot_of(end)
    levels = list(today.levels)
    if any(level is None for level in levels):
        raise _refuse("not_scheduled", f"zone {zone_id}'s program for today is malformed")
    window = levels[start_slot : end_slot + 1]
    if all(level is Level.COMFORT for level in window):
        raise _refuse(
            "already_comfort", f"zone {zone_id} is already in COMFORT for the whole window"
        )
    for slot in range(start_slot, end_slot + 1):
        levels[slot] = Level.COMFORT
    start = local_now
    existing = zone.override
    # Only an override in force keeps its start: an expired row left in the
    # store (whether the controller deletes them is unverified) must not
    # stretch the new window back to its old start.
    if (
        existing is not None
        and existing.applies
        and existing.preset == today.preset
        and existing.set_at is not None
    ):
        existing_start = state.device_clock.local_now(existing.set_at).replace(
            tzinfo=None, microsecond=0
        )
        if existing_start.date() == local_now.date() and existing_start <= local_now:
            start = existing_start
    record: dict[str, object] = {
        "Gruppo": "PROG_OVERRIDE",
        "Unita": zone_id,
        "SubUni": today.preset,
        "Key": f"PROG_GIORNO_{'ESTATE' if plant.season is Season.SUMMER else 'INVERNO'}",
        "Valore": ",".join(str(int(level)) for level in levels if level is not None),
        "Impostazione": start.strftime(_OVERRIDE_TIME),
        "Scadenza": end.strftime(_OVERRIDE_TIME),
    }
    path = f"PROG_OVERRIDE.{zone_id}.{today.preset}.{record['Key']}"
    return WritePlan(
        path=OVERRIDES_WRITE_PATH,
        records=(record,),
        expect={path: str(record["Valore"])},
        expect_window={path: (str(record["Impostazione"]), str(record["Scadenza"]))},
        description=f"zone {zone_id} temporary comfort {minutes} min",
    )


# ---------------------------------------------------------------------------
# VMC
# ---------------------------------------------------------------------------


def plan_vmc_fan(state: RehomState, vmc_id: str, value: int) -> WritePlan:
    """Set a VMC's fan: a :class:`FanSpeed` (discrete) or a value in ``step_min..step_max``.

    A continuous range is also capped to 0..100 (the write gate's domain).
    Refused while the VMC is in error or a forced mode, as a mode change is.
    """
    _check_plant(state.plant)
    vmc = _vmc(state, vmc_id)
    fan = vmc.fan
    if fan.control is not Availability.WRITABLE:
        raise _refuse("fan_not_writable", f"VMC {vmc_id}'s fan cannot be set")
    if vmc.effective_mode in _FAN_LOCKED_MODES:
        raise _refuse("fan_locked_by_mode", f"VMC {vmc_id}'s fan is fixed in its current mode")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _refuse("invalid_fan_value", "fan value must be an integer")
    if fan.kind is FanKind.DISCRETE:
        if value not in {int(s) for s in FanSpeed if s is not FanSpeed.NONE}:
            raise _refuse("invalid_fan_value", "fan speed must be MIN, MED, MAX or ATTENUATED")
    else:
        low = max(fan.step_min, _FAN_VALUE_LIMITS[0])
        high = min(fan.step_max, _FAN_VALUE_LIMITS[1])
        if not low <= value <= high:
            raise _refuse("invalid_fan_value", f"fan value must be within {low}..{high}")
    records = [_record("DEUM", vmc_id, "COM_VENTILA", str(value))]
    return _interface_plan(records, f"VMC {vmc_id} fan")


def plan_vmc_mode(state: RehomState, vmc_id: str, mode: VmcMode) -> WritePlan:
    """Set a VMC's operating mode (only modes the installer made selectable).

    The rapid modes (timed renewal / heating cycles) are never written, even
    when selectable.
    """
    _check_plant(state.plant)
    vmc = _vmc(state, vmc_id)
    if mode in _VMC_UNWRITABLE_MODES:
        raise _refuse("unsupported_vmc_mode", f"VMC mode {mode.name} is not supported")
    if mode not in vmc.selectable_modes:
        raise _refuse("mode_not_available", f"mode {mode.name} is not available on VMC {vmc_id}")
    records = [_record("DEUM", vmc_id, "ST_MODE", str(int(mode)))]
    return _interface_plan(records, f"VMC {vmc_id} mode -> {mode.name.lower()}")
