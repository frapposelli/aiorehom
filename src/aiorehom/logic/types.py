"""Input and result types of the pure logic layer.

Every type is an immutable dataclass.  Zone and schedule times are **naive
device-local wall time**; alarm, health and tracker times are aware UTC.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from ..enums import (
    AlarmSource,
    Availability,
    ControlSource,
    DeviceKind,
    FanKind,
    FanSpeed,
    HvacAction,
    Level,
    LockState,
    MasterMode,
    MasterSetPoint,
    ProgramSource,
    ScheduleSource,
    Season,
    VmcMode,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)

__all__ = [
    "AlarmCondition",
    "AlarmInputs",
    "AlarmView",
    "FanState",
    "ForecastItem",
    "FreeCoolingState",
    "GenericAlarmRow",
    "HeartbeatStatus",
    "LockInfo",
    "NextTransitionCompat",
    "OverrideRow",
    "OverrideState",
    "Program",
    "ScheduleRow",
    "TrackedAlarm",
    "UnitPresence",
    "VmcOperation",
    "WeatherDayInfo",
    "WeatherInfo",
    "WeekPrograms",
    "ZoneEffective",
    "ZoneInputs",
]

#: 48 half-hour slots from 00:00 device time; ``None`` = an invalid slot value.
Program = tuple[Level | None, ...]


@dataclass(frozen=True, slots=True)
class UnitPresence:
    """One installed unit of a ``PRESENZA_*`` vector."""

    unit: str
    index: int
    online: bool | None


@dataclass(frozen=True, slots=True)
class ScheduleRow:
    """A ``PROG`` (or ZONA-mirror) schedule row of one zone, in store order."""

    subuni: str
    key: str
    value: str


@dataclass(frozen=True, slots=True)
class WeekPrograms:
    """A zone's weekly schedule for one season."""

    #: weekday 0=Sun..6 -> preset id (exact string); bound days only.
    index_map: Mapping[int, str]
    #: bound days only; ``None`` = dangling binding or malformed program.
    program_map: Mapping[int, Program | None]
    #: preset ``"99"``; ``None`` if the row is missing or malformed.
    crono: Program | None
    source: ScheduleSource


@dataclass(frozen=True, slots=True)
class OverrideRow:
    """A ``PROG_OVERRIDE`` row of one zone (overrides store)."""

    subuni: str
    key: str
    value: str
    set_at_raw: str | None
    expires_at_raw: str | None


@dataclass(frozen=True, slots=True)
class OverrideState:
    """The zone's primary override row, parsed, and whether it applies now."""

    row: OverrideRow
    program: Program | None
    set_at: datetime | None  # naive device time
    expires_at: datetime | None  # naive device time
    applies: bool


@dataclass(frozen=True, slots=True)
class NextTransitionCompat:
    """Result of :func:`~aiorehom.logic.transitions.next_transition_compat`."""

    day: int | None
    last_slot: int | None
    wrap: bool | None
    running: bool
    slots_minus_one: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneInputs:
    """Everything the zone logic reads, already parsed (raw only where noted)."""

    mode: MasterMode | None
    set_point: MasterSetPoint | None
    season: Season | None
    is_crono: bool
    setp: ZoneSetp | None
    forced: bool | None
    forced_setpoint: float | None
    offset_raw: str | None
    calling: bool | None
    #: plant-wide level temperatures (``LEVEL_TEMPERATURE_KEYS``).
    temperatures: Mapping[Level, float | None]
    #: the ACTIVE season's programs; ``None`` when the season is unknown.
    week: WeekPrograms | None
    #: this zone's rows from the overrides store.
    override_rows: tuple[OverrideRow, ...]
    now_local: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneEffective:
    """Effective zone state at ``ZoneInputs.now_local``."""

    source: ProgramSource | None
    running: Program | None
    override: OverrideState | None
    weekday: int
    slot: int
    level: Level | None
    base: float | None
    offset: float | None
    target: float | None
    mode: ZoneMode | None
    preset: ZonePreset | None
    control_source: ControlSource | None
    hvac_action: HvacAction | None
    next_change_local: datetime | None


