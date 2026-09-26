"""Store -> model wiring on small synthetic stores."""

from __future__ import annotations

import copy
import json
import pickle
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aiorehom.builder import StateBuilder
from aiorehom.clock import DeviceClock
from aiorehom.enums import (
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
    VmcMode,
    VmcState,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)
from aiorehom.models import RehomState
from aiorehom.state import Snapshot, StoreSet, parse_record_rows

from .conftest import rec

ROME = ZoneInfo("Europe/Rome")
ROME_CLOCK = DeviceClock(tz=ROME, tz_name="Europe/Rome")
#: Friday 2026-09-25 12:22:00 local (CEST, UTC+2).
T0 = datetime(2026, 9, 25, 10, 22, 0, tzinfo=UTC)
MONO0 = 1000.0
PROGRAM_1 = ",".join(["1"] * 24 + ["3"] * 20 + ["0"] * 4)  # COMFORT 12:00-22:00
CRONO_99 = ",".join(["2"] * 48)


def _vector(ones: Iterable[int], size: int) -> str:
    chosen = set(ones)
    return ",".join("1" if i in chosen else "0" for i in range(size))


def house() -> list[dict[str, Any]]:
    """A synthetic plant: zones 001 (+ stray 004/005/000), VMC 001 (+ stray 1/2/000/003)."""
    return [
        rec("REHOM", "", "", "STAGIONE", "1"),
        rec("REHOM", "", "", "FORZATURA_STAGIONE", "0"),
        rec("REHOM", "", "", "MODO", "1"),
        rec("REHOM", "", "", "SET_POINT", "3"),
        rec("REHOM", "", "", "TEMP_OFF", "38"),
        rec("REHOM", "", "", "TEMP_MAN", "29"),
        rec("REHOM", "", "", "TEMP_PRE", "26"),
        rec("REHOM", "", "", "TEMP_COM", "24"),
        rec("REHOM", "", "", "TEMP_ECO", "16"),
        rec("REHOM", "", "", "SET_POINT_TEMP", "26"),
        rec("REHOM", "", "", "ALG_ATTIVO", "0"),
        rec("REHOM", "", "", "TERMO_READONLY", "0"),
        rec("REHOM", "", "", "SERVER_ON", "1"),
        rec("REHOM", "", "", "VER_SOFT", "3.1.0.12"),
        rec("REHOM", "", "", "ABILITA_DOMOTICA", "1"),
        rec("REHOM", "", "", "PRESENZA_SONDE", _vector({0, 1}, 24)),
        rec("REHOM", "", "", "STATO_SONDE", _vector({0}, 24)),
        rec("REHOM", "", "", "PRESENZA_DEUM", "1,0,0"),
        rec("REHOM", "", "", "STATO_DEUM", "1,0,0"),
        rec("REHOM", "", "", "PRESENZA_ATT", "0,1,0"),
        rec("REHOM", "", "", "STATO_ATT", "0,1,0"),
        rec("REHOM", "", "", "PRESENZA_AT9091", _vector({2}, 24)),
        rec("REHOM", "", "", "STATO_AT9091", _vector({2}, 24)),
        rec("REHOM", "", "", "ALLARME_BUS", "1"),
        rec("REHOM", "", "", "WEBSERVER", "1"),
        rec("REHOM", "", "", "WEBSERVER_OLD", "0"),
        rec("REHOM", "14", "", "PROCESS_STATE", "Temperatura cpu 64.5"),
        rec("REHOM", "114", "", "PROCESS_STATE", ""),
        # zone 001: present, online, calling, offset -1, schedule in PROG
        rec("ZONA", "001", "", "NOME", "Zona 001"),
        rec("ZONA", "001", "", "TEMP_AMBIENTE", "21.5"),
        rec("ZONA", "001", "", "UMIDITA", "48"),
        rec("ZONA", "001", "", "ATTIVA", "1"),
        rec("ZONA", "001", "", "SETP_CORRENTE", "0"),
        rec("ZONA", "001", "", "DELTA_SETP_CORRENTE", "-1"),
        rec("ZONA", "001", "", "SET_FORZ", "0"),
        rec("ZONA", "001", "", "SETP_TEMP_FORZATO", "24.0"),
        rec("ZONA", "001", "", "SET_POINT_TEMP", "26"),
        rec("ZONA", "001", "", "DEUM_PRES", "1"),
        rec("ZONA", "001", "", "MATRICOLA", "S-0001"),
        rec("ZONA", "001", "", "VERSIONE_FW", "7"),
        rec("ZONA", "001", "", "CUSTOM_ID", " "),
        # zone 002: present, offline, no name, no humidity sensor, winter schedule in mirror
        rec("ZONA", "002", "", "TEMP_AMBIENTE", "22"),
        rec("ZONA", "002", "", "UMIDITA", "255"),
        rec("ZONA", "002", "", "ATTIVA", "0"),
        rec("ZONA", "002", "", "SETP_CORRENTE", "4"),
        # stray zone rows: never modelled
        rec("ZONA", "004", "", "NOME", "Stray 004"),
        rec("ZONA", "005", "", "TEMP_AMBIENTE", "19"),
        rec("ZONA", "000", "2", "ICO_IMG", "sun"),
        rec("ZONA", "001", "1", "ICO_IMG", "fan_on"),  # a sub-row that is not a schedule
        rec("PROG", "001", "1", "PROG_GIORNO_ESTATE", PROGRAM_1),
        rec("PROG", "001", "99", "PROG_GIORNO_ESTATE", CRONO_99),
        *(rec("PROG", "001", str(day), "PROG_SETT_ESTATE", "1") for day in range(7)),
        rec("ZONA", "002", "5", "PROG_SETT_INVERNO", "2"),
        rec("ZONA", "002", "2", "PROG_GIORNO_INVERNO", ",".join(["2"] * 48)),
        # VMC globals and unit 001
        rec("DEUM", "", "", "TIPO", "6"),
        rec("DEUM", "", "", "STEP", "0"),
        rec("DEUM", "", "", "ABILITA_STOP", "2"),
        rec("DEUM", "", "", "DEUM_ARIA_NEUTRA", "2"),
        rec("DEUM", "", "", "ABILITA_INTEGR_FREDDO", "1"),
        rec("DEUM", "", "", "ABILITA_VENTILA", "2"),
        rec("DEUM", "", "", "ABILITA_F_COOLING", "2"),
        rec("DEUM", "", "", "ABILITA_RINNOVO", "1"),
        rec("DEUM", "001", "", "NOME", "VMC 001"),
        rec("DEUM", "001", "", "ST_MODE", "1"),
        rec("DEUM", "001", "", "ST_STATO_DEUM", "1"),
        rec("DEUM", "001", "", "COM_VENTILA", "2"),
        rec("DEUM", "001", "", "ABILITA_VENTOLA", "2"),
        rec("DEUM", "001", "", "STEP_MIN", "0"),
        rec("DEUM", "001", "", "STEP_MAX", "100"),
        rec("DEUM", "001", "", "STEP_NO_VAL", "0"),
        rec("DEUM", "001", "", "FREE_COOLING", "1"),
        rec("DEUM", "001", "", "ERR_FREE_COOLING", "0"),
        rec("DEUM", "001", "", "ST_DEUMIDIFICA", "1"),
        rec("DEUM", "001", "", "ST_RAFF_RISC", "1"),
        rec("DEUM", "001", "", "COM_SBRINA", "0"),
        rec("DEUM", "001", "", "CICLO_ATTIVO", "0"),
        rec("DEUM", "001", "", "COM_COMPR", "1"),
        rec("DEUM", "001", "", "ST_DUTY_CICLE", "40"),
        rec("DEUM", "001", "", "TEMP_ARIA_INGRESSO", "18.5"),
        rec("DEUM", "001", "", "COM_RINNOVO", "40"),
        rec("DEUM", "001", "", "ST_SER_RINNO", "35"),
        rec("DEUM", "001", "", "ALLARME", "248"),
        rec("DEUM", "001", "", "ALLARM_SONDA_RICIRCOLO", "1"),
        rec("DEUM", "001", "", "ALLARM_SBRINAMENTO", "0"),
        rec("DEUM", "001", "", "MATRICOLA", "D-1"),
        # stray VMC rows: never modelled
        rec("DEUM", "1", "", "ST_MODE", "8"),
        rec("DEUM", "2", "", "ST_MODE", "5"),
        rec("DEUM", "000", "", "STEP_NO_VAL", "0"),
        rec("DEUM", "003", "", "ST_MODE", "1"),
        rec("ATTUATORE", "002", "", "TEMP", "100,100,100"),
        rec("ATTUATORE", "002", "", "RELE", "100,100,00000000000,00,000"),
        rec("ATTUATORE", "002", "", "VERSIONE_FW", "3"),
        rec("FANCOIL", "003", "", "ABILITA", "2"),
        rec("METEO", "", "", "DATA", "2026-09-25 12:04:01"),
        rec("METEO", "", "", "TEMPERATURE", "21.62,21.02"),
        rec("METEO", "", "", "UMIDITA", "59.625"),
        rec("METEO", "", "", "ALLARM_METEO_KEY", "0"),
        rec("METEO", "", "", "POSIZIONE", "45.0000000,10.0000000"),
        rec("METEO", "0", "", "PREVISIONE", "0,1"),
        rec("METEO", "0", "", "RANGE_TEMP", "15.5,21.84"),
        rec("METEO", "0", "", "PRESSIONE", "1020.375"),
        rec("METEO", "0", "", "VENTO", "3.5,112.25"),
        rec("PROC", "", "", "WATCHDOG_MASTER", "0"),
        rec("PROC", "", "", "WATCHDOG_ALARM", "0"),
        rec("PROC", "", "", "INTERNET_ACCESS", "1"),
        rec("PROC", "", "", "TEMP_RASPBERRY", "64.5"),
        rec("WIFI", "", "", "AVAILABLE", "0"),
        rec("WEBSERVER", "", "", "MacAddress", " 02:00:00:AB:CD:EF "),
        rec("CONFIG", "", "", "REMOTE_TOKEN", "tok-123"),
    ]


