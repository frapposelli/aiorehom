"""Store -> model wiring: :class:`StateBuilder` turns the stores into a :class:`RehomState`.

The builder is the only place where the stores (builder B) meet the pure
logic (builder A).  It reads everything through :class:`~aiorehom.state.StoreView`
during one synchronous :meth:`StateBuilder.build` call and never keeps the view.

Between builds it keeps only three things: the alarm tracker (``first_seen``
per alarm id), the instant ``setpoint_mismatch`` last turned True, and a cache
of the parsed forecast burst.  Zone and schedule logic runs on naive device
wall time (``clock.local_now(now)``); every naive device time that reaches the
model is converted with ``clock.to_utc``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from .clock import DeviceClock
from .enums import (
    Availability,
    DeviceKind,
    HvacAction,
    Level,
    MasterMode,
    MasterSetPoint,
    Season,
    VmcMode,
)
from .logic import (
    GENERIC_ALARM_KINDS,
    LEVEL_TEMPERATURE_KEYS,
    VMC_ALARM_KEYS,
    WIDTH_ACTUATORS,
    WIDTH_FANCOILS,
    WIDTH_VMCS,
    WIDTH_ZONES,
    AlarmInputs,
    AlarmTracker,
    GenericAlarmRow,
    HeartbeatStatus,
    OverrideRow,
    ScheduleRow,
    TrackedAlarm,
    UnitPresence,
    WeekPrograms,
    ZoneInputs,
    compute_alarms,
    display_temperature,
    effective_setpoint,
    heartbeat_status,
    lock_state,
    master_preset,
    mode_availability,
    next_slot_boundary,
    nonempty,
    parse_availability,
    parse_device_timestamp,
    parse_forecast,
    parse_master_mode,
    parse_master_set_point,
    parse_season,
    parse_vmc_mode,
    parse_vmc_state,
    parse_weather,
    parse_zone_setp,
    present_units,
    selectable_modes,
    setpoint_mismatch,
    token_configured,
    vmc_fan,
    vmc_free_cooling,
    vmc_operation,
    week_programs,
    zone_effective,
    zone_humidity,
)
from .models import (
    Actuator,
    Alarm,
    DayProgram,
    Fancoil,
    Forecast,
    ForecastEntry,
    Health,
    Hub,
    Plant,
    RehomState,
    SeasonSchedule,
    UnitIdentity,
    Vmc,
    VmcFan,
    VmcFreeCooling,
    Weather,
    WeatherDay,
    Zone,
    ZoneOverride,
    ZoneSchedule,
)
from .state import ForecastSnapshot, RowView, StoreView
from .store import OVERRIDE_GRUPPO
from .values import parse_bool01, parse_float, parse_int

__all__ = ["StateBuilder"]

_ONE_SECOND: Final = timedelta(seconds=1)
_WEEKDAYS: Final = range(7)
#: ``METEO`` rows that are never read (personal: latitude/longitude).
_METEO_NEVER_READ: Final = frozenset({"POSIZIONE"})
#: Plant-conf flags that mean an installer session is open.
_INSTALLER_FLAGS: Final = ("CONFIGURA_ON", "CONF_ZONE_ON", "CONF_DEUM_ON")
#: VMC integration direction while ``ST_RAFF_RISC`` is on.
_SEASON_INTEGRATION: Final[Mapping[Season, HvacAction]] = {
    Season.SUMMER: HvacAction.COOLING,
    Season.WINTER: HvacAction.HEATING,
}


def _alarm(entry: TrackedAlarm) -> Alarm:
    condition = entry.condition
    return Alarm(
        id=condition.id,
        source=condition.source,
        device=condition.device,
        unit=condition.unit,
        code=condition.code,
        text=condition.text,
        first_seen=entry.first_seen,
    )


def _to_utc(clock: DeviceClock, local: datetime | None) -> datetime | None:
    """``clock.to_utc(local)``; ``None`` for ``None`` or an out-of-range wire value."""
    if local is None:
        return None
    try:
        return clock.to_utc(local)
    except (ValueError, OverflowError):
        return None


def _plus_one_second(when: datetime | None) -> datetime | None:
    if when is None:
        return None
    try:
        return when + _ONE_SECOND
    except OverflowError:
        return None


def _config_text(config: Mapping[str, Any], key: str) -> str | None:
    value = config.get(key)
    return nonempty(value) if isinstance(value, str) else None


def _unit_fields(rows: Iterable[RowView]) -> dict[str, str]:
    """``{Key: Valore}`` of the rows with ``SubUni ""`` (the unit's own fields)."""
    return {row.key: row.value for row in rows if row.subuni == ""}


def _identity(fields: Mapping[str, str]) -> UnitIdentity:
    return UnitIdentity(
        serial=nonempty(fields.get("MATRICOLA")),
        firmware=nonempty(fields.get("VERSIONE_FW")),
        custom_id=nonempty(fields.get("CUSTOM_ID")),
    )


def _season_schedule(week: WeekPrograms) -> SeasonSchedule:
    return SeasonSchedule(
        days=tuple(
            DayProgram(
                weekday=day, preset=week.index_map.get(day), levels=week.program_map.get(day)
            )
            for day in _WEEKDAYS
        ),
        crono=week.crono,
        source=week.source,
    )


def _forecast_entries(snapshot: ForecastSnapshot) -> tuple[ForecastEntry, ...]:
    entries: list[ForecastEntry] = []
    for item in parse_forecast(snapshot.items):
        try:
            when = datetime.fromtimestamp(item.epoch, UTC)
        except (ValueError, OverflowError, OSError):
            continue  # an epoch the platform cannot represent is wire garbage
        entries.append(
            ForecastEntry(
                time=when,
                temperature=item.temperature,
                feels_like=item.feels_like,
                humidity=item.humidity,
                dew_point=item.dew_point,
                pressure=item.pressure,
                wind_speed=item.wind_speed,
                wind_bearing=item.wind_bearing,
                wind_gust=item.wind_gust,
                precipitation_probability=item.pop,
                clouds=item.clouds,
                condition_id=item.condition_id,
                icon=item.icon,
            )
        )
    return tuple(entries)


@dataclass(frozen=True, slots=True, kw_only=True)
class _PlantContext:
    """Plant values every zone needs (the shared part of :class:`ZoneInputs`)."""

    mode: MasterMode | None
    set_point: MasterSetPoint | None
    season: Season | None
    is_crono: bool
    temperatures: Mapping[Level, float | None]
    now_local: datetime


class StateBuilder:
    """Builds :class:`RehomState` from the stores; keeps alarm/mismatch trackers between builds."""

    def __init__(self, *, alarm_debounce: timedelta, heartbeat_stale_after: timedelta) -> None:
        if heartbeat_stale_after <= timedelta(0):
            raise ValueError("heartbeat_stale_after must be positive")
        self._alarm_debounce = alarm_debounce
        self._heartbeat_stale_after = heartbeat_stale_after
        self._tracker = AlarmTracker(alarm_debounce)  # ValueError below 30 s
        self._mismatch_since: datetime | None = None
        self._forecast_source: ForecastSnapshot | None = None
        self._forecast_entries: tuple[ForecastEntry, ...] = ()

    def build(self, view: StoreView, *, now: datetime, clock: DeviceClock) -> RehomState:
        """Return the state at ``now`` (aware UTC).  Synchronous; must not keep ``view``."""
        local = clock.local_now(now)

        def rehom(key: str) -> str | None:
            return view.value("REHOM", "", "", key)

        # -- plant ----------------------------------------------------------
        season = parse_season(rehom("STAGIONE"))
        mode = parse_master_mode(rehom("MODO"))
        set_point = parse_master_set_point(rehom("SET_POINT"))
        preset = master_preset(mode, set_point)
        temperatures: dict[Level, float | None] = {
            level: parse_float(rehom(key)) for level, key in LEVEL_TEMPERATURE_KEYS.items()
        }
        controller_setpoint = parse_float(rehom("SET_POINT_TEMP"))
        mismatch = setpoint_mismatch(
            mode=mode,
            set_point=set_point,
            temperatures=temperatures,
            controller_setpoint=controller_setpoint,
        )
        if mismatch is True:
            if self._mismatch_since is None:
                self._mismatch_since = now
        else:
            self._mismatch_since = None
        lock = lock_state(
            [row.value for row in view.rows_with_key("WEBSERVER")],
            [row.value for row in view.override_rows() if row.key == "WEBSERVER"],
        )

        # -- presence ---------------------------------------------------------
        zone_presence = present_units(
            rehom("PRESENZA_SONDE"), rehom("STATO_SONDE"), width=WIDTH_ZONES
        )
        vmc_presence = present_units(rehom("PRESENZA_DEUM"), rehom("STATO_DEUM"), width=WIDTH_VMCS)
        actuator_presence = present_units(
            rehom("PRESENZA_ATT"), rehom("STATO_ATT"), width=WIDTH_ACTUATORS
        )
        fancoil_presence = present_units(
            rehom("PRESENZA_AT9091"), rehom("STATO_AT9091"), width=WIDTH_FANCOILS
        )

        # -- zones --------------------------------------------------------------
        overrides_by_zone: dict[str, list[OverrideRow]] = {}
        for row in view.override_rows():
            if row.gruppo == OVERRIDE_GRUPPO:
                overrides_by_zone.setdefault(row.unita, []).append(
                    OverrideRow(
                        subuni=row.subuni,
                        key=row.key,
                        value=row.value,
                        set_at_raw=row.impostazione,
                        expires_at_raw=row.scadenza,
                    )
                )
        context = _PlantContext(
            mode=mode,
            set_point=set_point,
            season=season,
            is_crono=lock.is_crono,
            temperatures=MappingProxyType(temperatures),
            now_local=local,
        )
        zones: dict[str, Zone] = {}
        instants: list[datetime] = []
        for unit, presence in zone_presence.items():
            zone, zone_instants = self._zone(
                view,
                unit,
                presence=presence,
                plant=context,
                override_rows=overrides_by_zone.get(unit, []),
                clock=clock,
            )
            zones[unit] = zone
            instants.extend(zone_instants)
        callings = [zone.calling for zone in zones.values()]
        demand: bool | None
        if any(calling is True for calling in callings):
            demand = True
        elif all(calling is None for calling in callings):
            demand = None
        else:
            demand = False
        installer = [parse_bool01(view.plant_conf.get(key)) for key in _INSTALLER_FLAGS]
        installer_session: bool | None
        if any(flag is True for flag in installer):
            installer_session = True
        elif all(flag is None for flag in installer):
            installer_session = None
        else:
            installer_session = False
        first_zone = zones.get("001")

        plant = Plant(
            season=season,
            season_forced=parse_bool01(rehom("FORZATURA_STAGIONE")),
            conf_season=parse_season(view.plant_conf.get("stagione")),
            mode=mode,
            set_point=set_point,
            preset=preset,
            temperature_off=temperatures[Level.OFF],
            temperature_economy=temperatures[Level.ECONOMY],
            temperature_pre_comfort=temperatures[Level.PRE_COMFORT],
            temperature_comfort=temperatures[Level.COMFORT],
            temperature_eco_aux=parse_float(rehom("TEMP_ECO")),
            display_temperature=display_temperature(preset, temperatures),
            controller_setpoint=controller_setpoint,
            effective_setpoint=effective_setpoint(mode, controller_setpoint),
            setpoint_mismatch=mismatch,
            setpoint_mismatch_since=self._mismatch_since,
            predictive=parse_bool01(rehom("ALG_ATTIVO")),
            read_only=parse_bool01(rehom("TERMO_READONLY")),
            lock=lock.state,
            is_crono=lock.is_crono,
            serial_down=lock.serial_down,
            server_on_requested=parse_bool01(rehom("SERVER_ON")),
            demand=demand,
            current_temperature=None if first_zone is None else first_zone.temperature,
            domotica_enabled=parse_bool01(rehom("ABILITA_DOMOTICA")) is True,
            installer_session=installer_session,
            installer_activity_at=view.last_commissioning_at,
            vmc_type_code=nonempty(view.value("DEUM", "", "", "TIPO")),
        )

        # -- VMCs, actuators, fancoils --------------------------------------------
        vmc_globals = _unit_fields(view.rows("DEUM", ""))
        availability = mode_availability(vmc_globals)
        selectable = selectable_modes(availability)
        vmcs: dict[str, Vmc] = {}
        vmc_fields: dict[str, dict[str, str]] = {}
        for unit, presence in vmc_presence.items():
            fields = _unit_fields(view.rows("DEUM", unit))
            vmc_fields[unit] = fields
            vmcs[unit] = self._vmc(
                unit,
                presence=presence,
                fields=fields,
                globals_=vmc_globals,
                availability=availability,
                selectable=selectable,
                season=season,
            )
        actuators: dict[str, Actuator] = {}
        for unit, presence in actuator_presence.items():
            fields = _unit_fields(view.rows("ATTUATORE", unit))
            actuators[unit] = Actuator(
                id=unit,
                online=presence.online,
                temperatures_raw=fields.get("TEMP"),
                relays_raw=fields.get("RELE"),
                identity=_identity(fields),
            )
        fancoils = {
            unit: Fancoil(
                id=unit,
                online=presence.online,
                availability=parse_availability(
                    view.value("FANCOIL", unit, "", "ABILITA"), missing=Availability.HIDDEN
                ),
            )
            for unit, presence in fancoil_presence.items()
        }

        # -- weather and forecast ----------------------------------------------------
        scalars: dict[str, str | None] = {}
        days: dict[str, dict[str, str | None]] = {}
        for row in view.rows("METEO"):
            if row.subuni != "" or row.key in _METEO_NEVER_READ:
                continue
            if row.unita == "":
                scalars[row.key] = row.value
            else:
                days.setdefault(row.unita, {})[row.key] = row.value
        weather_info = parse_weather(scalars, days)
        weather = (
            None
            if weather_info is None
            else Weather(
                updated_at=_to_utc(clock, weather_info.updated_local),
                forecast_temperatures=weather_info.forecast_temperatures,
                humidity=weather_info.humidity,
                days=tuple(
                    WeatherDay(
                        index=day.index,
                        condition_type=day.condition_type,
                        icon_code=day.icon_code,
                        temp_min=day.temp_min,
                        temp_max=day.temp_max,
                        pressure=day.pressure,
                        wind_speed=day.wind_speed,
                        wind_bearing=day.wind_bearing,
                    )
                    for day in weather_info.days
                ),
            )
        )
        forecast = self._forecast(view.forecast)

        # -- alarms and health ---------------------------------------------------------
        remote_token = view.value("CONFIG", "", "", "REMOTE_TOKEN")
        alarm_inputs = AlarmInputs(
            presence={
                DeviceKind.ZONE: zone_presence,
                DeviceKind.VMC: vmc_presence,
                DeviceKind.ACTUATOR: actuator_presence,
                DeviceKind.FANCOIL: fancoil_presence,
            },
            vmc_flags={
                unit: {key: fields.get(key) for key in VMC_ALARM_KEYS}
                for unit, fields in vmc_fields.items()
            },
            vmc_free_cooling={
                unit: (fields.get("FREE_COOLING"), fields.get("ERR_FREE_COOLING"))
                for unit, fields in vmc_fields.items()
            },
            generic=tuple(
                GenericAlarmRow(
                    gruppo=row.gruppo, unita=row.unita, subuni=row.subuni, value=row.value
                )
                for row in view.rows_with_key("GENERIC_ALARM")
                if row.gruppo in GENERIC_ALARM_KINDS
            ),
            process=tuple(
                (row.unita, row.value)
                for row in view.rows_with_key("PROCESS_STATE")
                if row.gruppo == "REHOM"
            ),
            bus_raw=rehom("ALLARME_BUS"),
            watchdog_raw=view.value("PROC", "", "", "WATCHDOG_ALARM"),
            internet_raw=view.value("PROC", "", "", "INTERNET_ACCESS"),
            remote_token_raw=remote_token,
            weather_key_raw=view.value("METEO", "", "", "ALLARM_METEO_KEY"),
            lock=lock.state,
        )
        alarm_view = self._tracker.update(compute_alarms(alarm_inputs), now)
        alarms = tuple(_alarm(entry) for entry in alarm_view.active)
        # Debounced on both edges: an ended alarm stays here for the window.
        alarms_debounced = tuple(_alarm(entry) for entry in alarm_view.debounced)
        heartbeat_row = view.row("PROC", "", "", "WATCHDOG_MASTER")
        heartbeat_at = None if heartbeat_row is None else heartbeat_row.frame_at
        if heartbeat_row is None:
            # A controller without the (optional) heartbeat row is never "stalled":
            # the heartbeat is unknown and sets no deadline.
            heartbeat = HeartbeatStatus(ok=None, deadline=None)
        else:
            heartbeat = heartbeat_status(
                heartbeat_at=heartbeat_at,
                live_since=view.live_since,
                now=now,
                stale_after=self._heartbeat_stale_after,
            )
        watchdog_alarm = parse_bool01(view.value("PROC", "", "", "WATCHDOG_ALARM"))
        health = Health(
            bus_ok=parse_bool01(rehom("ALLARME_BUS")),
            watchdog_ok=None if watchdog_alarm is None else not watchdog_alarm,
            internet_ok=parse_bool01(view.value("PROC", "", "", "INTERNET_ACCESS")),
            remote_access_configured=token_configured(remote_token),
            heartbeat_at=heartbeat_at,
            heartbeat_ok=heartbeat.ok,
            cpu_temperature=parse_float(view.value("PROC", "", "", "TEMP_RASPBERRY")),
            wifi_available=parse_bool01(view.value("WIFI", "", "", "AVAILABLE")),
            weather_service_ok=None if weather_info is None else weather_info.service_ok,
        )

        # -- next time-driven change ---------------------------------------------------
        boundary_local = next_slot_boundary(local)
        boundary = _to_utc(clock, boundary_local)
        if boundary is None or boundary <= now:
            # DST fold (a repeated hour) or an unrepresentable instant: step from now
            boundary = now + (boundary_local - local)
        candidates = [boundary, *instants]
        if alarm_view.next_maturity is not None:
            candidates.append(alarm_view.next_maturity)
        if heartbeat.deadline is not None:
            candidates.append(heartbeat.deadline)
        next_change_at = min(when for when in candidates if when > now)

        version = view.alive.get("version")
        mac = nonempty(view.value("WEBSERVER", "", "", "MacAddress"))
        return RehomState(
            hub=Hub(
                mac=None if mac is None else mac.lower(),
                web_version=version if isinstance(version, str) else None,
                controller_version=nonempty(rehom("VER_SOFT")),
                board=_config_text(view.config, "BOARD"),
                platform=_config_text(view.config, "PLATFORM"),
                timezone=_config_text(view.config, "TIMEZONE"),
            ),
            plant=plant,
            zones=MappingProxyType(zones),
            vmcs=MappingProxyType(vmcs),
            actuators=MappingProxyType(actuators),
            fancoils=MappingProxyType(fancoils),
            alarms=alarms,
            alarms_debounced=alarms_debounced,
            health=health,
            weather=weather,
            forecast=forecast,
            device_clock=clock,
            plant_conf=MappingProxyType(dict(view.plant_conf)),
            next_change_at=next_change_at,
            synced_at=view.synced_at,
            built_at=now,
        )

    # -- per-unit helpers ------------------------------------------------------------

    def _zone(
        self,
        view: StoreView,
        unit: str,
        *,
        presence: UnitPresence,
        plant: _PlantContext,
        override_rows: list[OverrideRow],
        clock: DeviceClock,
    ) -> tuple[Zone, list[datetime]]:
        """One zone, plus the UTC instants (override start/expiry) it may change at."""
        fields: dict[str, str] = {}
        mirror: list[ScheduleRow] = []
        for row in view.rows("ZONA", unit):
            if row.subuni == "":
                fields[row.key] = row.value
            elif row.key.startswith("PROG_"):
                mirror.append(ScheduleRow(subuni=row.subuni, key=row.key, value=row.value))
        prog = [
            ScheduleRow(subuni=row.subuni, key=row.key, value=row.value)
            for row in view.rows("PROG", unit)
        ]
        winter = week_programs(prog, mirror, Season.WINTER)
        summer = week_programs(prog, mirror, Season.SUMMER)
        week = (
            {Season.WINTER: winter, Season.SUMMER: summer}.get(plant.season)
            if plant.season
            else None
        )
        humidity, has_sensor = zone_humidity(fields.get("UMIDITA"))
        forced = parse_bool01(fields.get("SET_FORZ"))
        inputs = ZoneInputs(
            mode=plant.mode,
            set_point=plant.set_point,
            season=plant.season,
            is_crono=plant.is_crono,
            setp=parse_zone_setp(fields.get("SETP_CORRENTE")),
            forced=forced,
            forced_setpoint=parse_float(fields.get("SETP_TEMP_FORZATO")),
            offset_raw=fields.get("DELTA_SETP_CORRENTE"),
            calling=parse_bool01(fields.get("ATTIVA")),
            temperatures=plant.temperatures,
            week=week,
            override_rows=tuple(override_rows),
            now_local=plant.now_local,
        )
        effective = zone_effective(inputs)
        override: ZoneOverride | None = None
        if effective.override is not None:
            state = effective.override
            override = ZoneOverride(
                preset=state.row.subuni,
                levels=state.program,
                set_at=_to_utc(clock, state.set_at),
                expires_at=_to_utc(clock, state.expires_at),
                applies=state.applies,
            )
        instants: list[datetime] = []
        for override_row in override_rows:
            set_at = _to_utc(clock, parse_device_timestamp(override_row.set_at_raw))
            expiry = _plus_one_second(
                _to_utc(clock, parse_device_timestamp(override_row.expires_at_raw))
            )
            instants.extend(when for when in (set_at, expiry) if when is not None)
        next_change_at = _to_utc(clock, effective.next_change_local)
        if next_change_at is not None:
            instants.append(next_change_at)
        zone = Zone(
            id=unit,
            name=nonempty(fields.get("NOME")) or f"Zona {unit}",
            online=presence.online,
            temperature=parse_float(fields.get("TEMP_AMBIENTE")),
            humidity=humidity,
            has_humidity_sensor=has_sensor,
            calling=inputs.calling,
            setp=inputs.setp,
            offset=effective.offset,
            forced=forced,
            forced_setpoint=inputs.forced_setpoint,
            controller_setpoint=parse_float(fields.get("SET_POINT_TEMP")),
            level=effective.level,
            base=effective.base,
            target=effective.target,
            mode=effective.mode,
            preset=effective.preset,
            control_source=effective.control_source,
            hvac_action=effective.hvac_action,
            override=override,
            next_change_at=next_change_at,
            schedule=ZoneSchedule(winter=_season_schedule(winter), summer=_season_schedule(summer)),
            has_vmc=parse_bool01(fields.get("DEUM_PRES")),
            identity=_identity(fields),
        )
        return zone, instants

    @staticmethod
    def _vmc(
        unit: str,
        *,
        presence: UnitPresence,
        fields: Mapping[str, str],
        globals_: Mapping[str, str],
        availability: Mapping[VmcMode, Availability],
        selectable: tuple[VmcMode, ...],
        season: Season | None,
    ) -> Vmc:
        mode = parse_vmc_mode(fields.get("ST_MODE"))
        forced_mode = parse_vmc_mode(fields.get("ST_MODE_FORZATO"))
        vmc_state = parse_vmc_state(fields.get("ST_STATO_DEUM"))
        operation = vmc_operation(mode=mode, forced_mode=forced_mode, state=vmc_state)
        fan = vmc_fan(
            step_raw=globals_.get("STEP"),
            value_raw=fields.get("COM_VENTILA"),
            control_raw=fields.get("ABILITA_VENTOLA"),
            step_min_raw=fields.get("STEP_MIN"),
            step_max_raw=fields.get("STEP_MAX"),
            step_no_val_raw=fields.get("STEP_NO_VAL"),
        )
        free_cooling = vmc_free_cooling(
            level_raw=globals_.get("ABILITA_F_COOLING"),
            on_raw=fields.get("FREE_COOLING"),
            error_raw=fields.get("ERR_FREE_COOLING"),
        )
        integration_active = parse_bool01(fields.get("ST_RAFF_RISC"))
        integration: HvacAction | None = None
        if integration_active is True:
            integration = _SEASON_INTEGRATION.get(season) if season is not None else None
        renewal = (
            parse_int(fields.get("COM_RINNOVO"))
            if parse_bool01(globals_.get("ABILITA_RINNOVO")) is True
            else None
        )
        return Vmc(
            id=unit,
            name=nonempty(fields.get("NOME")) or f"Deum {unit}",
            online=presence.online,
            mode=mode,
            forced_mode=forced_mode,
            state=vmc_state,
            effective_mode=operation.effective_mode,
            running=operation.running,
            error=operation.error,
            mode_availability=MappingProxyType(dict(availability)),
            selectable_modes=selectable,
            fan=VmcFan(
                kind=fan.kind,
                control=fan.control,
                speed=fan.speed,
                value=fan.value,
                step_min=fan.step_min,
                step_max=fan.step_max,
                show_labels=fan.show_labels,
            ),
            free_cooling=VmcFreeCooling(
                level=free_cooling.level, on=free_cooling.on, error=free_cooling.error
            ),
            dehumidifying=parse_bool01(fields.get("ST_DEUMIDIFICA")),
            integration_active=integration_active,
            integration=integration,
            defrosting=parse_bool01(fields.get("COM_SBRINA")),
            schedule_active=parse_bool01(fields.get("CICLO_ATTIVO")),
            compressor=parse_bool01(fields.get("COM_COMPR")),
            duty_cycle=parse_int(fields.get("ST_DUTY_CICLE")),
            inlet_air_temperature=parse_float(fields.get("TEMP_ARIA_INGRESSO")),
            renewal=renewal,
            renewal_position=parse_int(fields.get("ST_SER_RINNO")),
            alarm_flags=MappingProxyType(
                {key: parse_bool01(fields.get(key)) for key in VMC_ALARM_KEYS}
            ),
            alarm_bitmask=parse_int(fields.get("ALLARME")),
            identity=_identity(fields),
        )

    def _forecast(self, snapshot: ForecastSnapshot | None) -> Forecast | None:
        """The forecast burst as a model (parsed once per burst snapshot)."""
        if snapshot is None:
            return None
        if snapshot is not self._forecast_source:
            self._forecast_source = snapshot
            self._forecast_entries = _forecast_entries(snapshot)
        return Forecast(received_at=snapshot.received_at, entries=self._forecast_entries)
