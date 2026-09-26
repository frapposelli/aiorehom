"""Models: frozen, comparable, read-only mappings."""

from __future__ import annotations

import dataclasses
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import pytest

from aiorehom import models
from aiorehom.clock import DeviceClock
from aiorehom.enums import Availability, UpdateReason, VmcMode
from aiorehom.models import (
    HistorySample,
    RehomState,
    StateUpdate,
    SyncStats,
    UnitIdentity,
    VmcFan,
)
from aiorehom.state import StoreSet

from .sync_fakes import FakeBuilder, interface_rows
from .test_state_store import snapshot

T0 = datetime(2026, 9, 25, 10, tzinfo=UTC)


def _state(now: datetime = T0) -> RehomState:
    stores = StoreSet()
    stores.replace(snapshot(interface_rows(), plant_conf={"stagione": "1"}), T0)
    return FakeBuilder().build(stores, now=now, clock=DeviceClock.utc())


def test_every_model_is_a_frozen_slotted_kw_only_dataclass() -> None:
    names = [
        "UnitIdentity", "Hub", "Plant", "DayProgram", "SeasonSchedule", "ZoneSchedule",
        "ZoneOverride", "Zone", "VmcFan", "VmcFreeCooling", "Vmc", "Actuator", "Fancoil",
        "Alarm", "Health", "WeatherDay", "Weather", "ForecastEntry", "Forecast",
        "RehomState", "StateUpdate", "HistorySample", "SyncStats",
    ]  # fmt: skip
    for name in names:
        cls = getattr(models, name)
        params = cls.__dataclass_params__
        assert params.frozen, name
        assert "__slots__" in cls.__dict__, name
        assert all(f.kw_only for f in dataclasses.fields(cls)), name


def test_state_is_frozen_and_equality_ignores_built_and_synced_at() -> None:
    first = _state()
    later = _state(T0 + timedelta(minutes=5))
    assert first.built_at != later.built_at
    assert first == later
    assert replace(first, synced_at=T0 + timedelta(hours=1)) == first
    assert replace(first, next_change_at=T0) != first
    with pytest.raises(FrozenInstanceError):
        first.built_at = T0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.plant.temperature_comfort = 1.0  # type: ignore[misc]


def test_state_mappings_are_read_only_and_states_are_not_hashable() -> None:
    state = _state()
    assert isinstance(state.zones, MappingProxyType)
    with pytest.raises(TypeError):
        state.zones["009"] = state.zones["001"]  # type: ignore[index]
    with pytest.raises(TypeError):
        state.plant_conf["x"] = "y"  # type: ignore[index]
    with pytest.raises(TypeError):
        hash(state)
    assert state.zones["001"].temperature == 21.5
    assert state.zones["002"].calling is True


def test_small_models_and_defaults() -> None:
    assert UnitIdentity() == UnitIdentity(serial=None, firmware=None, custom_id=None)
    with pytest.raises(TypeError):
        UnitIdentity("positional")  # type: ignore[misc]
    fan = VmcFan(
        kind=models.FanKind.DISCRETE,
        control=Availability.READ_ONLY,
        speed=None,
        value=None,
        step_min=0,
        step_max=100,
        show_labels=True,
    )
    assert hash(fan) == hash(replace(fan))
    stats = dataclasses.asdict(SyncStats())
    assert stats.pop("last_sync_error") is None
    assert stats == dict.fromkeys(stats, 0)
    sample = HistorySample(
        time=T0,
        local_time=datetime(2026, 9, 25, 12),
        value=1.5,
        rowid=None,
    )
    assert sample.local_time.tzinfo is None
    update = StateUpdate(state=_state(), previous=None, reason=UpdateReason.SYNC, at=T0)
    assert update.changed == frozenset()
    assert update.removed == frozenset()
    assert update.conf_changed == frozenset()
    assert update.config_changed == frozenset()
    assert update.forecast_changed is False
    assert models.Program == tuple[models.Level | None, ...]
    assert VmcMode.VENTILATE == 8


def test_every_public_model_and_client_property_is_documented() -> None:
    """Regression: the public state model had no docstrings (dataclass signatures only)."""
    from aiorehom.client import RehomClient

    classes = [
        obj
        for obj in vars(models).values()
        if isinstance(obj, type)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == models.__name__
    ]
    assert len(classes) >= 23
    for cls in classes:
        doc = cls.__doc__ or ""
        assert doc and not doc.startswith(f"{cls.__name__}("), cls.__name__
    for name in ("host", "options", "state", "connection_state", "available", "device_clock",
                 "last_synced_at", "stats"):  # fmt: skip
        prop = getattr(RehomClient, name)
        assert isinstance(prop, property)
        assert prop.__doc__, name