CONFIG: dict[str, Any] = {
    "TIMEZONE": "Europe/Rome",
    "LOCAL_TIME": "2026-09-25T12:22:00",
    "BOARD": "pi",
    "PLATFORM": "arm32",
    "VERSION": 3,
}
PLANT_CONF = {"stagione": "1", "CONFIGURA_ON": "0", "CONF_ZONE_ON": "1"}


def stores(
    rows: list[dict[str, Any]] | None = None,
    *,
    overrides: list[dict[str, Any]] | None = None,
    plant_conf: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    alive: object = None,
    at: datetime = T0,
) -> StoreSet:
    store = StoreSet()
    snapshot = Snapshot(
        interface=tuple(parse_record_rows(house() if rows is None else rows, path="/i/")),
        overrides=tuple(parse_record_rows(overrides or [], path="/o/", overrides=True)),
        plant_conf=dict(PLANT_CONF if plant_conf is None else plant_conf),
        config=dict(CONFIG if config is None else config),
        config_received_at=at,
    )
    store.replace(snapshot, at)
    store.set_alive({"version": "3.16.3"} if alive is None else alive)
    return store


def builder(debounce: float = 60.0, stale: float = 180.0) -> StateBuilder:
    return StateBuilder(
        alarm_debounce=timedelta(seconds=debounce), heartbeat_stale_after=timedelta(seconds=stale)
    )


