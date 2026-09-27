"""Supervised write tests: one write, verified, then reverted (``rehom-probe write-test``).

The protocol, for one operation:

1. **Pre-flight** on a fresh complete sync: no crono/bus-down/read-only, no
   installer session and no commissioning (``RIS_*``) activity in the last 10
   minutes, no active (debounced) alarm, the whole run at least two minutes
   clear of every half-hour schedule boundary, no open journal entry, a
   consistent house setpoint (house and comfort operations), the operation's
   own preconditions (checked by planning both the forward and the revert
   write), and something to test (the forward values are not already there).
2. **Write-ahead journal**: the forward and revert records, the original
   values and the raw pre-write value of every path either write touches are
   appended (fsync'd) before anything is sent.
3. **Forward write** through :class:`~aiorehom.client.RehomClient` (confirmed
   by the controller's echo, or by one fresh snapshot).
4. **Dwell**, then **verify**: every record and plant-conf key (``CONF.<key>``)
   is compared semantically (``"0" == "0.0"``) with the baseline; a change
   outside the written paths, the operation's known side effects and the
   volatile set, an alarm that was not active at pre-flight, or a new
   ``/alive/`` web version stops the test without a revert.
5. **Revert** (planned from the *original* values), then the same checks
   against the baseline, allowing only the operation's known residue.
6. The journal entry is closed.

Any failure or interruption stops the test: it prints what to restore by
hand, journals a ``stopped`` line and leaves the entry open (so the next run
refuses to write).  Without ``execute`` the test is a dry run: it prints both
writes and sends nothing.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import math
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from ._files import ensure_private_dir
from .client import RehomClient
from .enums import LockState, MasterPreset, VmcMode, ZoneSetp
from .exceptions import RehomError, RehomWriteRefusedError
from .models import RehomState, Vmc, Zone
from .redact import is_secret_key, is_secret_record
from .state import record_values_equal
from .writes import (
    WritePlan,
    plan_comfort_temperature,
    plan_house_preset,
    plan_predictive,
    plan_vmc_fan,
    plan_vmc_mode,
    plan_zone_mode,
    plan_zone_offset,
)

if sys.platform != "win32":
    import fcntl

__all__ = [
    "BOUNDARY_GUARD_MINUTES",
    "CONF_PREFIX",
    "DEFAULT_DWELL_S",
    "OPERATIONS",
    "VMC_MODE_DWELL_S",
    "VOLATILE_PATTERNS",
    "Operation",
    "WriteTestError",
    "build_operation",
    "check_arguments",
    "close_entry",
    "open_entries",
    "preflight_problems",
    "run_write_test",
    "unexpected_changes",
]

#: Record paths that change on their own (sensors, housekeeping, weather, alarms
#: flapping).  Alarms are checked separately: an alarm id that was not active
#: at pre-flight stops the test.  Plant-conf keys are never volatile.
VOLATILE_PATTERNS: Final = (
    "*.TEMP_AMBIENTE",
    "*.UMIDITA",
    "*.ATTIVA",
    "PROC.*",
    "WIFI.*",
    "METEO*",
    "REHOM.*.PROCESS_STATE",
    "ZONA.*.SETP_TEMP_FORZATO",
    "FANCOIL.*",
    "ATTUATORE.*",
    "DEUM.*.TEMP*",
    "DEUM.*.PRESSIONE_ARIA_PA",
    "DEUM.*.ST_STATO_DEUM",
    "DEUM.*.ST_DEUMIDIFICA",
    "DEUM.*.ST_RAFF_RISC",
    "DEUM.*.ST_SER_*",
    "DEUM.*.ST_EV_*",
    "DEUM.*.ST_RELE_*",
    "DEUM.*.ST_DUTY_CICLE",
    "DEUM.*.ST_MODE_FORZATO",
    "DEUM.*.COM_COMPR",
    "DEUM.*.COM_SBRINA",
    "DEUM.*.COM_RINNOVO",
    "DEUM.*.ALLARM*",
    "DEUM.*.RELE",
    "DEUM.*.CICLO_ATTIVO",
)
#: Plant-conf keys appear in the snapshots as ``CONF.<key>``.
CONF_PREFIX: Final = "CONF."
BOUNDARY_GUARD_MINUTES: Final = 2
DEFAULT_DWELL_S: Final = 15.0
VMC_MODE_DWELL_S: Final = 600.0
#: Per write, on top of ``write_confirm_timeout``: the fallback resync.
CONFIRM_ALLOWANCE_S: Final = 30.0
#: Commissioning (``RIS_*``) activity this recent refuses the test.
INSTALLER_QUIET_MINUTES: Final = 10
_HALF_HOUR_S: Final = 30 * 60
_UNIT_ID_RE: Final = re.compile(r"[0-9]{3}")
_WHOLE_RE: Final = re.compile(r"[+-]?[0-9]{1,3}")
_DECIMAL_RE: Final = re.compile(r"[+-]?[0-9]{1,2}(?:\.[0-9]{1,2})?")
#: VMC-mode tests switch only between the current mode and these
#: (non-compressor modes, never the timed "rapid" ones).
_VMC_TEST_MODES: Final = frozenset({VmcMode.VENTILATE, VmcMode.STANDBY})
#: VMC modes a vmc-mode test cannot start from (it would restore them).
_VMC_UNRESTORABLE: Final = frozenset({VmcMode.STOP, VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT})


class WriteTestError(RehomError):
    """The write test was refused or stopped (a stopped test leaves its entry open)."""


Planner = Callable[[RehomState], WritePlan]
Caller = Callable[[RehomClient], Awaitable[bool]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Operation:
    """One reversible write: forward and revert, as planners and client calls.

    ``side_effects`` may differ from the baseline after the forward write (on
    top of the written paths); ``revert_side_effects`` may still differ after
    the revert, when everything else must be back to the baseline.
    ``needs_consistent_setpoint`` refuses the test while the house's
    ``SET_POINT_TEMP`` differs from its level's temperature.
    """

    name: str
    summary: str
    forward_plan: Planner
    revert_plan: Planner
    forward: Caller
    revert: Caller
    side_effects: tuple[str, ...] = ()
    revert_side_effects: tuple[str, ...] = ()
    dwell_s: float = DEFAULT_DWELL_S
    originals: Mapping[str, object] = field(default_factory=dict)
    needs_consistent_setpoint: bool = False


# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------


def _whole(value: str, what: str) -> int:
    if not _WHOLE_RE.fullmatch(value):
        raise ValueError(f"{what} must be a whole number, not {value!r}")
    return int(value)


def _zone_setp(name: str) -> ZoneSetp:
    table = {
        "schedule": ZoneSetp.UNSET,
        "economy": ZoneSetp.ECONOMY,
        "pre_comfort": ZoneSetp.PRE_COMFORT,
        "comfort": ZoneSetp.COMFORT,
    }
    if name not in table:
        raise ValueError(f"zone mode must be one of {sorted(table)}")
    return table[name]


def _vmc_test_mode(name: str) -> VmcMode:
    allowed = sorted(m.name.lower() for m in _VMC_TEST_MODES)
    try:
        mode = VmcMode[name.upper()]
    except KeyError:
        raise ValueError(f"the VMC test mode must be one of {allowed}") from None
    if mode not in _VMC_TEST_MODES:
        raise ValueError(
            f"vmc-mode tests switch only to {' or '.join(allowed)} "
            "(never the rapid, compressor or stop modes)"
        )
    return mode


def _on_off(value: str) -> bool:
    table = {"on": True, "off": False}
    if value not in table:
        raise ValueError(f"predictive takes on or off, not {value!r}")
    return table[value]


def _house_preset(value: str) -> MasterPreset:
    try:
        preset = MasterPreset(value)
    except ValueError:
        choices = sorted(p.value for p in MasterPreset if p is not MasterPreset.OFF)
        raise ValueError(f"house preset must be one of {choices}") from None
    if preset is MasterPreset.OFF:
        raise ValueError("a write test never switches the house off")
    return preset


def _comfort_delta(value: str) -> float:
    if not _DECIMAL_RE.fullmatch(value):
        raise ValueError(
            f"the comfort-temperature delta must be a number such as 0.1, not {value!r}"
        )
    return float(value)


_VALUE_PARSERS: Final[Mapping[str, Callable[[str], object]]] = {
    "zone-offset": lambda v: _whole(v, "the zone offset"),
    "zone-mode": _zone_setp,
    "vmc-fan": lambda v: _whole(v, "the fan value"),
    "vmc-mode": _vmc_test_mode,
    "predictive": _on_off,
    "house": _house_preset,
    "comfort-temp": _comfort_delta,
}
#: Operations that need ``--target``, and what it names.
_TARGETS: Final[Mapping[str, str]] = {
    "zone-offset": "zone",
    "zone-mode": "zone",
    "vmc-fan": "VMC",
    "vmc-mode": "VMC",
}

OPERATIONS: Final = {
    "zone-offset": "--target ZONE --value -3..3",
    "zone-mode": "--target ZONE --value schedule|economy|pre_comfort|comfort",
    "vmc-fan": "--target VMC --value SPEED",
    "vmc-mode": "--target VMC --value ventilate|standby (dwell >= 600 s)",
    "predictive": "--value on|off",
    "house": "--value auto|economy|pre_comfort|comfort",
    "comfort-temp": "--value DELTA (e.g. 0.1)",
}


def _need(target: str | None, what: str) -> str:
    if not target:
        raise ValueError(f"this operation needs --target {what}")
    if not _UNIT_ID_RE.fullmatch(target):
        raise ValueError(f"--target must be a 3-digit {what} id such as 001, not {target!r}")
    return target


def check_arguments(name: str, target: str | None, value: str) -> None:
    """Validate an operation's arguments without a state; raises :class:`ValueError`.

    :func:`build_operation` repeats these checks and adds the state-dependent ones.
    """
    if name not in OPERATIONS:
        raise ValueError(f"unknown operation {name!r}; choose from {sorted(OPERATIONS)}")
    _VALUE_PARSERS[name](value)
    if name in _TARGETS:
        _need(target, _TARGETS[name])


def _zone_of(state: RehomState, target: str | None) -> Zone:
    zone_id = _need(target, "zone")
    zone = state.zones.get(zone_id)
    if zone is None:
        raise ValueError(f"zone {zone_id} is not present")
    return zone


def _vmc_of(state: RehomState, target: str | None) -> Vmc:
    vmc_id = _need(target, "VMC")
    vmc = state.vmcs.get(vmc_id)
    if vmc is None:
        raise ValueError(f"VMC {vmc_id} is not present")
    return vmc


def build_operation(state: RehomState, name: str, target: str | None, value: str) -> Operation:
    """Build a reversible operation from the current state (originals captured here).

    Raises :class:`ValueError` for unknown operations, targets or values, and
    for a current state the test could not restore.
    """
    if name not in OPERATIONS:
        raise ValueError(f"unknown operation {name!r}; choose from {sorted(OPERATIONS)}")
    if name == "zone-offset":
        new = _whole(value, "the zone offset")
        zone = _zone_of(state, target)
        if zone.offset is None or not zone.offset.is_integer():
            raise ValueError(
                f"zone {zone.id}'s current offset is unknown or not a whole number: "
                "it could not be restored exactly"
            )
        original = int(zone.offset)
        return Operation(
            name=name,
            summary=f"zone {zone.id} offset {original:+d} -> {new:+d}",
            forward_plan=lambda st: plan_zone_offset(st, zone.id, new),
            revert_plan=lambda st: plan_zone_offset(st, zone.id, original),
            forward=lambda c: c.set_zone_offset(zone.id, new),
            revert=lambda c: c.set_zone_offset(zone.id, original),
            originals={"offset": original},
        )
    if name == "zone-mode":
        new_setp = _zone_setp(value)
        zone = _zone_of(state, target)
        original_setp = zone.setp
        if original_setp not in (
            ZoneSetp.UNSET,
            ZoneSetp.ECONOMY,
            ZoneSetp.PRE_COMFORT,
            ZoneSetp.COMFORT,
        ):
            raise ValueError(f"zone {zone.id} is in a mode that cannot be restored")
        residue = (f"ZONA.{zone.id}..SETP_CORRENTE_OLD", f"ZONA.{zone.id}..SET_POINT_TEMP")
        return Operation(
            name=name,
            summary=f"zone {zone.id} mode {original_setp.name} -> {new_setp.name}",
            forward_plan=lambda st: plan_zone_mode(st, zone.id, new_setp),
            revert_plan=lambda st: plan_zone_mode(st, zone.id, original_setp),
            forward=lambda c: c.set_zone_mode(zone.id, new_setp),
            revert=lambda c: c.set_zone_mode(zone.id, original_setp),
            side_effects=residue,
            revert_side_effects=residue,
            originals={"setp": int(original_setp)},
        )
    if name == "vmc-fan":
        new_fan = _whole(value, "the fan value")
        vmc = _vmc_of(state, target)
        original_fan = vmc.fan.value
        if original_fan is None:
            raise ValueError(f"VMC {vmc.id}'s fan value is unknown")
        damper = (f"DEUM.{vmc.id}..ST_SER_RINNO",)
        return Operation(
            name=name,
            summary=f"VMC {vmc.id} fan {original_fan} -> {new_fan}",
            forward_plan=lambda st: plan_vmc_fan(st, vmc.id, new_fan),
            revert_plan=lambda st: plan_vmc_fan(st, vmc.id, original_fan),
            forward=lambda c: c.set_vmc_fan(vmc.id, new_fan),
            revert=lambda c: c.set_vmc_fan(vmc.id, original_fan),
            side_effects=damper,
            revert_side_effects=damper,
            originals={"fan": original_fan},
        )
    if name == "vmc-mode":
        new_mode = _vmc_test_mode(value)
        vmc = _vmc_of(state, target)
        original_mode = vmc.mode
        if original_mode is None:
            raise ValueError(f"VMC {vmc.id}'s mode is unknown")
        if original_mode in _VMC_UNRESTORABLE:
            raise ValueError(
                f"VMC {vmc.id} is in {original_mode.name}: a vmc-mode test would restore it "
                "(timed rapid modes and STOP are never written)"
            )
        if original_mode is new_mode:
            raise ValueError(f"VMC {vmc.id} is already in {new_mode.name}: nothing to test")
        if vmc.schedule_active is True:
            raise ValueError(f"VMC {vmc.id} runs a schedule; a mode test would fight it")
        # No side effects: the DEUM status rows are volatile, and ST_MODE and
        # COM_VENTILA must be back to the baseline after the revert.
        return Operation(
            name=name,
            summary=f"VMC {vmc.id} mode {original_mode.name} -> {new_mode.name}",
            forward_plan=lambda st: plan_vmc_mode(st, vmc.id, new_mode),
            revert_plan=lambda st: plan_vmc_mode(st, vmc.id, original_mode),
            forward=lambda c: c.set_vmc_mode(vmc.id, new_mode),
            revert=lambda c: c.set_vmc_mode(vmc.id, original_mode),
            dwell_s=VMC_MODE_DWELL_S,
            originals={"mode": int(original_mode)},
        )
    if name == "predictive":
        new_on = _on_off(value)
        original_on = state.plant.predictive
        if original_on is None:
            raise ValueError("the predictive algorithm state is unknown")
        return Operation(
            name=name,
            summary=f"predictive {original_on} -> {new_on}",
            forward_plan=lambda st: plan_predictive(st, new_on),
            revert_plan=lambda st: plan_predictive(st, original_on),
            forward=lambda c: c.set_predictive(new_on),
            revert=lambda c: c.set_predictive(original_on),
            originals={"predictive": original_on},
        )
    if name == "house":
        new_preset = _house_preset(value)
        original_preset = state.plant.preset
        if original_preset in (None, MasterPreset.OFF):
            raise ValueError("the house preset cannot be restored from its current state")
        # The controller rewrites the master SET_POINT_TEMP on every mode change
        # (AUTO: the TEMP_OFF placeholder); the revert writes it back, so only
        # the zones' mirrors may still lag afterwards.
        return Operation(
            name=name,
            summary=f"house {original_preset.value} -> {new_preset.value}",
            forward_plan=lambda st: plan_house_preset(st, new_preset),
            revert_plan=lambda st: plan_house_preset(st, original_preset),
            forward=lambda c: c.set_house_preset(new_preset),
            revert=lambda c: c.set_house_preset(original_preset),
            side_effects=("REHOM...SET_POINT_TEMP", "ZONA.*..SET_POINT_TEMP"),
            revert_side_effects=("ZONA.*..SET_POINT_TEMP",),
            originals={"preset": original_preset.value},
            needs_consistent_setpoint=True,
        )
    # comfort-temp
    delta = _comfort_delta(value)
    original_t = state.plant.temperature_comfort
    if original_t is None:
        raise ValueError("the comfort temperature is unknown")
    new_t = round(original_t + delta, 1)
    return Operation(
        name=name,
        summary=f"comfort temperature {original_t:g} -> {new_t:g}",
        forward_plan=lambda st: plan_comfort_temperature(st, new_t),
        revert_plan=lambda st: plan_comfort_temperature(st, original_t),
        forward=lambda c: c.set_comfort_temperature(new_t),
        revert=lambda c: c.set_comfort_temperature(original_t),
        side_effects=("ZONA.*..SET_POINT_TEMP",),
        revert_side_effects=("ZONA.*..SET_POINT_TEMP",),
        originals={"comfort": original_t},
        needs_consistent_setpoint=True,
    )


# ---------------------------------------------------------------------------
# pre-flight and diff
# ---------------------------------------------------------------------------


def preflight_problems(state: RehomState, now_utc: datetime, duration_s: float = 0.0) -> list[str]:
    """Plant-level reasons not to write now (empty list = go).

    ``duration_s`` is the planned length of the run: the whole interval
    ``[now, now + duration_s]`` must stay :data:`BOUNDARY_GUARD_MINUTES` away
    from every local :00/:30 schedule boundary.
    """
    problems: list[str] = []
    plant = state.plant
    if plant.lock is not LockState.NORMAL:
        problems.append(f"lock is {plant.lock.value}")
    if plant.read_only is True:
        problems.append("controller read-only flag is set")
    if plant.installer_session is True:
        problems.append("an installer session is active")
    activity = plant.installer_activity_at
    if activity is not None and now_utc - activity < timedelta(minutes=INSTALLER_QUIET_MINUTES):
        problems.append(
            f"installer (commissioning) activity in the last {INSTALLER_QUIET_MINUTES} minutes"
        )
    if state.alarms_debounced:
        problems.append(f"{len(state.alarms_debounced)} active alarm(s)")
    local = state.device_clock.local_now(now_utc)
    into = (local.minute % 30) * 60 + local.second + local.microsecond / 1e6
    guard = BOUNDARY_GUARD_MINUTES * 60
    if into < guard or into + duration_s > _HALF_HOUR_S - guard:
        problems.append(
            f"the run (about {duration_s:.0f} s) would come within {BOUNDARY_GUARD_MINUTES} "
            "minutes of a half-hour schedule boundary"
        )
    return problems


def _allowed(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def _key(path: str) -> str:
    """The record ``Key`` of a snapshot path (empty for a plant-conf path)."""
    parts = path.split(".", 3)
    return parts[3] if len(parts) == 4 and not path.startswith(CONF_PREFIX) else ""


def _same(path: str, a: str | None, b: str | None) -> bool:
    """Semantic equality (``"0" == "0.0"``); a missing value equals only a missing one."""
    if a == b:
        return True
    return a is not None and b is not None and record_values_equal(_key(path), a, b)


def unexpected_changes(
    before: Mapping[str, str],
    after: Mapping[str, str],
    allowed: Sequence[str],
) -> list[str]:
    """Paths that changed (or appeared/vanished) outside ``allowed`` and the volatile set.

    Values are compared semantically (:func:`~aiorehom.state.record_values_equal`),
    so the controller's re-spelling of a written value is not a change.
    Plant-conf paths (``CONF.<key>``) are never volatile.
    """
    patterns = (*VOLATILE_PATTERNS, *allowed)
    out: list[str] = []
    for path in sorted(set(before) | set(after)):
        if _same(path, before.get(path), after.get(path)):
            continue
        if _allowed(path, allowed if path.startswith(CONF_PREFIX) else patterns):
            continue
        out.append(path)
    return out


def _is_secret(path: str) -> bool:
    if path.startswith(CONF_PREFIX):
        return is_secret_key(path.removeprefix(CONF_PREFIX))
    gruppo, _unita, _subuni, key = [*path.split(".", 3), "", "", ""][:4]
    return is_secret_record(gruppo, key)


def _show(path: str, value: str | None) -> str:
    if value is not None and _is_secret(path):
        return "<redacted>"
    return repr(value)


def _observe(client: RehomClient) -> tuple[dict[str, str], str | None]:
    """Raw records plus plant conf (``CONF.<key>``), and the ``/alive/`` web version.

    Read from the client's stores rather than the published state, which lags
    by the notify batch (and, for ``/alive/``, by the resync a new version
    triggers).
    """
    stores = client.dump()
    values = client.record_values()
    values.update({f"{CONF_PREFIX}{key}": str(v) for key, v in stores["plant_conf"].items()})
    version = stores["alive"].get("version")
    return values, version if isinstance(version, str) else None


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def _sync(fd: int) -> None:
    """Force ``fd`` to stable storage (``F_FULLFSYNC`` where it exists: macOS)."""
    if sys.platform != "win32":
        full = getattr(fcntl, "F_FULLFSYNC", None)
        if full is not None:
            try:
                fcntl.fcntl(fd, full)
            except OSError:
                pass  # a file system without F_FULLFSYNC: plain fsync below
            else:
                return
    os.fsync(fd)


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        _sync(fd)
    finally:
        os.close(fd)


def _append(journal: Path, entry: Mapping[str, Any]) -> None:
    """Append one line and force it (and, when the file is new, its directory) to disk."""
    ensure_private_dir(journal.parent)
    created = not journal.exists()
    data = memoryview((json.dumps(entry, sort_keys=True) + "\n").encode())
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        while data:
            data = data[os.write(fd, data) :]
        _sync(fd)
    finally:
        os.close(fd)
    if created:
        _sync_directory(journal.parent)


def open_entries(journal: Path) -> list[dict[str, Any]]:
    """Journal entries that were planned but never closed.

    A line that is not a JSON object with a string ``id`` and ``event`` (a torn
    append, a bad hand edit) raises :class:`WriteTestError` naming its line
    number: every test is refused until the journal is repaired by hand.
    """
    if not journal.exists():
        return []
    planned: dict[str, dict[str, Any]] = {}
    for number, raw in enumerate(journal.read_bytes().splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8"))
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            entry = None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("event"), str)
        ):
            raise WriteTestError(
                f"the journal {journal} is corrupt at line {number}: inspect and repair it by "
                "hand (no test runs until then)"
            )
        if entry["event"] == "planned":
            planned[entry["id"]] = entry
        elif entry["event"] in ("closed", "closed_by_owner"):
            planned.pop(entry["id"], None)
    return list(planned.values())


def close_entry(journal: Path, entry_id: str, note: str) -> None:
    """Mark an open entry closed after the owner restored the state by hand."""
    if entry_id not in {entry["id"] for entry in open_entries(journal)}:
        raise WriteTestError(f"{entry_id!r} is not an open entry of {journal} (see --status)")
    _append(journal, {"id": entry_id, "event": "closed_by_owner", "note": note})


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


def _records(plan: WritePlan) -> list[dict[str, object]]:
    return [dict(r) for r in plan.records]


def _check(
    client: RehomClient,
    before: Mapping[str, str],
    *,
    allowed: Sequence[str],
    alarms: frozenset[str],
    version: str | None,
    label: str,
) -> str | None:
    """The reason to stop now, compared with the pre-flight baseline, or ``None``."""
    after, now_version = _observe(client)
    changed = unexpected_changes(before, after, allowed)
    if changed:
        details = ", ".join(
            f"{p}: {_show(p, before.get(p))} -> {_show(p, after.get(p))}" for p in changed[:10]
        )
        return f"{label}: {details}"
    new_alarms = sorted({alarm.id for alarm in client.state.alarms} - alarms)
    if new_alarms:
        return f"new alarm(s): {', '.join(new_alarms)}"
    if now_version != version:
        return f"the controller's web version changed ({version} -> {now_version})"
    return None


async def run_write_test(
    client: RehomClient,
    op_builder: Callable[[RehomState], Operation],
    *,
    journal: Path,
    execute: bool,
    echo: Callable[[str], None],
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], datetime],
    dwell_s: float | None = None,
) -> Operation:
    """Run one supervised write test (see the module docstring).  Returns the operation.

    Raises :class:`WriteTestError` when the test is refused or stopped, and
    :class:`ValueError` for an invalid operation or dwell.  Once the entry is
    journaled, any exception (cancellation included) first prints what to
    restore and journals a ``stopped`` line, then propagates.
    """
    if dwell_s is not None and not (math.isfinite(dwell_s) and dwell_s >= 0):
        raise ValueError("the dwell must be a finite number of seconds >= 0")
    if open_entries(journal):
        raise WriteTestError(
            f"the journal {journal} has an open entry: restore/verify it and close it first"
        )
    state = client.state
    op = op_builder(state)
    wait = op.dwell_s if dwell_s is None else dwell_s
    duration = (
        wait + 2 * (client.options.write_confirm_timeout + CONFIRM_ALLOWANCE_S) + DEFAULT_DWELL_S
    )
    problems = preflight_problems(state, now(), duration)
    if op.needs_consistent_setpoint and state.plant.setpoint_mismatch is True:
        problems.append(
            "the house SET_POINT_TEMP differs from the selected level's temperature "
            "(setpoint mismatch): correct it first"
        )
    if problems:
        raise WriteTestError("pre-flight failed: " + "; ".join(problems))
    alarms = frozenset(alarm.id for alarm in state.alarms)
    try:
        forward = op.forward_plan(state)
        revert = op.revert_plan(state)
    except RehomWriteRefusedError as err:
        raise WriteTestError(f"refused ({err.reason}): {err}") from err
    before, version = _observe(client)
    if all(_same(path, before.get(path), value) for path, value in forward.expect.items()):
        raise WriteTestError(
            f"nothing to test: {op.summary}: the controller already reports the forward values"
        )
    raw_before = {
        path: "<redacted>" if _is_secret(path) else before.get(path)
        for path in sorted({*forward.expect, *revert.expect})
    }
    echo(f"operation: {op.summary}")
    echo(f"forward  -> {forward.path}: {json.dumps(_records(forward))}")
    echo(f"revert   -> {revert.path}: {json.dumps(_records(revert))}")
    if not execute:
        echo("dry run: nothing sent (use --execute to run it)")
        return op

    entry_id = now().strftime("%Y%m%dT%H%M%S%fZ")
    _append(
        journal,
        {
            "id": entry_id,
            "event": "planned",
            "at": now().isoformat(),
            "op": op.name,
            "summary": op.summary,
            "forward": _records(forward),
            "revert": _records(revert),
            "originals": dict(op.originals),
            "before": raw_before,
        },
    )
    stopped = False

    def announce_stop(reason: str) -> None:
        # Tell the owner first: the journal is only the second line of defence.
        nonlocal stopped
        stopped = True
        try:
            echo(f"STOPPED: {reason}")
            echo(
                f"restore by hand: {op.summary} -> back to {dict(op.originals)} (raw {raw_before})"
            )
            echo(f"then close the entry: rehom-probe write-test --close {entry_id}")
        finally:
            try:
                _append(
                    journal,
                    {"id": entry_id, "event": "stopped", "at": now().isoformat(), "reason": reason},
                )
            except OSError as err:
                echo(f"could not journal the stop ({type(err).__name__}); the entry stays open")

    def stop(reason: str) -> WriteTestError:
        announce_stop(reason)
        return WriteTestError(reason)

    try:
        try:
            sent = await op.forward(client)
        except RehomError as err:
            raise stop(f"forward write failed: {type(err).__name__}: {err}") from err
        if not sent:
            raise stop("the forward write sent nothing (already in place): nothing was tested")
        _append(journal, {"id": entry_id, "event": "forward_confirmed", "at": now().isoformat()})
        echo("forward write confirmed")

        echo(f"dwell {wait:g} s")
        await sleep(wait)
        reason = _check(
            client,
            before,
            allowed=(*forward.expect, *op.side_effects),
            alarms=alarms,
            version=version,
            label="unexpected change(s)",
        )
        if reason is not None:
            raise stop(reason)  # no revert: the owner decides

        try:
            now_revert = op.revert_plan(client.state)
            echo(f"revert   -> {now_revert.path}: {json.dumps(_records(now_revert))}")
            await op.revert(client)
        except RehomError as err:
            raise stop(f"revert failed: {type(err).__name__}: {err}") from err
        _append(journal, {"id": entry_id, "event": "reverted", "at": now().isoformat()})
        await sleep(DEFAULT_DWELL_S)
        reason = _check(
            client,
            before,
            allowed=op.revert_side_effects,
            alarms=alarms,
            version=version,
            label="state differs after revert",
        )
        if reason is not None:
            raise stop(reason)
        _append(journal, {"id": entry_id, "event": "closed", "at": now().isoformat()})
    except BaseException as err:
        if not stopped:
            interrupted = isinstance(err, (asyncio.CancelledError, KeyboardInterrupt))
            announce_stop("interrupted" if interrupted else type(err).__name__)
        raise
    echo("reverted and verified; journal entry closed")
    return op
