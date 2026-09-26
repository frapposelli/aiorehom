"""E2E acceptance: the sanitised capture replayed through the real
``RehomClient`` on a virtual clock.

The replay runs once per module (about 3 s) with every socket call blocked,
and the checkpoints are inspected afterwards.  The ``e2`` oracle is computed
independently from ``ws.jsonl`` (``Decimal`` comparison, remove->update
coalescing, ``METEO_DATA`` excluded), without any library code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from aiorehom.client import ClientOptions, RehomClient
from aiorehom.clock import VirtualClock
from aiorehom.enums import (
    ControlSource,
    HvacAction,
    Level,
    LockState,
    MasterPreset,
    Season,
    UpdateReason,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)
from aiorehom.models import RehomState, StateUpdate, SyncStats
from aiorehom.replay import ReplayDevice, ReplayDriver

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"
ZONES = ("001", "002", "003", "009", "010", "011")
CHECKPOINTS = (
    "10:22:12",
    "10:24:40",
    "10:34:05",
    "10:37:10",
    "10:37:25",
    "10:41:06.000",
    "10:41:11",
    "10:41:26",
    "10:41:33.0",
    "10:41:35",
)


def T(text: str) -> datetime:
    """2026-09-25 at ``hh:mm:ss[.fff]`` UTC."""
    return datetime.fromisoformat(f"2026-09-25T{text}+00:00")


@contextlib.contextmanager
def no_sockets() -> Iterator[None]:
    """Fail any DNS lookup or connect (loopback included): a replay needs no socket."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("e2e replay: sockets are forbidden")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket, "getaddrinfo", refuse)
        mp.setattr(socket, "create_connection", refuse)
        mp.setattr(socket.socket, "connect", refuse)
        mp.setattr(socket.socket, "connect_ex", refuse)
        yield


@dataclass
class Run:
    device: ReplayDevice
    states: dict[str, RehomState]
    final: RehomState
    updates: list[StateUpdate]
    stats: SyncStats
    calls: list[str]
    pending_after_close: int


async def _replay() -> Run:
    device = ReplayDevice.from_dir(FIXTURE)
    assert device.start == T("10:22:06.806")  # the ws connect event
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
    await driver.run_until(T(CHECKPOINTS[0]))
    await task
    states = {CHECKPOINTS[0]: client.state}
    for checkpoint in CHECKPOINTS[1:]:
        await driver.run_until(T(checkpoint))
        states[checkpoint] = client.state
    await driver.run_to_end()
    final, stats, calls = client.state, client.stats, list(device.transport_calls)
    await client.close()
    return Run(device, states, final, updates, stats, calls, clock.pending_sleepers)


@pytest.fixture(scope="module")
def run() -> Run:
    with no_sockets():
        return asyncio.run(_replay())


def frames_of(run: Run) -> list[StateUpdate]:
    return [u for u in run.updates if u.reason is UpdateReason.FRAMES]


def series(run: Run, value: Any) -> list[Any]:
    """Distinct consecutive values of ``value(state)`` over every published update."""
    out: list[Any] = []
    for update in run.updates:
        item = value(update.state)
        if not out or out[-1] != item:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Sync order (WS-first buffering)
# ---------------------------------------------------------------------------


def test_sync_then_buffered_frames(run: Run) -> None:
    first, second = run.updates[0], run.updates[1]
    assert (first.reason, first.at, first.previous) == (UpdateReason.SYNC, T("10:22:09.906"), None)
    # frames of 10:22:08.026 and .138 arrived during the GETs: applied after the snapshot
    assert (second.reason, second.at) == (UpdateReason.FRAMES, T("10:22:10.006"))
    # ZONA.001..TEMP_AMBIENTE 24.2 equals the snapshot value: a no-op, never reported
    assert second.changed == {"ZONA.003..UMIDITA"}
    assert run.stats.buffered_max >= 2


# ---------------------------------------------------------------------------
# Checkpoints a-f
# ---------------------------------------------------------------------------