def build(
    store: StoreSet,
    now: datetime = T0,
    *,
    b: StateBuilder | None = None,
    clock: DeviceClock = ROME_CLOCK,
) -> RehomState:
    return (b or builder()).build(store, now=now, clock=clock)


def set_value(store: StoreSet, path: str, value: str, at: datetime, **extra: Any) -> None:
    frame = {"domain": "termo", "type": "update", "path": path, "value": value}
    frame["gruppo"] = extra.pop("gruppo", path.split(".", 1)[0])
    frame.update(extra)
    store.apply_frame(frame, at, MONO0 + (at - T0).total_seconds())


def override_row(zone: str, preset: str, program: str, set_at: str, expires: str) -> dict[str, Any]:
    return {
        "Gruppo": "PROG",  # REST says PROG; the store regroups PROG_GIORNO_* rows
        "Unita": zone,
        "SubUni": preset,
        "Key": "PROG_GIORNO_ESTATE",
        "Valore": program,
        "Impostazione": set_at,
        "Scadenza": expires,
    }


# ---------------------------------------------------------------------------
# The store -> model table
# ---------------------------------------------------------------------------


def test_hub_and_plant() -> None:
    state = build(stores())
    hub = state.hub
    assert hub.mac == "02:00:00:ab:cd:ef"
    assert (hub.web_version, hub.controller_version) == ("3.16.3", "3.1.0.12")
    assert (hub.board, hub.platform, hub.timezone) == ("pi", "arm32", "Europe/Rome")
    plant = state.plant
    assert plant.season is Season.SUMMER
    assert plant.season_forced is False
    assert plant.conf_season is Season.SUMMER
    assert plant.mode is MasterMode.MANUAL
    assert plant.set_point is MasterSetPoint.COMFORT
    assert plant.preset is MasterPreset.COMFORT
    assert (
        plant.temperature_off,
        plant.temperature_economy,
        plant.temperature_pre_comfort,
        plant.temperature_comfort,
        plant.temperature_eco_aux,
    ) == (38.0, 29.0, 26.0, 24.0, 16.0)
    assert plant.display_temperature == 24.0
    assert plant.controller_setpoint == 26.0
    assert plant.effective_setpoint == 26.0
    assert plant.setpoint_mismatch is True
    assert plant.setpoint_mismatch_since == T0
    assert (plant.predictive, plant.read_only, plant.server_on_requested) == (False, False, True)
    assert plant.lock is LockState.NORMAL
    assert (plant.is_crono, plant.serial_down) == (False, False)
    assert plant.demand is True  # zone 001 calls
    assert plant.current_temperature == 21.5
    assert plant.domotica_enabled is True
    assert plant.installer_session is True  # CONF_ZONE_ON = 1
    assert plant.installer_activity_at is None
    assert plant.vmc_type_code == "6"
    assert dict(state.plant_conf) == PLANT_CONF
    assert state.device_clock is ROME_CLOCK
    assert state.synced_at == T0
    assert state.built_at == T0


