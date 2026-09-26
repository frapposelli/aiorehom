"""Model-level change helpers of ``aiorehom.replay`` (flatten, mask, diff)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone

from aiorehom import replay
from aiorehom.clock import DeviceClock
from aiorehom.enums import Level, UpdateReason
from aiorehom.models import StateUpdate
from aiorehom.replay import (
    ABSENT,
    diff_flat,
    flatten_state,
    format_value,
    mask_flat,
    masked_records,
    update_alarm_ids,
)

from .test_builder import T0, build, builder, stores


def test_flatten_rules() -> None:
    flat = flatten_state(build(stores()))
    assert "built_at" not in flat
    assert flat["synced_at"] == "2026-09-25T10:22:00.000Z"
    assert flat["plant.mode"] == "MANUAL"  # IntEnum by name
    assert flat["plant.season"] == "summer"  # StrEnum by value
    assert flat["plant.setpoint_mismatch"] is True
    assert flat["plant.controller_setpoint"] == 26.0
    assert flat["zones.001.name"] == "Zona 001"
    assert flat["zones.001.schedule.summer.days[5].preset"] == "1"
    assert flat["zones.001.schedule.summer.days[5].levels"] == ",".join(
        ["1"] * 24 + ["3"] * 20 + ["0"] * 4
    )
    assert flat["zones.001.schedule.winter.days[0].levels"] is None
    assert flat["alarms[vmc:001:ALLARM_SONDA_RICIRCOLO].first_seen"] == "2026-09-25T10:22:00.000Z"
    assert flat["vmcs.001.mode_availability.STOP"] == "WRITABLE"
    assert flat["vmcs.001.selectable_modes"] == "STOP,DEHUMIDIFY,VENTILATE"
    assert flat["vmcs.001.alarm_flags.ALLARM_SONDA_RICIRCOLO"] is True
    assert flat["weather.forecast_temperatures"] == "21.62,21.02"
    assert flat["weather.days[0].pressure"] == 1020.375
    assert flat["device_clock.tz"] == "Europe/Rome"
    assert flat["device_clock.skew"] == 0.0
    assert flat["plant_conf.stagione"] == "1"
    assert flatten_state(None) == {}


def test_flatten_csv_with_missing_items() -> None:
    state = build(stores())
    zone = state.zones["001"]
    levels = (Level.OFF, None, Level.COMFORT)
    schedule = dataclasses.replace(
        zone.schedule,
        summer=dataclasses.replace(zone.schedule.summer, crono=levels),
    )
    weather = state.weather
    assert weather is not None
    changed = dataclasses.replace(
        state,
        zones={"001": dataclasses.replace(zone, schedule=schedule)},
        weather=dataclasses.replace(weather, forecast_temperatures=(1.5, None)),
        device_clock=DeviceClock(tz=timezone(timedelta(hours=1)), tz_name=None),
    )
    flat = flatten_state(changed)
    assert flat["zones.001.schedule.summer.crono"] == "0,?,3"
    assert flat["weather.forecast_temperatures"] == "1.5,"
    assert flat["device_clock.tz"] == "UTC+01:00"


def test_mask_flat() -> None:
    flat = {
        "zones.001.name": "Zona 001",
        "vmcs.001.name": None,
        "hub.mac": "02:00:00:ab:cd:ef",
        "zones.001.identity.serial": "S-1",
        "vmcs.001.identity.custom_id": "C-1",
        "plant_conf.secret": "<redacted len=12>",
        "plant.mode": "MANUAL",
    }
    masked = mask_flat(flat)
    assert masked == {
        "zones.001.name": "<name>",
        "vmcs.001.name": None,
        "hub.mac": "<mac>",
        "zones.001.identity.serial": "<id>",
        "vmcs.001.identity.custom_id": "<id>",
        "plant_conf.secret": "<redacted>",
        "plant.mode": "MANUAL",
    }
    shown = mask_flat(flat, show_names=True)
    assert shown["zones.001.name"] == "Zona 001"
    assert shown["hub.mac"] == "<mac>"


def test_diff_flat() -> None:
    old = {"a": 1, "b": True, "c": "x", "gone": 5, "zones.001.temperature": 20.0}
    new = {"a": 1.0, "b": 1, "c": "x", "added": None, "zones.001.temperature": 20.5}
    changes = diff_flat(old, new)
    assert changes == [
        ("a", 1, 1.0),  # type changes count
        ("b", True, 1),
        ("added", ABSENT, None),
        ("zones.001.temperature", 20.0, 20.5),
        ("gone", 5, ABSENT),
    ]
    assert diff_flat(old, new, ignore=["zones.*.temperature", "[ab]"]) == [
        ("added", ABSENT, None),
        ("gone", 5, ABSENT),
    ]
    assert format_value(ABSENT) == "<absent>"
    assert format_value(3) == 3
    assert repr(ABSENT) == "<absent>"


def test_masked_records() -> None:
    assert masked_records(["UTEN...alice", "ZONA.001..ATTIVA", "odd"]) == [
        "UTEN...<user>",
        "ZONA.001..ATTIVA",
        "odd",
    ]


def test_update_alarm_ids() -> None:
    store = stores()
    b = builder(debounce=30.0)
    first = build(store, T0, b=b)
    later = build(store, T0 + timedelta(seconds=30), b=b)
    sync = StateUpdate(state=first, previous=None, reason=UpdateReason.SYNC, at=T0)
    raised, debounced = update_alarm_ids(sync)
    assert raised == {"vmc:001:ALLARM_SONDA_RICIRCOLO", "zone:002:not_responding"}
    assert debounced == set()
    matured = StateUpdate(
        state=later, previous=first, reason=UpdateReason.CLOCK, at=datetime.now(UTC)
    )
    assert update_alarm_ids(matured) == (set(), raised)


def test_scalar_fallbacks() -> None:
    assert replay._Absent() is ABSENT
    assert replay._scalar(datetime(2026, 9, 25, 12, 0)) == "2026-09-25T12:00:00.000"
    assert replay._scalar(b"x") == "b'x'"