def test_a_topology_and_house_state(run: Run) -> None:
    state = run.states["10:22:12"]
    assert tuple(state.zones) == ZONES
    assert tuple(state.vmcs) == ("001", "002")
    assert tuple(state.actuators) == ("001", "002")
    assert dict(state.fancoils) == {}
    assert state.plant.lock is LockState.NORMAL
    assert state.plant.season is Season.SUMMER
    assert state.plant.preset is MasterPreset.COMFORT
    for zone in state.zones.values():
        assert zone.level is Level.COMFORT, zone.id
        assert zone.mode is ZoneMode.MANUAL, zone.id
        assert zone.preset is ZonePreset.COMFORT, zone.id
        assert zone.control_source is ControlSource.HOUSE, zone.id
        assert zone.next_change_at is None, zone.id
    assert state.zones["011"].setp is ZoneSetp.COMFORT
    assert state.alarms == ()
    assert state.weather is not None
    assert state.forecast is None


@pytest.mark.parametrize("checkpoint", ["10:22:12", "10:41:06.000"])
def test_b1_setpoint_mismatch(run: Run, checkpoint: str) -> None:
    plant = run.states[checkpoint].plant
    assert plant.setpoint_mismatch is True
    assert plant.controller_setpoint == 26.0
    assert plant.display_temperature == 24.0
    assert plant.setpoint_mismatch_since is not None
    assert plant.setpoint_mismatch_since == T("10:22:09.906")  # kept while True


def test_b2_setpoint_mismatch_cleared(run: Run) -> None:
    plant = run.states["10:41:11"].plant
    assert plant.setpoint_mismatch is False
    assert plant.controller_setpoint == 24.0
    assert plant.setpoint_mismatch_since is None


def test_c1_c2_zone_controller_setpoints(run: Run) -> None:
    assert {z.controller_setpoint for z in run.states["10:41:06.000"].zones.values()} == {26.0}
    assert {z.controller_setpoint for z in run.states["10:41:26"].zones.values()} == {24.0}
    assert series(run, lambda s: s.zones["001"].controller_setpoint) == [26.0, 24.0]
    for zone in ZONES[1:]:
        assert series(run, lambda s, z=zone: s.zones[z].controller_setpoint) == [26.0, 24.1, 24.0]


def test_c3_zone_002_calls(run: Run) -> None:
    zone = run.states["10:24:40"].zones["002"]
    assert zone.calling is True
    assert zone.hvac_action is HvacAction.COOLING


def test_c4_zones_start_calling(run: Run) -> None:
    before, after = run.states["10:41:33.0"], run.states["10:41:35"]
    for zone in ("001", "009", "010"):
        assert before.zones[zone].calling is False, zone
        assert after.zones[zone].calling is True, zone
        assert after.zones[zone].hvac_action is HvacAction.COOLING, zone
    for update in run.updates:
        assert update.state.zones["003"].calling is False
        assert update.state.zones["011"].calling is False


@pytest.mark.parametrize("which", ["10:22:12", "end"])
def test_d_zone_002_target(run: Run, which: str) -> None:
    state = run.final if which == "end" else run.states[which]
    zone = state.zones["002"]
    assert (zone.target, zone.offset, zone.base) == (23.0, -1.0, 24.0)


def test_e1_weather_and_forecast(run: Run) -> None:
    state = run.states["10:34:05"]
    assert state.weather is not None
    assert state.weather.updated_at == T("10:34:02")  # DATA 12:34:02 local
    assert state.forecast is not None
    assert len(state.forecast.entries) == 40
    meteo_changed: set[str] = set()
    for update in run.updates:
        assert not any(path.startswith("METEO") for path in update.removed)
        if update.reason is not UpdateReason.SYNC:
            meteo_changed |= {p for p in update.changed if p.startswith("METEO")}
    assert meteo_changed == {
        "METEO...DATA",
        "METEO...TEMPERATURE",
        "METEO.0..RANGE_TEMP",
        "METEO...UMIDITA",
    }


def _decimal(value: str) -> Decimal | None:
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", value.strip()):
        return None
    try:
        return Decimal(value.strip())
    except InvalidOperation:  # pragma: no cover - the pattern admits only valid decimals
        return None