def test_zones_presence_allowlist_and_fields() -> None:
    state = build(stores())
    assert tuple(state.zones) == ("001", "002")  # stray 004/005/000 never modelled
    z1, z2 = state.zones["001"], state.zones["002"]
    assert (z1.name, z1.online, z1.temperature) == ("Zona 001", True, 21.5)
    assert (z1.humidity, z1.has_humidity_sensor) == (48.0, True)
    assert (z1.calling, z1.forced, z1.has_vmc) == (True, False, True)
    assert (z1.setp, z1.offset, z1.forced_setpoint) == (ZoneSetp.UNSET, -1.0, 24.0)
    assert z1.controller_setpoint == 26.0
    # MANUAL comfort: house preset, 24 + trunc(-1) = 23
    assert (z1.level, z1.base, z1.target) == (Level.COMFORT, 24.0, 23.0)
    assert (z1.mode, z1.preset, z1.control_source) == (
        ZoneMode.MANUAL,
        ZonePreset.COMFORT,
        ControlSource.HOUSE,
    )
    assert z1.hvac_action is HvacAction.COOLING
    assert z1.next_change_at is None
    assert z1.override is None
    assert z1.identity.serial == "S-0001"
    assert z1.identity.firmware == "7"
    assert z1.identity.custom_id is None  # blank -> None
    assert (z2.name, z2.online) == ("Zona 002", False)
    assert (z2.humidity, z2.has_humidity_sensor) == (None, False)  # 255 = no sensor
    assert z2.hvac_action is HvacAction.IDLE
    assert z2.setp is ZoneSetp.COMFORT


def test_zone_schedules_both_seasons_and_sources() -> None:
    state = build(stores())
    summer = state.zones["001"].schedule.summer
    assert summer.source is ScheduleSource.PROG
    assert [day.weekday for day in summer.days] == list(range(7))
    assert all(day.preset == "1" for day in summer.days)
    assert summer.days[5].levels is not None
    assert summer.days[5].levels[24] is Level.COMFORT
    assert summer.crono == (Level.PRE_COMFORT,) * 48
    assert state.zones["001"].schedule.winter.source is ScheduleSource.NONE
    winter = state.zones["002"].schedule.winter
    assert winter.source is ScheduleSource.ZONA_MIRROR
    assert winter.days[5].preset == "2"
    assert winter.days[5].levels == (Level.PRE_COMFORT,) * 48
    assert winter.days[0].preset is None
    assert winter.days[0].levels is None


def test_vmc_presence_allowlist_and_fields() -> None:
    state = build(stores())
    assert tuple(state.vmcs) == ("001",)  # DEUM 1/2/000 and absent 003 never modelled
    vmc = state.vmcs["001"]
    assert (vmc.name, vmc.online) == ("VMC 001", True)
    assert (vmc.mode, vmc.forced_mode, vmc.state) == (VmcMode.DEHUMIDIFY, None, VmcState.RUNNING)
    assert (vmc.effective_mode, vmc.running, vmc.error) == (VmcMode.DEHUMIDIFY, True, False)
    assert vmc.mode_availability[VmcMode.STOP] is Availability.WRITABLE
    assert vmc.mode_availability[VmcMode.COOL] is Availability.READ_ONLY
    assert vmc.mode_availability[VmcMode.HEAT] is Availability.HIDDEN  # missing row
    assert vmc.selectable_modes == (VmcMode.STOP, VmcMode.DEHUMIDIFY, VmcMode.VENTILATE)
    assert vmc.fan.kind is FanKind.DISCRETE
    assert vmc.fan.control is Availability.WRITABLE
    assert (vmc.fan.speed, vmc.fan.value) == (FanSpeed.MED, 2)
    assert (vmc.fan.step_min, vmc.fan.step_max, vmc.fan.show_labels) == (0, 100, False)
    assert vmc.free_cooling.level is Availability.WRITABLE
    assert (vmc.free_cooling.on, vmc.free_cooling.error) == (True, False)
    assert (vmc.dehumidifying, vmc.integration_active) == (True, True)
    assert vmc.integration is HvacAction.COOLING  # summer
    assert (vmc.defrosting, vmc.schedule_active, vmc.compressor) == (False, False, True)
    assert (vmc.duty_cycle, vmc.renewal_position, vmc.alarm_bitmask) == (40, 35, 248)
    assert vmc.inlet_air_temperature == 18.5
    assert vmc.renewal == 40
    assert vmc.alarm_flags["ALLARM_SONDA_RICIRCOLO"] is True
    assert vmc.alarm_flags["ALLARM_SBRINAMENTO"] is False
    assert vmc.alarm_flags["ALLARM_ESPANSIONE"] is None
    assert vmc.identity.serial == "D-1"


