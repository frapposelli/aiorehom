"""Immutable state model of one Rehom controller (built by aiorehom.builder).

Conventions for every class here:

* All classes are frozen dataclasses; a :class:`RehomState` is never mutated,
  a new one is built for every change.  Mappings are read-only
  (``types.MappingProxyType``) and compare with ``==``.
* Every timestamp is an aware UTC ``datetime``, except
  :attr:`HistorySample.local_time` (naive device-local time).
* ``None`` always means *unknown*: the controller row is missing or its value
  does not parse.  It never means ``False``, ``0`` or "off".
* Temperatures are degrees Celsius as ``float``.
* Only units the controller flags as present (``PRESENZA_*`` vectors) are
  modelled; mappings of units are keyed by the 3-digit unit id (``"001"``),
  in ascending order.
* ``dataclasses.asdict()`` does not support the read-only mappings: use
  :meth:`RehomState.as_dict` (JSON-ready).  ``copy.deepcopy()`` and ``pickle``
  work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Any

from .clock import DeviceClock
from .enums import (
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
    MasterPreset,
    MasterSetPoint,
    ScheduleSource,
    Season,
    UpdateReason,
    VmcMode,
    VmcState,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)

Program = tuple[Level | None, ...]  # 48 half-hour slots from 00:00 device time; None = invalid slot


# ---------------------------------------------------------------------------
# JSON conversion and pickling of the models that hold read-only mappings
# ---------------------------------------------------------------------------


def _json_key(key: object) -> str:
    return key.name if isinstance(key, Enum) else str(key)


def _jsonable(value: Any) -> Any:
    """JSON-ready copy: dataclasses and mappings become dicts, tuples lists.

    Datetimes become ISO strings (UTC with ``Z``), timedeltas seconds, a time
    zone its name, a ``StrEnum`` its value and an ``IntEnum`` its name (as in
    ``rehom-probe replay``).
    """
    if value is None or (isinstance(value, (bool, str)) and not isinstance(value, Enum)):
        return value
    if isinstance(value, IntEnum):
        return value.name
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, tzinfo):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {_json_key(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        items = sorted(value) if isinstance(value, (frozenset, set)) else value
        return [_jsonable(item) for item in items]
    return str(value)


def _restore[T](cls: Callable[..., T], values: dict[str, Any]) -> T:
    """Unpickle helper: re-wrap the mappings :func:`_reduce_frozen` unwrapped."""
    return cls(
        **{
            name: MappingProxyType(value) if isinstance(value, dict) else value
            for name, value in values.items()
        }
    )


def _reduce_frozen(obj: Any) -> tuple[Any, tuple[Any, ...]]:
    """``__reduce__`` for a model with ``MappingProxyType`` fields (not picklable as is)."""
    values = {
        item.name: dict(value) if isinstance(value, MappingProxyType) else value
        for item in fields(obj)
        for value in (getattr(obj, item.name),)
    }
    return (_restore, (type(obj), values))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class UnitIdentity:
    """Identity rows of one unit (zone probe, VMC, actuator); personal-ish, mask in logs.

    Attributes:
        serial: ``MATRICOLA``.
        firmware: ``VERSIONE_FW``.
        custom_id: ``CUSTOM_ID``.
    """

    serial: str | None = None  # MATRICOLA (personal-ish: mask in logs/diagnostics)
    firmware: str | None = None  # VERSIONE_FW
    custom_id: str | None = None  # CUSTOM_ID


@dataclass(frozen=True, slots=True, kw_only=True)
class Hub:
    """The controller itself.

    Attributes:
        mac: ``WEBSERVER...MacAddress``, lower case (the stable hub id; mask in logs).
        web_version: ``/api/alive/`` ``version`` (the web server's version).
        controller_version: ``REHOM...VER_SOFT`` (the plant firmware).
        board: ``/api/config/`` ``BOARD``.
        platform: ``/api/config/`` ``PLATFORM``.
        timezone: ``/api/config/`` ``TIMEZONE`` as reported (see also
            :attr:`RehomState.device_clock`).
    """

    mac: str | None
    web_version: str | None
    controller_version: str | None
    board: str | None
    platform: str | None
    timezone: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Plant:
    """Whole-house state (the ``REHOM`` rows).

    Attributes:
        season: ``STAGIONE``: winter (heating) or summer (cooling).
        season_forced: ``FORZATURA_STAGIONE``.
        conf_season: the plant-conf ``stagione`` key.
        mode: ``MODO``: OFF, MANUAL (a fixed level) or AUTO (zone schedules).
        set_point: ``SET_POINT``: the level selected in MANUAL.
        preset: the decoded whole-house preset (``mode`` + ``set_point``);
            ``None`` for combinations the UI would show as OFF.
        temperature_off: ``TEMP_OFF``, the OFF-level temperature.
        temperature_economy: ``TEMP_MAN``, the ECONOMY-level temperature.
        temperature_pre_comfort: ``TEMP_PRE``.
        temperature_comfort: ``TEMP_COM``.
        temperature_eco_aux: ``TEMP_ECO``, display only (not a level temperature).
        display_temperature: the temperature of the preset's level (the UI's
            big number); ``None`` in AUTO.
        controller_setpoint: ``SET_POINT_TEMP``, what the controller actually uses.
        effective_setpoint: ``controller_setpoint`` in MANUAL, else ``None``.
        setpoint_mismatch: MANUAL and ``controller_setpoint`` differs from the
            selected level's temperature; ``False`` outside MANUAL, ``None`` if
            unknown.
        setpoint_mismatch_since: when ``setpoint_mismatch`` last became True.
        predictive: ``ALG_ATTIVO``.
        read_only: ``TERMO_READONLY``.
        lock: ``WEBSERVER`` lock state (crono, serial line down, normal).
        is_crono: the plant is driven by the crono (``WEBSERVER == 0``).
        serial_down: the serial line to the plant is down (``WEBSERVER == 2``).
        server_on_requested: ``SERVER_ON``.
        demand: any present zone is calling; ``None`` if no zone's call is known.
        current_temperature: zone ``001``'s temperature (the UI's house reading).
        domotica_enabled: ``ABILITA_DOMOTICA == 1`` (missing = False).
        installer_session: an installer session is open (plant-conf
            ``CONFIGURA_ON``/``CONF_ZONE_ON``/``CONF_DEUM_ON``).
        installer_activity_at: last commissioning (``RIS_*``) frame.
        vmc_type_code: ``DEUM...TIPO`` raw (a numeric type code).
    """

    season: Season | None
    season_forced: bool | None
    conf_season: Season | None
    mode: MasterMode | None
    set_point: MasterSetPoint | None
    preset: MasterPreset | None
    temperature_off: float | None
    temperature_economy: float | None
    temperature_pre_comfort: float | None
    temperature_comfort: float | None
    temperature_eco_aux: float | None
    display_temperature: float | None
    controller_setpoint: float | None
    effective_setpoint: float | None
    setpoint_mismatch: bool | None
    setpoint_mismatch_since: datetime | None
    predictive: bool | None
    read_only: bool | None
    lock: LockState
    is_crono: bool
    serial_down: bool
    server_on_requested: bool | None
    demand: bool | None
    current_temperature: float | None
    domotica_enabled: bool
    installer_session: bool | None
    installer_activity_at: datetime | None
    vmc_type_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DayProgram:
    """One weekday of a zone's season schedule.

    Attributes:
        weekday: 0 = Sunday .. 6 = Saturday (the controller's convention).
        preset: the bound preset id (an exact string); ``None`` = unbound.
        levels: the preset's 48 half-hour levels from 00:00 device time;
            ``None`` if unbound, dangling (the preset does not exist) or malformed.
    """

    weekday: int  # 0 = Sunday
    preset: str | None  # bound preset id (exact string); None = unbound
    levels: Program | None  # None = unbound, dangling or malformed


@dataclass(frozen=True, slots=True, kw_only=True)
class SeasonSchedule:
    """A zone's weekly schedule for one season.

    Attributes:
        days: 7 entries, index = weekday (0 = Sunday).
        crono: the hidden preset ``99`` used while the plant is in crono.
        source: where the rows came from (``PROG``, the ``ZONA`` mirror, none).
    """

    days: tuple[DayProgram, ...]  # 7 entries, index = weekday
    crono: Program | None  # preset 99
    source: ScheduleSource


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneSchedule:
    """Both season schedules of a zone (the active one follows :attr:`Plant.season`)."""

    winter: SeasonSchedule
    summer: SeasonSchedule


@dataclass(frozen=True, slots=True, kw_only=True)
class ZoneOverride:
    """A zone's temporary override ("boost") row for the active season.

    Attributes:
        preset: the preset id the override is bound to (it can apply only on a
            day bound to that preset).
        levels: the override's 48 half-hour levels.
        set_at: ``Impostazione`` (start).
        expires_at: ``Scadenza``, the last second the override applies.
        applies: the override drives the zone right now (AUTO, zone following
            its schedule, not crono, bound to today's preset, within its window).
    """

    preset: str
    levels: Program | None
    set_at: datetime | None
    expires_at: datetime | None
    applies: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Zone:
    """One present zone (thermostat probe).

    Attributes:
        id: the 3-digit unit id.
        name: ``NOME``, or ``"Zona <id>"`` when empty (personal: mask in logs).
        online: the unit answers on the bus (``STATO_SONDE``).
        temperature: ``TEMP_AMBIENTE``.
        humidity: ``UMIDITA``; ``None`` without a sensor.
        has_humidity_sensor: ``False`` when ``UMIDITA`` is 255, ``None`` if unknown.
        calling: ``ATTIVA``: the zone is calling for heating/cooling.
        setp: ``SETP_CORRENTE``: the zone's own mode (follow schedule, off,
            a fixed level, or off forced by the probe).
        offset: ``DELTA_SETP_CORRENTE`` (the user's -3..+3 correction).
        forced: ``SET_FORZ``: an external source forces the setpoint.
        forced_setpoint: ``SETP_TEMP_FORZATO`` (used as the base when ``forced``).
        controller_setpoint: ``ZONA...SET_POINT_TEMP``, the controller's own figure.
        level: the level in force now (override, schedule, crono or fixed).
        base: the level's plant temperature, or ``forced_setpoint`` when forced.
        target: ``base + trunc(offset)``, what the UI shows as the zone target.
        mode: the effective zone mode.
        preset: the effective zone preset.
        control_source: why ``level``/``target`` are what they are.
        hvac_action: heating/cooling (calling, by season), idle or off.
        override: the active season's override row, if any.
        next_change_at: the next instant ``level`` changes (7-day look-ahead);
            ``None`` when it never changes by time.
        schedule: both season schedules.
        has_vmc: ``DEUM_PRES``: a VMC serves this zone.
        identity: serial, firmware and custom id.
    """

    id: str
    name: str
    online: bool | None
    temperature: float | None
    humidity: float | None
    has_humidity_sensor: bool | None
    calling: bool | None
    setp: ZoneSetp | None
    offset: float | None
    forced: bool | None
    forced_setpoint: float | None
    controller_setpoint: float | None
    level: Level | None
    base: float | None
    target: float | None
    mode: ZoneMode | None
    preset: ZonePreset | None
    control_source: ControlSource | None
    hvac_action: HvacAction | None
    override: ZoneOverride | None
    next_change_at: datetime | None
    schedule: ZoneSchedule
    has_vmc: bool | None
    identity: UnitIdentity


@dataclass(frozen=True, slots=True, kw_only=True)
class VmcFan:
    """A VMC's fan.

    Attributes:
        kind: discrete speeds, or continuous ``step_min..step_max`` (global ``STEP == -1``).
        control: ``ABILITA_VENTOLA``: whether the fan may be set (missing = read-only).
        speed: the discrete speed (``COM_VENTILA``); ``None`` when continuous.
        value: ``COM_VENTILA`` as an int.
        step_min: ``STEP_MIN`` (default 0).
        step_max: ``STEP_MAX`` (default 100).
        show_labels: ``STEP_NO_VAL != 0``.
    """

    kind: FanKind
    control: Availability
    speed: FanSpeed | None
    value: int | None
    step_min: int
    step_max: int
    show_labels: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class VmcFreeCooling:
    """A VMC's free cooling.

    Attributes:
        level: global ``ABILITA_F_COOLING`` availability (missing = hidden).
        on: ``FREE_COOLING``.
        error: ``ERR_FREE_COOLING`` while on; ``None`` if ``on`` is unknown.
    """

    level: Availability
    on: bool | None
    error: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Vmc:
    """One present VMC / dehumidifier (``DEUM`` unit).

    Attributes:
        id: the 3-digit unit id.
        name: ``NOME``, or ``"Deum <id>"`` when empty (personal: mask in logs).
        online: the unit answers on the bus (``STATO_DEUM``).
        mode: ``ST_MODE``, the selected mode.
        forced_mode: ``ST_MODE_FORZATO`` (optional row).
        state: ``ST_STATO_DEUM``.
        effective_mode: ``forced_mode`` while FORCED (``mode`` when that row is
            missing), else ``mode``.
        running: the unit is running (RUNNING, or FORCED into a mode other than STOP).
        error: the state is ERROR.
        mode_availability: every mode's availability (a missing row hides that
            mode only).
        selectable_modes: the modes a user may select, in enum order.
        fan: the fan.
        free_cooling: free cooling.
        dehumidifying: ``ST_DEUMIDIFICA``.
        integration_active: ``ST_RAFF_RISC``.
        integration: cooling (summer) or heating (winter) while integrating.
        defrosting: ``COM_SBRINA`` (never an alarm).
        schedule_active: ``CICLO_ATTIVO``.
        compressor: ``COM_COMPR``.
        duty_cycle: ``ST_DUTY_CICLE``.
        inlet_air_temperature: ``TEMP_ARIA_INGRESSO``.
        renewal: ``COM_RINNOVO`` when renewal is enabled (global ``ABILITA_RINNOVO``).
        renewal_position: ``ST_SER_RINNO``.
        alarm_flags: the 8 ``ALLARM_*`` flags by key (see ``logic.VMC_ALARM_KEYS``).
        alarm_bitmask: ``ALLARME`` raw bitmask (diagnostic only, never an alarm).
        identity: serial, firmware and custom id.
    """

    id: str
    name: str
    online: bool | None
    mode: VmcMode | None
    forced_mode: VmcMode | None
    state: VmcState | None
    effective_mode: VmcMode | None
    running: bool | None
    error: bool | None
    mode_availability: Mapping[VmcMode, Availability]
    selectable_modes: tuple[VmcMode, ...]
    fan: VmcFan
    free_cooling: VmcFreeCooling
    dehumidifying: bool | None
    integration_active: bool | None
    integration: HvacAction | None
    defrosting: bool | None
    schedule_active: bool | None
    compressor: bool | None
    duty_cycle: int | None
    inlet_air_temperature: float | None
    renewal: int | None
    renewal_position: int | None
    alarm_flags: Mapping[str, bool | None]
    alarm_bitmask: int | None
    identity: UnitIdentity

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return _reduce_frozen(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class Actuator:
    """One present actuator (``ATTUATORE`` unit); raw values only (for now).

    Attributes:
        id: the 3-digit unit id.
        online: the unit answers on the bus (``STATO_ATT``).
        temperatures_raw: ``TEMP`` raw CSV.
        relays_raw: ``RELE`` raw CSV.
        identity: serial, firmware and custom id.
    """

    id: str
    online: bool | None
    temperatures_raw: str | None
    relays_raw: str | None
    identity: UnitIdentity


@dataclass(frozen=True, slots=True, kw_only=True)
class Fancoil:
    """One present fancoil (``AT9091`` unit); presence only (for now).

    Attributes:
        id: the 3-digit unit id.
        online: the unit answers on the bus (``STATO_AT9091``).
        availability: ``FANCOIL...ABILITA`` (missing = hidden).
    """

    id: str
    online: bool | None
    availability: Availability


@dataclass(frozen=True, slots=True, kw_only=True)
class Alarm:
    """One active alarm condition (a missing row is never an alarm).

    Attributes:
        id: stable id, for example ``"vmc:001:ALLARM_SONDA_RICIRCOLO"`` or ``"hub:bus"``.
        source: what raised it.
        device: the kind of device it concerns.
        unit: the unit id, if any.
        code: the controller's numeric code, if any.
        text: a short description (English for flags, the controller's text otherwise).
        first_seen: when this id became active (kept while it stays active, and
            across a dropout shorter than ``alarm_debounce`` once debounced).
    """

    id: str
    source: AlarmSource
    device: DeviceKind
    unit: str | None
    code: int | None
    text: str | None
    first_seen: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Health:
    """Controller health.

    Attributes:
        bus_ok: ``REHOM...ALLARME_BUS`` (1 = the bus is fine).
        watchdog_ok: not ``PROC...WATCHDOG_ALARM``.
        internet_ok: ``PROC...INTERNET_ACCESS``.
        remote_access_configured: a remote-access token is configured (presence only).
        heartbeat_at: the last ``PROC...WATCHDOG_MASTER`` frame (the controller
            pulses it every 60 s).
        heartbeat_ok: ``True`` while heartbeats arrive, ``False`` after
            ``heartbeat_stale_after`` without one while the WebSocket is live,
            ``None`` when unknown (WebSocket down, none seen yet, or the
            controller has no ``WATCHDOG_MASTER`` row).
        cpu_temperature: ``PROC...TEMP_RASPBERRY``.
        wifi_available: ``WIFI...AVAILABLE``.
        weather_service_ok: not ``METEO...ALLARM_METEO_KEY``.
    """

    bus_ok: bool | None
    watchdog_ok: bool | None
    internet_ok: bool | None
    remote_access_configured: bool
    heartbeat_at: datetime | None
    heartbeat_ok: bool | None
    cpu_temperature: float | None
    wifi_available: bool | None
    weather_service_ok: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class WeatherDay:
    """One day of the controller's own weather summary (``METEO`` rows, ``Unita`` 0..5).

    Attributes:
        index: 0 = today.
        condition_type: ``PREVISIONE`` type.
        icon_code: ``PREVISIONE`` icon.
        temp_min: ``RANGE_TEMP`` minimum.
        temp_max: ``RANGE_TEMP`` maximum.
        pressure: ``PRESSIONE``.
        wind_speed: ``VENTO`` speed.
        wind_bearing: ``VENTO`` direction (degrees).
    """

    index: int
    condition_type: int | None
    icon_code: int | None
    temp_min: float | None
    temp_max: float | None
    pressure: float | None
    wind_speed: float | None
    wind_bearing: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Weather:
    """The controller's weather rows (``METEO``); never an outdoor sensor reading.

    Attributes:
        updated_at: ``DATA``, when the controller last refreshed them.
        forecast_temperatures: ``TEMPERATURE``, a 9-value forecast series.
        humidity: ``UMIDITA``.
        days: per-day summaries, index ascending.
    """

    updated_at: datetime | None
    forecast_temperatures: tuple[float | None, ...]
    humidity: float | None
    days: tuple[WeatherDay, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ForecastEntry:
    """One ``METEO_DATA`` forecast item (the weather provider's JSON, parsed).

    Attributes:
        time: the item's ``dt``.
        temperature: ``main.temp``.
        feels_like: ``main.feels_like``.
        humidity: ``main.humidity``.
        dew_point: ``main.dew_point``.
        pressure: ``main.pressure``.
        wind_speed: ``wind.speed``.
        wind_bearing: ``wind.deg``.
        wind_gust: ``wind.gust``.
        precipitation_probability: ``pop``.
        clouds: ``clouds.all``.
        condition_id: ``weather[0].id``.
        icon: ``weather[0].icon``.
    """

    time: datetime
    temperature: float | None
    feels_like: float | None
    humidity: float | None
    dew_point: float | None
    pressure: float | None
    wind_speed: float | None
    wind_bearing: float | None
    wind_gust: float | None
    precipitation_probability: float | None
    clouds: float | None
    condition_id: int | None
    icon: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Forecast:
    """The latest ``METEO_DATA`` burst (WebSocket only; ``None`` in the state until one).

    Attributes:
        received_at: the first frame of the burst.
        entries: the items, by time.
    """

    received_at: datetime
    entries: tuple[ForecastEntry, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class RehomState:
    """Everything known about one controller at one instant (immutable).

    Two states compare equal when every field except ``synced_at`` and
    ``built_at`` is equal.  The state holds nothing that changes continuously
    with time: the client rebuilds and re-publishes it at ``next_change_at``.

    ``dataclasses.asdict()`` does not support the read-only mappings: use
    :meth:`as_dict`.  ``copy.deepcopy()`` and ``pickle`` work.

    Attributes:
        hub: the controller.
        plant: whole-house state.
        zones: present zones, by id ascending.
        vmcs: present VMCs, by id ascending.
        actuators: present actuators, by id ascending.
        fancoils: present fancoils, by id ascending.
        alarms: active alarms, sorted by id.
        alarms_debounced: the alarms debounced on both edges: active for at least
            ``alarm_debounce``, and kept (as last seen) until they have been
            inactive for ``alarm_debounce``; sorted by id.  An ended alarm held
            here is not in ``alarms``.
        health: controller health.
        weather: the controller's weather rows, ``None`` without any.
        forecast: the latest forecast burst, ``None`` before the first one.
        device_clock: the controller's time zone and clock skew used to build
            this state.
        plant_conf: ``/api/plant/conf/`` as opaque strings.
        next_change_at: the next instant the state changes by time alone
            (slot boundary, override start/end, alarm maturity or release,
            heartbeat deadline).
        synced_at: the stores' REST snapshot time when this state was built.
            A resync that finds no difference publishes no new state, so this
            can lag; ``RehomClient.last_synced_at`` is the latest snapshot time.
        built_at: when this state was built.
    """

    hub: Hub
    plant: Plant
    zones: Mapping[str, Zone]  # present zones only, ascending id
    vmcs: Mapping[str, Vmc]
    actuators: Mapping[str, Actuator]
    fancoils: Mapping[str, Fancoil]
    alarms: tuple[Alarm, ...]  # active now, sorted by id
    alarms_debounced: tuple[Alarm, ...]  # debounced on both edges (see above)
    health: Health
    weather: Weather | None
    forecast: Forecast | None
    device_clock: DeviceClock
    plant_conf: Mapping[str, str]
    next_change_at: datetime | None
    synced_at: datetime = field(compare=False)  # a no-diff resync must compare equal
    built_at: datetime = field(compare=False)

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return _reduce_frozen(self)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-serialisable copy (for diagnostics and snapshot tests).

        Nested models and mappings become dicts, tuples become lists, datetimes
        ISO strings (UTC, ``Z``), a ``StrEnum`` its value and an ``IntEnum`` its
        name (mapping keys too).  Names, the MAC and unit identities are
        included as they are: mask them before sharing.
        """
        result: dict[str, Any] = _jsonable(self)
        return result


@dataclass(frozen=True, slots=True, kw_only=True)
class StateUpdate:
    """One published change of :class:`RehomState`.

    Attributes:
        state: the new state.
        previous: the state before (``None`` only for ``SYNC``, the first one).
        reason: what triggered the update.
        at: when it was published.
        changed: interface and override record paths (``G.U.S.K``; override
            rows as ``PROG_OVERRIDE.<u>.<s>.<k>``) that were added, semantically
            changed or removed.
        removed: the paths of ``changed`` that no longer exist.
        conf_changed: plant-conf keys that changed.
        config_changed: ``/api/config/`` keys that changed (never ``LOCAL_TIME``).
        forecast_changed: ``METEO_DATA`` frames arrived.
    """

    state: RehomState
    previous: RehomState | None
    reason: UpdateReason
    at: datetime
    changed: frozenset[str] = frozenset()
    removed: frozenset[str] = frozenset()
    conf_changed: frozenset[str] = frozenset()
    config_changed: frozenset[str] = frozenset()
    forecast_changed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class HistorySample:
    """One ``/api/history/`` row.

    Attributes:
        time: aware UTC.
        local_time: naive device-local time, as returned by the controller.
        value: the value (history has 0.5 degC resolution for temperatures).
        rowid: the controller's row id (for de-duplication), if any.
    """

    time: datetime  # aware UTC
    local_time: datetime  # naive device time (as returned by the server)
    value: float
    rowid: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncStats:
    """Counters of one client since it was created (JSON-serialisable).

    Attributes:
        frames_received: WebSocket frames received.
        frames_ignored: frames that could not change anything (unknown domain,
            ``domotica``, commissioning, removes of unknown paths...).
        noop_updates: updates whose value was semantically unchanged.
        changed_updates: updates that changed a value.
        removes_coalesced: removes cancelled by an update of the same path within
            ``remove_coalesce_window``.
        removes_applied: removes applied.
        forecast_frames: ``METEO_DATA`` frames.
        buffered_max: most frames buffered during one sync.
        syncs: successful initial syncs (``connect()``).
        resyncs: successful resyncs.
        fallbacks: successful REST fallback snapshots (WebSocket down).
        reconnects: successful WebSocket reconnects.
        alive_failures: failed ``/api/alive/`` polls.
        notifications: published updates.
        sync_failures: failed syncs of any kind (resync or fallback).
        sync_failure_streak: consecutive failed syncs (0 after a success); the
            client retries a failed resync after ``min_resync_interval``,
            doubling up to ``resync_interval``.
        last_sync_error: the exception class name of the latest failure while
            ``sync_failure_streak > 0`` (for example ``"RehomResponseError"``
            when the API no longer parses), else ``None``.
        ws_idle_timeouts: WebSocket connections dropped after ``ws_idle_timeout``
            without a frame (a half-open socket).
    """

    frames_received: int = 0
    frames_ignored: int = 0
    noop_updates: int = 0
    changed_updates: int = 0
    removes_coalesced: int = 0
    removes_applied: int = 0
    forecast_frames: int = 0
    buffered_max: int = 0
    syncs: int = 0
    resyncs: int = 0
    fallbacks: int = 0
    reconnects: int = 0
    alive_failures: int = 0
    notifications: int = 0
    sync_failures: int = 0
    sync_failure_streak: int = 0
    last_sync_error: str | None = None
    ws_idle_timeouts: int = 0