@dataclass(frozen=True, slots=True)
class LockInfo:
    """Decoded ``WEBSERVER`` lock."""

    state: LockState
    is_crono: bool
    serial_down: bool


@dataclass(frozen=True, slots=True)
class VmcOperation:
    """Effective VMC mode and run state."""

    effective_mode: VmcMode | None
    running: bool | None
    error: bool | None


@dataclass(frozen=True, slots=True)
class FanState:
    """Decoded VMC fan."""

    kind: FanKind
    control: Availability
    speed: FanSpeed | None
    value: int | None
    step_min: int
    step_max: int
    show_labels: bool


@dataclass(frozen=True, slots=True)
class FreeCoolingState:
    """Decoded VMC free cooling."""

    level: Availability
    on: bool | None
    error: bool | None


@dataclass(frozen=True, slots=True)
class AlarmCondition:
    """One known-active alarm condition; ``id`` is stable."""

    id: str
    source: AlarmSource
    device: DeviceKind
    unit: str | None
    code: int | None
    text: str | None


@dataclass(frozen=True, slots=True)
class GenericAlarmRow:
    """A ``GENERIC_ALARM`` row (ZONA/DEUM/AT9091/ATT)."""

    gruppo: str
    unita: str
    subuni: str
    value: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AlarmInputs:
    """Everything :func:`aiorehom.logic.compute_alarms` reads (raw values: ``None`` = absent)."""

    #: ZONE, VMC, ACTUATOR, FANCOIL -> present units.
    presence: Mapping[DeviceKind, Mapping[str, UnitPresence]]
    #: vmc unit -> {ALLARM_*: raw}.
    vmc_flags: Mapping[str, Mapping[str, str | None]]
    #: vmc unit -> (FREE_COOLING, ERR_FREE_COOLING).
    vmc_free_cooling: Mapping[str, tuple[str | None, str | None]]
    #: GENERIC_ALARM rows of ZONA/DEUM/AT9091/ATT.
    generic: tuple[GenericAlarmRow, ...]
    #: REHOM PROCESS_STATE rows as (Unita, Valore).
    process: tuple[tuple[str, str], ...]
    bus_raw: str | None
    watchdog_raw: str | None
    internet_raw: str | None
    remote_token_raw: str | None
    weather_key_raw: str | None
    lock: LockState


@dataclass(frozen=True, slots=True)
class TrackedAlarm:
    """An active condition with the (UTC) time it was first seen in this streak."""

    condition: AlarmCondition
    first_seen: datetime


@dataclass(frozen=True, slots=True)
class AlarmView:
    """Result of :meth:`aiorehom.logic.AlarmTracker.update`."""

    active: tuple[TrackedAlarm, ...]
    debounced: tuple[TrackedAlarm, ...]
    next_maturity: datetime | None


@dataclass(frozen=True, slots=True)
class HeartbeatStatus:
    """Controller heartbeat health (UTC)."""

    ok: bool | None
    deadline: datetime | None


@dataclass(frozen=True, slots=True)
class WeatherDayInfo:
    """One ``METEO`` forecast day (``Unita`` ``"0"``..``"5"``)."""

    index: int
    condition_type: int | None
    icon_code: int | None
    temp_min: float | None
    temp_max: float | None
    pressure: float | None
    wind_speed: float | None
    wind_bearing: float | None


@dataclass(frozen=True, slots=True)
class WeatherInfo:
    """Decoded ``METEO`` rows (cloud weather, not a probe)."""

    updated_local: datetime | None
    #: 9 three-hourly forecast values; **not** an outdoor reading.
    forecast_temperatures: tuple[float | None, ...]
    humidity: float | None
    days: tuple[WeatherDayInfo, ...]
    service_ok: bool | None


@dataclass(frozen=True, slots=True)
class ForecastItem:
    """One ``METEO_DATA`` item (an OpenWeatherMap 3-hour forecast entry)."""

    epoch: int
    temperature: float | None
    feels_like: float | None
    humidity: float | None
    dew_point: float | None
    pressure: float | None
    wind_speed: float | None
    wind_bearing: float | None
    wind_gust: float | None
    pop: float | None
    clouds: float | None
    condition_id: int | None
    icon: str | None