def test_vmc_integration_and_renewal_gating() -> None:
    rows = [
        r
        for r in house()
        if (r["Gruppo"], r["Key"]) not in {("REHOM", "STAGIONE"), ("DEUM", "ABILITA_RINNOVO")}
    ]
    vmc = build(stores(rows)).vmcs["001"]
    assert vmc.integration is None  # season unknown
    assert vmc.renewal is None  # ABILITA_RINNOVO missing
    winter = [
        rec("REHOM", "", "", "STAGIONE", "0") if r["Key"] == "STAGIONE" else r for r in house()
    ]
    assert build(stores(winter)).vmcs["001"].integration is HvacAction.HEATING
    idle = [rec("DEUM", "001", "", "ST_RAFF_RISC", "0") if r["Key"] == "ST_RAFF_RISC" else r
            for r in house()]  # fmt: skip
    assert build(stores(idle)).vmcs["001"].integration is None


def test_actuators_and_fancoils() -> None:
    state = build(stores())
    assert tuple(state.actuators) == ("002",)
    actuator = state.actuators["002"]
    assert actuator.online is True
    assert actuator.temperatures_raw == "100,100,100"
    assert actuator.relays_raw == "100,100,00000000000,00,000"
    assert actuator.identity.firmware == "3"
    assert tuple(state.fancoils) == ("003",)
    assert state.fancoils["003"].availability is Availability.WRITABLE
    assert state.fancoils["003"].online is True
    rows = [r for r in house() if r["Gruppo"] != "FANCOIL"]
    assert build(stores(rows)).fancoils["003"].availability is Availability.HIDDEN


def test_alarms_health_weather() -> None:
    store = stores()
    state = build(store)
    ids = [alarm.id for alarm in state.alarms]
    assert ids == ["vmc:001:ALLARM_SONDA_RICIRCOLO", "zone:002:not_responding"]
    first = state.alarms[0]
    assert (first.source, first.device, first.unit) == (AlarmSource.VMC_FLAG, DeviceKind.VMC, "001")
    assert first.first_seen == T0
    assert state.alarms_debounced == ()
    health = state.health
    assert (health.bus_ok, health.watchdog_ok, health.internet_ok) == (True, True, True)
    assert health.remote_access_configured is True  # REMOTE_TOKEN redacted but present
    assert (health.heartbeat_at, health.heartbeat_ok) == (None, None)  # WS not live
    assert health.cpu_temperature == 64.5
    assert health.wifi_available is False
    assert health.weather_service_ok is True
    weather = state.weather
    assert weather is not None
    assert weather.updated_at == datetime(2026, 9, 25, 10, 4, 1, tzinfo=UTC)
    assert weather.forecast_temperatures == (21.62, 21.02)
    assert weather.humidity == 59.625
    assert len(weather.days) == 1
    assert (weather.days[0].condition_type, weather.days[0].icon_code) == (0, 1)
    assert (weather.days[0].temp_min, weather.days[0].temp_max) == (15.5, 21.84)
    assert (weather.days[0].wind_speed, weather.days[0].wind_bearing) == (3.5, 112.25)
    assert state.forecast is None


def test_alarm_debounce_and_first_seen_survive_rebuilds() -> None:
    store = stores()
    b = builder(debounce=30.0)
    build(store, T0, b=b)
    later = build(store, T0 + timedelta(seconds=29), b=b)
    assert later.alarms[0].first_seen == T0
    assert later.alarms_debounced == ()
    assert later.next_change_at == T0 + timedelta(seconds=30)  # alarm maturity
    matured = build(store, T0 + timedelta(seconds=30), b=b)
    assert [a.id for a in matured.alarms_debounced] == [a.id for a in matured.alarms]


def test_debounced_alarm_clear_is_debounced_too() -> None:
    """A matured VMC flag that drops for 5 s stays debounced; one gone for 30 s is released."""
    flag = "vmc:001:ALLARM_SONDA_RICIRCOLO"
    store = stores()
    b = builder(debounce=30.0)
    build(store, T0, b=b)
    assert flag in [a.id for a in build(store, T0 + timedelta(seconds=30), b=b).alarms_debounced]

    set_value(store, "DEUM.001..ALLARM_SONDA_RICIRCOLO", "0", T0 + timedelta(seconds=40))
    held = build(store, T0 + timedelta(seconds=40), b=b)
    assert flag not in [a.id for a in held.alarms]  # not active ...
    assert flag in [a.id for a in held.alarms_debounced]  # ... but not cleared yet
    assert held.next_change_at == T0 + timedelta(seconds=70)  # the release

    set_value(store, "DEUM.001..ALLARM_SONDA_RICIRCOLO", "1", T0 + timedelta(seconds=45))
    back = build(store, T0 + timedelta(seconds=45), b=b)
    alarm = next(a for a in back.alarms_debounced if a.id == flag)
    assert alarm.first_seen == T0  # the same streak
    assert next(a for a in back.alarms if a.id == flag).first_seen == T0

    # the client rebuilds on every frame, so the end is seen at the frame instant
    set_value(store, "DEUM.001..ALLARM_SONDA_RICIRCOLO", "0", T0 + timedelta(seconds=50))
    build(store, T0 + timedelta(seconds=50), b=b)
    assert flag in [a.id for a in build(store, T0 + timedelta(seconds=79), b=b).alarms_debounced]
    released = build(store, T0 + timedelta(seconds=80), b=b)
    assert flag not in [a.id for a in released.alarms_debounced]