def _same(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    if a is None or b is None:
        return False
    da, db = _decimal(a), _decimal(b)
    return da is not None and db is not None and da == db


def oracle_changed_paths() -> set[str]:
    """Paths whose value changed semantically over ``ws.jsonl`` (independent computation)."""
    rows = json.loads((FIXTURE / "interface.json").read_text("utf-8"))
    current: dict[str, str | None] = {
        f"{r['Gruppo']}.{r['Unita']}.{r['SubUni']}.{r['Key']}": str(r["Valore"]) for r in rows
    }
    pending: dict[str, tuple[datetime, str | None]] = {}  # path -> (deadline, value before)
    changed: set[str] = set()

    def expire(now: datetime) -> None:
        for path, (deadline, _before) in list(pending.items()):
            if deadline <= now:
                del pending[path]
                current.pop(path, None)
                changed.add(path)

    for line in (FIXTURE / "ws.jsonl").read_text("utf-8").splitlines():
        entry = json.loads(line)
        frame = entry.get("frame")
        if frame is None:
            continue
        now = datetime.fromisoformat(entry["t"])
        expire(now)
        path = frame["path"]
        if frame.get("gruppo") == "METEO_DATA":
            continue
        if frame["type"] == "remove":
            if path in current and path not in pending:
                pending[path] = (now + timedelta(seconds=1), current[path])
            continue
        value = "" if frame.get("value") is None else str(frame["value"])
        before = pending.pop(path)[1] if path in pending else current.get(path)
        if path not in current or not _same(before, value):
            changed.add(path)
        current[path] = value
    return changed


def test_e2_changed_paths_match_the_oracle(run: Run) -> None:
    oracle = oracle_changed_paths()
    assert len(oracle) == 41
    union: set[str] = set()
    for update in frames_of(run):
        union |= update.changed
    assert union == oracle
    for noop in ("WIFI...AVAILABLE", "PROC...WATCHDOG_ALARM", "PROC...INTERNET_ACCESS"):
        assert all(noop not in u.changed for u in run.updates if u.reason is not UpdateReason.SYNC)
    for update in run.updates:
        if update.reason is UpdateReason.SYNC:
            continue
        has_changes = bool(
            update.changed
            or update.removed
            or update.conf_changed
            or update.config_changed
            or update.forecast_changed
        )
        assert has_changes or update.state != update.previous


def test_f_vmc_alarm_flap_is_never_debounced(run: Run) -> None:
    alarm_id = "vmc:001:ALLARM_SONDA_RICIRCOLO"
    assert alarm_id in {a.id for a in run.states["10:37:10"].alarms}
    assert alarm_id not in {a.id for a in run.states["10:37:25"].alarms}
    for update in run.updates:
        assert update.state.alarms_debounced == ()
        assert not any(a.id.startswith("plant:process:") for a in update.state.alarms)
    assert series(run, lambda s: s.vmcs["001"].alarm_bitmask) == [248, 250, 248]


# ---------------------------------------------------------------------------
# g: counters, no RESYNC update, only GETs, clean close
# ---------------------------------------------------------------------------


def test_g_counters_and_clean_close(run: Run) -> None:
    stats = run.stats
    assert stats.frames_received == 563
    assert stats.noop_updates == 169
    assert stats.changed_updates == 325
    assert stats.removes_coalesced == 29
    assert stats.removes_applied == 0
    assert stats.forecast_frames == 40
    assert stats.frames_ignored == 0
    assert (stats.syncs, stats.resyncs, stats.fallbacks) == (1, 2, 0)
    # the controller's 60-s heartbeat keeps the (default, 180 s) idle watchdog quiet
    assert (stats.ws_idle_timeouts, stats.reconnects) == (0, 0)
    assert (stats.sync_failures, stats.sync_failure_streak, stats.last_sync_error) == (0, 0, None)
    assert [u for u in run.updates if u.reason is UpdateReason.RESYNC] == []
    assert run.calls
    assert all(name.startswith("get_") for name in run.calls)
    assert run.pending_after_close == 0
    assert run.device.frames_dropped == 0
    assert run.final.synced_at > T("10:42:00")  # the second resync replaced the stores


def test_time_driven_updates_are_published(run: Run) -> None:
    clock_updates = [u for u in run.updates if u.reason is UpdateReason.CLOCK]
    assert clock_updates  # the 10:30:00 slot boundary at least
    assert all(u.at.utcoffset() == timedelta(0) for u in run.updates)
