"""Synthetic variants of the fixture replay.

``ReplayDevice.from_dir(..., patch=fn)`` edits the REST snapshot in memory:
(i) AUTO, (ii) a temporary override that expires at 10:45:00Z, (iii) crono.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aiorehom.client import ClientOptions, RehomClient
from aiorehom.clock import VirtualClock
from aiorehom.enums import (
    ControlSource,
    Level,
    LockState,
    UpdateReason,
    ZoneMode,
    ZonePreset,
)
from aiorehom.models import RehomState, StateUpdate
from aiorehom.replay import ReplayData, ReplayDevice, ReplayDriver

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"


def T(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-25T{text}+00:00")


def set_row(data: ReplayData, gruppo: str, unita: str, key: str, value: str) -> None:
    for row in data.interface:
        if (row["Gruppo"], row["Unita"], row["SubUni"], row["Key"]) == (gruppo, unita, "", key):
            row["Valore"] = value
            return
    raise AssertionError(f"no row {gruppo}.{unita}..{key}")  # pragma: no cover


def auto(data: ReplayData) -> None:
    set_row(data, "REHOM", "", "MODO", "2")
    set_row(data, "REHOM", "", "SET_POINT", "0")
    set_row(data, "ZONA", "001", "SETP_CORRENTE", "0")


def preset_1_summer(data: ReplayData) -> list[str]:
    for row in data.interface:
        if (row["Gruppo"], row["Unita"], row["SubUni"], row["Key"]) == (
            "PROG",
            "001",
            "1",
            "PROG_GIORNO_ESTATE",
        ):
            return str(row["Valore"]).split(",")
    raise AssertionError("zone 001 has no summer preset 1")  # pragma: no cover


def with_override(data: ReplayData) -> None:
    auto(data)
    program = preset_1_summer(data)
    program[24:26] = ["2", "2"]
    data.overrides.append(
        {
            "Gruppo": "PROG_OVERRIDE",
            "Unita": "001",
            "SubUni": "1",
            "Key": "PROG_GIORNO_ESTATE",
            "Valore": ",".join(program),
            "Impostazione": "2026-09-25 12:10:00",
            "Scadenza": "2026-09-25 12:44:59",
        }
    )


def crono(data: ReplayData) -> None:
    with_override(data)
    set_row(data, "REHOM", "", "WEBSERVER", "0")


async def replay(
    patch: Callable[[ReplayData], None], checkpoints: tuple[str, ...]
) -> tuple[dict[str, RehomState], list[StateUpdate], int]:
    device = ReplayDevice.from_dir(FIXTURE, patch=patch)
    clock = VirtualClock(device.start)
    client = RehomClient(
        "replay.invalid",
        transport=device.transport(clock),
        ws_connector=device.ws_connector(clock),
        clock=clock,
        options=ClientOptions(alarm_debounce=30.0),
    )
    updates: list[StateUpdate] = []
    client.subscribe(updates.append)
    driver = ReplayDriver(device, clock)
    task = asyncio.create_task(client.connect())
    states: dict[str, RehomState] = {}
    for index, checkpoint in enumerate(checkpoints):
        await driver.run_until(T(checkpoint))
        if index == 0:
            await task
        states[checkpoint] = client.state
    await client.close()
    return states, updates, clock.pending_sleepers


async def test_i_auto_schedule() -> None:
    states, _updates, pending = await replay(auto, ("10:22:12",))
    state = states["10:22:12"]
    zone = state.zones["001"]
    assert zone.control_source is ControlSource.SCHEDULE
    assert (zone.mode, zone.preset, zone.level) == (ZoneMode.AUTO, ZonePreset.NONE, Level.COMFORT)
    assert zone.next_change_at == T("20:00:00")  # slot 44 = 22:00 local
    zone_011 = state.zones["011"]
    assert zone_011.control_source is ControlSource.ZONE
    assert (zone_011.mode, zone_011.preset) == (ZoneMode.MANUAL, ZonePreset.COMFORT)
    assert zone_011.next_change_at is None
    assert pending == 0


async def test_ii_override_expiry_publishes_a_clock_update() -> None:
    states, updates, pending = await replay(with_override, ("10:22:12", "10:44:59", "10:45:01"))
    during = states["10:44:59"].zones["001"]
    assert during.control_source is ControlSource.OVERRIDE
    assert during.preset is ZonePreset.TEMPORARY_COMFORT
    assert during.level is Level.PRE_COMFORT
    assert during.target == 26.0
    assert during.next_change_at == T("10:45:00")
    assert during.override is not None
    assert during.override.applies is True
    assert during.override.expires_at == T("10:44:59")
    clock_updates = [u for u in updates if u.reason is UpdateReason.CLOCK]
    assert T("10:45:00.05") in [u.at for u in clock_updates]
    after = states["10:45:01"].zones["001"]
    assert after.control_source is ControlSource.SCHEDULE
    assert after.level is Level.COMFORT
    assert after.target == 24.0
    assert after.override is not None
    assert after.override.applies is False
    assert pending == 0


async def test_iii_crono() -> None:
    states, _updates, pending = await replay(crono, ("10:22:12",))
    state = states["10:22:12"]
    assert state.plant.lock is LockState.CRONO
    assert state.plant.is_crono is True
    for zone in state.zones.values():
        assert zone.control_source is ControlSource.CRONO, zone.id
        assert zone.mode is ZoneMode.AUTO, zone.id
        assert zone.level is Level.PRE_COMFORT, zone.id  # preset 99 = 48 x "2"
        assert zone.next_change_at is None, zone.id
    override = state.zones["001"].override
    assert override is not None
    assert override.applies is False
    assert state.alarms == ()  # crono is not an alarm
    assert pending == 0