def test_heartbeat_from_frame_time() -> None:
    store = stores()
    store.set_live_since(T0 - timedelta(seconds=10))
    before = build(store)
    assert before.health.heartbeat_ok is None
    # the alarm maturity (+60 s) comes before the heartbeat deadline (live_since + 180 s)
    assert before.next_change_at == T0 + timedelta(seconds=60)
    set_value(store, "PROC...WATCHDOG_MASTER", "1", T0 - timedelta(seconds=5))
    state = build(store, b=builder(debounce=600.0))
    assert state.health.heartbeat_at == T0 - timedelta(seconds=5)
    assert state.health.heartbeat_ok is True
    assert state.next_change_at == T0 + timedelta(seconds=175)  # heartbeat deadline
    stale = build(store, T0 + timedelta(seconds=175), b=builder(debounce=600.0))
    assert stale.health.heartbeat_ok is False


def test_setpoint_mismatch_since_memory() -> None:
    store = stores()
    b = builder()
    assert build(store, T0, b=b).plant.setpoint_mismatch_since == T0
    assert build(store, T0 + timedelta(minutes=3), b=b).plant.setpoint_mismatch_since == T0
    set_value(store, "REHOM...SET_POINT_TEMP", "24", T0 + timedelta(minutes=4))
    fixed = build(store, T0 + timedelta(minutes=4), b=b).plant
    assert (fixed.setpoint_mismatch, fixed.setpoint_mismatch_since) == (False, None)
    set_value(store, "REHOM...SET_POINT_TEMP", "26", T0 + timedelta(minutes=5))
    again = build(store, T0 + timedelta(minutes=5), b=b).plant
    assert again.setpoint_mismatch_since == T0 + timedelta(minutes=5)


def test_equal_stores_give_equal_states() -> None:
    b = builder()
    first = build(stores(), T0, b=b)
    second = build(stores(at=T0 + timedelta(seconds=1)), T0 + timedelta(seconds=1), b=b)
    assert first == second  # synced_at/built_at are not compared


# ---------------------------------------------------------------------------
# next_change_at composition
# ---------------------------------------------------------------------------


def test_next_change_is_the_slot_boundary_by_default() -> None:
    rows = [r for r in house() if not r["Key"].startswith("ALLARM_")]
    rows = [rec("REHOM", "", "", "STATO_SONDE", _vector({0, 1}, 24)) if r["Key"] == "STATO_SONDE"
            else r for r in rows]  # fmt: skip
    state = build(stores(rows))
    assert state.alarms == ()
    assert state.next_change_at == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def test_next_change_uses_zone_schedule_and_override_instants() -> None:
    rows = [
        rec("REHOM", "", "", r["Key"], {"MODO": "2", "SET_POINT": "0"}[r["Key"]])
        if r["Gruppo"] == "REHOM" and r["Key"] in ("MODO", "SET_POINT")
        else r
        for r in house()
    ]
    at = datetime(2026, 9, 25, 19, 50, tzinfo=UTC)  # 21:50 local, COMFORT until 22:00
    state = build(stores(rows, at=at), at)
    zone = state.zones["001"]
    assert zone.control_source is ControlSource.SCHEDULE
    assert zone.next_change_at == datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
    program = ",".join(["2"] * 48)
    overrides = [override_row("001", "1", program, "2026-09-25 21:00:00", "2026-09-25 21:52:29")]
    state = build(stores(rows, overrides=overrides, at=at), at, b=builder(debounce=3600.0))
    zone = state.zones["001"]
    assert zone.control_source is ControlSource.OVERRIDE
    assert zone.preset is ZonePreset.TEMPORARY_COMFORT
    assert zone.override is not None
    assert zone.override.applies is True
    assert zone.override.set_at == datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
    assert zone.override.expires_at == datetime(2026, 9, 25, 19, 52, 29, tzinfo=UTC)
    assert zone.next_change_at == datetime(2026, 9, 25, 19, 52, 30, tzinfo=UTC)
    assert state.next_change_at == datetime(2026, 9, 25, 19, 52, 30, tzinfo=UTC)


def test_next_change_includes_override_instants_outside_auto() -> None:
    program = ",".join(["2"] * 48)
    overrides = [override_row("001", "1", program, "2026-09-25 12:23:00", "2026-09-25 12:25:00")]
    state = build(stores(overrides=overrides), b=builder(debounce=3600.0))
    assert state.zones["001"].override is not None
    assert state.zones["001"].override.applies is False  # MANUAL
    assert state.zones["001"].next_change_at is None
    assert state.next_change_at == datetime(2026, 9, 25, 10, 23, tzinfo=UTC)  # set_at


def test_crono_lock_from_override_store() -> None:
    lock_row = {"Gruppo": "WEBSERVER", "Unita": "", "SubUni": "", "Key": "WEBSERVER", "Valore": "0"}
    state = build(stores(overrides=[lock_row]))
    assert state.plant.lock is LockState.CRONO
    assert state.plant.is_crono is True
    assert all(zone.control_source is ControlSource.HOUSE for zone in state.zones.values())


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_empty_stores() -> None:
    state = build(stores([], plant_conf={}, config={}, alive={"version": 3}))
    assert dict(state.zones) == {}
    assert dict(state.vmcs) == {}
    assert state.alarms == ()
    assert state.weather is None
    assert state.hub.web_version is None
    assert state.hub.mac is None
    assert state.hub.board is None
    assert state.plant.mode is None
    assert state.plant.setpoint_mismatch is None
    assert state.plant.demand is None
    assert state.plant.installer_session is None
    assert state.plant.lock is LockState.NORMAL
    assert state.health.remote_access_configured is False
    assert state.health.watchdog_ok is None
    assert state.next_change_at == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def test_demand_and_installer_false() -> None:
    rows = [rec("ZONA", "001", "", "ATTIVA", "0") if r["Key"] == "ATTIVA" and r["Unita"] == "001"
            else r for r in house()]  # fmt: skip
    state = build(stores(rows, plant_conf={"CONFIGURA_ON": "0", "CONF_DEUM_ON": "x"}))
    assert state.plant.demand is False
    assert state.plant.installer_session is False


def test_forecast_parsed_once_per_burst() -> None:
    store = stores()
    items = {
        "dt1790337600": {"dt": 1790337600, "main": {"temp": 21.5}, "pop": 0.2},
        "dt99999999999999999": {"dt": 99999999999999999, "main": {"temp": 1}},
    }
    for key, item in items.items():
        frame = {
            "domain": "termo",
            "type": "update",
            "gruppo": "METEO_DATA",
            "path": f"METEO_DATA...{key}",
            "value": json.dumps(item),
        }
        store.apply_frame(frame, T0, MONO0)
    b = builder()
    first = build(store, b=b)
    second = build(store, T0 + timedelta(seconds=1), b=b)
    assert first.forecast is not None
    assert second.forecast is not None
    assert first.forecast.received_at == T0
    assert [entry.time for entry in first.forecast.entries] == [
        datetime.fromtimestamp(1790337600, UTC)
    ]  # the unrepresentable epoch is skipped
    assert first.forecast.entries[0].precipitation_probability == 0.2
    assert second.forecast.entries is first.forecast.entries  # cached


def test_extreme_override_timestamps_never_raise() -> None:
    program = ",".join(["2"] * 48)
    forever = [override_row("001", "1", program, "0001-01-01 00:00:00", "9999-12-31 23:59:59")]
    state = build(stores(overrides=forever))
    override = state.zones["001"].override
    assert override is not None
    assert override.set_at is None  # before year 1 in UTC: unrepresentable
    assert override.expires_at == datetime(9999, 12, 31, 22, 59, 59, tzinfo=UTC)
    utc_state = build(stores(overrides=forever), clock=DeviceClock.utc())
    assert utc_state.next_change_at is not None


def test_override_without_expiry() -> None:
    program = ",".join(["2"] * 48)
    row = override_row("001", "1", program, "2026-09-25 12:23:00", "")
    override = build(stores(overrides=[row])).zones["001"].override
    assert override is not None
    assert (override.expires_at, override.applies) == (None, False)


def test_dst_fold_slot_boundary_steps_from_now() -> None:
    # 2026-10-25 01:15Z is 02:15 CET, the second pass of the repeated hour: the
    # naive boundary 02:30 (fold=0) would be 00:30Z, which is in the past.
    now = datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
    rows = [r for r in house() if not r["Key"].startswith("ALLARM_")]
    rows = [rec("REHOM", "", "", "STATO_SONDE", _vector({0, 1}, 24)) if r["Key"] == "STATO_SONDE"
            else r for r in rows]  # fmt: skip
    state = build(stores(rows, at=now), now)
    assert state.next_change_at == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_rejects_bad_windows() -> None:
    with pytest.raises(ValueError, match="30 s"):
        builder(debounce=10.0)
    with pytest.raises(ValueError, match="positive"):
        builder(stale=0.0)


# ---------------------------------------------------------------------------
# review regressions
# ---------------------------------------------------------------------------


def test_heartbeat_is_unknown_without_a_watchdog_master_row() -> None:
    """Regression: a controller without the optional row was reported stalled forever."""
    rows = [r for r in house() if r["Key"] != "WATCHDOG_MASTER"]
    store = stores(rows)
    store.set_live_since(T0)
    b = builder(debounce=3600.0)
    for seconds in (0, 181, 3600):
        health = build(store, T0 + timedelta(seconds=seconds), b=b).health
        assert (health.heartbeat_ok, health.heartbeat_at) == (None, None)
    # no heartbeat deadline among the time-driven instants: the slot boundary comes first
    assert build(store, T0, b=b).next_change_at == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    # once the row appears (a first frame), the heartbeat is tracked as usual
    set_value(store, "PROC...WATCHDOG_MASTER", "1", T0 + timedelta(seconds=3601))
    assert build(store, T0 + timedelta(seconds=3602), b=b).health.heartbeat_ok is True


def test_a_numeric_noop_frame_never_changes_the_built_state() -> None:
    """Regression: a suppressed "6" -> "6.0" frame still rewrote a raw model field."""
    store = stores()
    b = builder(debounce=3600.0)
    before = build(store, b=b)
    at = T0 + timedelta(seconds=1)
    frame = {"domain": "termo", "type": "update", "path": "DEUM...TIPO", "value": "6.0"}
    assert store.apply_frame(frame, at, MONO0 + 1).empty()
    after = build(store, b=b)
    assert after.plant.vmc_type_code == "6"
    assert after == before


def test_preset_ids_are_identifiers_so_a_rebinding_is_a_change() -> None:
    """Regression: PROG_SETT "1" -> "01" was a suppressed no-op that unbound the day."""
    store = stores()
    b = builder(debounce=3600.0)
    friday = build(store, b=b).zones["001"].schedule.summer.days[5]
    assert (friday.preset, friday.levels is not None) == ("1", True)
    frame = {
        "domain": "termo",
        "type": "update",
        "path": "PROG.001.5.PROG_SETT_ESTATE",
        "gruppo": "PROG",
        "value": "01",
    }
    changes = store.apply_frame(frame, T0 + timedelta(seconds=1), MONO0 + 1)
    assert changes.changed == {"PROG.001.5.PROG_SETT_ESTATE"}
    friday = build(store, b=b).zones["001"].schedule.summer.days[5]
    assert (friday.preset, friday.levels) == ("01", None)  # "01" names no preset


def test_override_holds_through_its_last_second() -> None:
    """Regression: a rebuild inside Scadenza's last second dropped the override early."""
    rows = [
        rec("REHOM", "", "", r["Key"], {"MODO": "2", "SET_POINT": "0"}[r["Key"]])
        if r["Gruppo"] == "REHOM" and r["Key"] in ("MODO", "SET_POINT")
        else r
        for r in house()
    ]
    program = ",".join(["2"] * 48)
    overrides = [override_row("001", "1", program, "2026-09-25 21:00:00", "2026-09-25 21:52:29")]
    b = builder(debounce=3600.0)
    at = datetime(2026, 9, 25, 19, 52, 29, 500_000, tzinfo=UTC)  # 21:52:29.5 local
    zone = build(stores(rows, overrides=overrides, at=at), at, b=b).zones["001"]
    assert zone.control_source is ControlSource.OVERRIDE
    assert zone.next_change_at == datetime(2026, 9, 25, 19, 52, 30, tzinfo=UTC)
    after = datetime(2026, 9, 25, 19, 52, 30, tzinfo=UTC)
    zone = build(stores(rows, overrides=overrides, at=after), after, b=b).zones["001"]
    assert zone.control_source is ControlSource.SCHEDULE


def test_a_built_state_converts_copies_and_pickles() -> None:
    """Regression: asdict/deepcopy/pickle failed on the read-only mappings."""
    state = build(stores())
    data = state.as_dict()
    text = json.dumps(data)
    assert "mappingproxy" not in text
    assert data["plant"]["season"] == "summer"
    assert data["zones"]["001"]["level"] == "COMFORT"  # an IntEnum by name
    assert data["vmcs"]["001"]["mode_availability"]["STOP"] == "WRITABLE"
    assert data["device_clock"] == {"tz": "Europe/Rome", "tz_name": "Europe/Rome", "skew": 0.0}
    assert data["built_at"] == "2026-09-25T10:22:00Z"
    assert data["zones"]["001"]["schedule"]["summer"]["days"][5]["levels"][24] == "COMFORT"
    assert copy.deepcopy(state) == state
    restored = pickle.loads(pickle.dumps(state))  # noqa: S301 - our own bytes
    assert restored == state
    assert isinstance(restored.zones, MappingProxyType)
    assert isinstance(restored.vmcs["001"].mode_availability, MappingProxyType)
    with pytest.raises(TypeError):
        restored.zones["x"] = restored.zones["001"]  # type: ignore[index]
