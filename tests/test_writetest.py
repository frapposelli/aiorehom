"""Supervised write tests: dry run, journal, verify, stop conditions, revert."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aiorehom import writetest
from aiorehom.exceptions import RehomConnectionError
from aiorehom.writes import format_temperature
from aiorehom.writetest import (
    Operation,
    WriteTestError,
    build_operation,
    check_arguments,
    close_entry,
    open_entries,
    preflight_problems,
    run_write_test,
    unexpected_changes,
)

from .sync_fakes import bus, termo
from .test_client_writes import Rig

#: The helpers' ``now`` runs 10 minutes ahead of the rig's clock: 12:10 local.
AHEAD = timedelta(minutes=10)
Hook = Callable[[int, list[dict[str, Any]]], None]


def _entries(journal: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in journal.read_text().splitlines()]


def _events(journal: Path) -> list[str]:
    return [entry["event"] for entry in _entries(journal)]


def _sent(rig: Rig) -> list[list[tuple[str, str]]]:
    return [[(r["path"], str(r["Valore"])) for r in w[1]] for w in rig.ctrl.writes]


def _set(rig: Rig, path: str, value: str) -> None:
    """The controller changes ``path`` by itself: REST snapshot and WS frame."""
    gruppo, unita, subuni, key = path.split(".", 3)
    rig.ctrl.set_value(gruppo, unita, subuni, key, value)
    rig.connector.current.push(termo(path, value))


def _rest_value(rig: Rig, path: str) -> str | None:
    wanted = tuple(path.split(".", 3))
    for row in rig.ctrl.interface:
        if (row["Gruppo"], row["Unita"], row["SubUni"], row["Key"]) == wanted:
            return str(row["Valore"])
    return None


def _after_posts(rig: Rig, hook: Hook) -> None:
    """Call ``hook(n, records)`` after the controller handled its n-th bulk write."""
    original = rig.ctrl.post_bulk_update
    count = 0

    async def post(path: str, records: Any, *, timeout: float | None = None) -> int:  # noqa: ASYNC109
        nonlocal count
        count += 1
        status = await original(path, records, timeout=timeout)
        hook(count, list(records))
        return status

    rig.ctrl.post_bulk_update = post  # type: ignore[method-assign]


def _fail_post(rig: Rig, number: int, error: BaseException) -> None:
    """The controller rejects its ``number``-th bulk write (nothing is applied)."""
    original = rig.ctrl.post_bulk_update
    count = 0

    async def post(path: str, records: Any, *, timeout: float | None = None) -> int:  # noqa: ASYNC109
        nonlocal count
        count += 1
        if count == number:
            raise error
        return await original(path, records, timeout=timeout)

    rig.ctrl.post_bulk_update = post  # type: ignore[method-assign]


def _normalise_later(rig: Rig, key: str, delay: float = 6.0) -> list[asyncio.Task[None]]:
    """Like the live controller: ``delay`` s after a write of ``key``, re-emit it normalised.

    The re-emitted value is the controller's *current* one ("1.0" -> "1").
    """
    tasks: list[asyncio.Task[None]] = []

    async def reemit(path: str) -> None:
        await rig.clock.sleep(delay)
        current = _rest_value(rig, path)
        assert current is not None
        _set(rig, path, format_temperature(float(current)))

    def hook(_n: int, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record["Key"] == key:
                tasks.append(asyncio.ensure_future(reemit(str(record["path"]))))

    _after_posts(rig, hook)
    return tasks


def _auto(rig: Rig) -> None:
    rig.ctrl.set_value("REHOM", "", "", "MODO", "2")
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT", "0")


def _consistent_setpoint(rig: Rig) -> None:
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT_TEMP", "24")  # = TEMP_COM


async def _run(
    rig: Rig,
    journal: Path,
    *,
    execute: bool,
    op: tuple[str, str | None, str],
    dwell: float | None = 1.0,
    advance: float = 120.0,
    lines: list[str] | None = None,
    ahead: timedelta = AHEAD,
    builder: Callable[[Any], Operation] | None = None,
) -> Operation:
    echoed = lines if lines is not None else []
    coro = run_write_test(
        rig.client,
        builder or (lambda st: build_operation(st, *op)),
        journal=journal,
        execute=execute,
        echo=echoed.append,
        sleep=rig.clock.sleep,
        now=lambda: rig.clock.utcnow() + ahead,
        dwell_s=dwell,
    )
    result: Operation = await rig.run(coro, advance=advance)
    return result


# ---------------------------------------------------------------------------
# dry run and the happy paths, one per operation
# ---------------------------------------------------------------------------


async def test_dry_run_sends_nothing(tmp_path: Path) -> None:
    rig = Rig(allow_writes=False)
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    lines: list[str] = []
    op = await _run(rig, journal, execute=False, op=("vmc-fan", "001", "2"), lines=lines)
    assert op.summary == "VMC 001 fan 1 -> 2"
    assert rig.ctrl.writes == []
    assert not journal.exists()
    assert any("dry run" in line for line in lines)
    # the full revert payload is shown before anything is sent
    (revert_line,) = [line for line in lines if line.startswith("revert")]
    records = json.loads(revert_line.split(": ", 1)[1])
    assert [(r["path"], r["Valore"]) for r in records] == [("DEUM.001..COM_VENTILA", "1")]
    await rig.client.close()


async def test_happy_path_forward_verify_revert(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert _sent(rig) == [[("DEUM.001..COM_VENTILA", "2")], [("DEUM.001..COM_VENTILA", "1")]]
    assert _events(journal) == ["planned", "forward_confirmed", "reverted", "closed"]
    assert open_entries(journal) == []
    assert oct(journal.stat().st_mode & 0o777) == "0o600"
    planned = _entries(journal)[0]
    assert [r["Valore"] for r in planned["forward"]] == ["2"]
    assert [r["Valore"] for r in planned["revert"]] == ["1"]
    assert planned["before"] == {"DEUM.001..COM_VENTILA": "1"}
    await rig.client.close()


async def test_zone_offset_with_normalised_reemit_closes(tmp_path: Path) -> None:
    """The controller re-spells "1.0"/"0.0" as "1"/"0" later: not a change (was a false STOP)."""
    rig = Rig()
    await rig.connect()
    tasks = _normalise_later(rig, "DELTA_SETP_CORRENTE")
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("zone-offset", "001", "1"), dwell=10.0)
    assert _sent(rig) == [
        [("ZONA.001..DELTA_SETP_CORRENTE", "1.0")],
        [("ZONA.001..DELTA_SETP_CORRENTE", "0.0")],
    ]
    assert len(tasks) == 2 and all(task.done() for task in tasks)
    assert _events(journal) == ["planned", "forward_confirmed", "reverted", "closed"]
    assert open_entries(journal) == []
    assert _entries(journal)[0]["before"] == {"ZONA.001..DELTA_SETP_CORRENTE": "0"}
    await rig.client.close()


async def test_zone_mode_closes_with_the_level_mirror_left_behind(tmp_path: Path) -> None:
    rig = Rig()
    _auto(rig)
    await rig.connect()

    def mirror(n: int, _records: list[dict[str, Any]]) -> None:
        if n == 1:  # the controller mirrors the level and keeps it
            _set(rig, "ZONA.001..SETP_CORRENTE_OLD", "2")

    _after_posts(rig, mirror)
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("zone-mode", "001", "economy"))
    assert _sent(rig) == [[("ZONA.001..SETP_CORRENTE", "2")], [("ZONA.001..SETP_CORRENTE", "0")]]
    assert _events(journal)[-1] == "closed"
    await rig.client.close()


async def test_vmc_mode_closes(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("vmc-mode", "001", "ventilate"), dwell=5.0)
    assert _sent(rig) == [[("DEUM.001..ST_MODE", "8")], [("DEUM.001..ST_MODE", "1")]]
    assert _events(journal)[-1] == "closed"
    await rig.client.close()


async def test_predictive_closes(tmp_path: Path) -> None:
    rig = Rig()
    _auto(rig)
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("predictive", None, "off"))
    assert _sent(rig) == [[("REHOM...ALG_ATTIVO", "0")], [("REHOM...ALG_ATTIVO", "1")]]
    assert _events(journal)[-1] == "closed"
    await rig.client.close()


async def test_house_closes_when_the_setpoint_comes_back(tmp_path: Path) -> None:
    rig = Rig()
    _consistent_setpoint(rig)
    await rig.connect()

    def placeholder(n: int, _records: list[dict[str, Any]]) -> None:
        if n == 1:  # entering AUTO, the controller parks SET_POINT_TEMP at TEMP_OFF
            _set(rig, "REHOM...SET_POINT_TEMP", "38")

    _after_posts(rig, placeholder)
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("house", None, "auto"))
    assert _sent(rig) == [
        [("REHOM...MODO", "2"), ("REHOM...SET_POINT", "0")],
        [("REHOM...MODO", "1"), ("REHOM...SET_POINT", "3"), ("REHOM...SET_POINT_TEMP", "24")],
    ]
    assert _events(journal)[-1] == "closed"
    planned = _entries(journal)[0]
    assert planned["before"] == {
        "REHOM...MODO": "1",
        "REHOM...SET_POINT": "3",
        "REHOM...SET_POINT_TEMP": "24",
    }
    await rig.client.close()


async def test_comfort_temp_closes(tmp_path: Path) -> None:
    rig = Rig()
    _consistent_setpoint(rig)
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("comfort-temp", None, "0.1"))
    assert _sent(rig) == [
        [("REHOM...TEMP_COM", "24.1"), ("REHOM...SET_POINT_TEMP", "24.1")],
        [("REHOM...TEMP_COM", "24"), ("REHOM...SET_POINT_TEMP", "24")],
    ]
    assert _events(journal)[-1] == "closed"
    await rig.client.close()


# ---------------------------------------------------------------------------
# refusals before anything is journaled
# ---------------------------------------------------------------------------


async def test_open_entry_blocks_the_next_run(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    journal.write_text(
        json.dumps({"id": "x", "event": "planned", "summary": "s", "originals": {}}) + "\n"
    )
    with pytest.raises(WriteTestError, match="open entry"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert rig.ctrl.writes == []
    close_entry(journal, "x", "restored")
    assert open_entries(journal) == []
    await rig.client.close()


async def test_preflight_refuses_crono(tmp_path: Path) -> None:
    rig = Rig()
    rig.ctrl.set_value("REHOM", "", "", "WEBSERVER", "0")
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match="lock is crono"):
        await _run(rig, journal, execute=True, op=("zone-offset", "001", "1"))
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


@pytest.mark.parametrize("op", [("house", None, "auto"), ("comfort-temp", None, "0.1")])
async def test_setpoint_mismatch_refuses_house_and_comfort(
    tmp_path: Path, op: tuple[str, str | None, str]
) -> None:
    rig = Rig()  # the fixture: MANUAL/COMFORT, TEMP_COM 24, SET_POINT_TEMP 26
    await rig.connect()
    assert rig.client.state.plant.setpoint_mismatch is True
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match=r"pre-flight failed: .*setpoint mismatch"):
        await _run(rig, journal, execute=True, op=op)
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


async def test_nothing_to_test_is_refused_before_the_journal(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match="nothing to test"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "1"))  # already MIN
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


async def test_vmc_mode_run_crossing_a_boundary_is_refused(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    # 12:20 local with the default 600 s dwell: the run would reach past 12:28
    with pytest.raises(WriteTestError, match="boundary"):
        await _run(
            rig,
            journal,
            execute=True,
            op=("vmc-mode", "001", "ventilate"),
            dwell=None,
            ahead=timedelta(minutes=20),
            advance=1000,  # a regressed guard completes the run and fails the raises
        )
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


async def test_invalid_dwell_is_refused(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    for bad in (math.nan, math.inf, -5.0):
        with pytest.raises(ValueError, match="dwell"):
            await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"), dwell=bad)
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


async def test_preflight_boundary_guard() -> None:
    rig = Rig()
    await rig.connect()
    state = rig.client.state
    t0 = rig.clock.utcnow()  # 12:00 local
    assert any("boundary" in p for p in preflight_problems(state, t0))
    assert preflight_problems(state, t0 + timedelta(minutes=10)) == []
    assert any("boundary" in p for p in preflight_problems(state, t0 + timedelta(minutes=29)))
    await rig.client.close()


async def test_preflight_boundary_guard_covers_the_whole_run() -> None:
    rig = Rig()
    await rig.connect()
    state = rig.client.state
    t0 = rig.clock.utcnow() - timedelta(seconds=5)  # 12:00:00 local
    vmc_mode_run = 600 + 2 * (20 + 30) + 15
    assert any("boundary" in p for p in preflight_problems(state, t0 + timedelta(minutes=20), 700))
    assert preflight_problems(state, t0 + timedelta(minutes=2, seconds=30), vmc_mode_run) == []
    # from 12:02:00, a run may last until 12:28:00, two minutes before 12:30
    assert preflight_problems(state, t0 + timedelta(minutes=2), 26 * 60) == []
    assert any(
        "boundary" in p for p in preflight_problems(state, t0 + timedelta(minutes=2), 26 * 60 + 1)
    )
    assert any("boundary" in p for p in preflight_problems(state, t0 + timedelta(minutes=5), 3600))
    await rig.client.close()


async def test_preflight_refuses_recent_installer_activity() -> None:
    rig = Rig()
    await rig.connect()
    rig.connector.current.push(bus("RIS_ZONE", "1"))  # a commissioning frame
    await rig.clock.advance(1)
    state = rig.client.state
    seen = state.plant.installer_activity_at
    assert seen is not None
    assert any("installer" in p for p in preflight_problems(state, seen + timedelta(minutes=9)))
    assert preflight_problems(state, seen + timedelta(minutes=11)) == []
    await rig.client.close()


# ---------------------------------------------------------------------------
# stops after the forward write
# ---------------------------------------------------------------------------


async def test_unexpected_change_stops_without_revert(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    _after_posts(rig, lambda _n, _r: _set(rig, "REHOM...ALG_ATTIVO", "0"))  # someone else
    with pytest.raises(WriteTestError, match="unexpected change"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert len(rig.ctrl.writes) == 1  # no revert sent
    assert _events(journal) == ["planned", "forward_confirmed", "stopped"]
    assert len(open_entries(journal)) == 1
    await rig.client.close()


async def test_plant_conf_change_stops_without_revert(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    assert "temperatura_comfort_inverno" in rig.client.state.plant_conf
    journal = tmp_path / "j.jsonl"

    def conf_change(n: int, _records: list[dict[str, Any]]) -> None:
        if n == 1:
            rig.connector.current.push(bus("temperatura_comfort_inverno", "19.5"))

    _after_posts(rig, conf_change)
    with pytest.raises(WriteTestError, match=r"unexpected change.*CONF\.temperatura_comfort"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert len(rig.ctrl.writes) == 1
    assert _events(journal)[-1] == "stopped"
    await rig.client.close()


async def test_web_version_change_stops_without_revert(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"

    def upgrade(n: int, _records: list[dict[str, Any]]) -> None:
        if n == 1:
            rig.ctrl.alive = {"version": "9.9.9"}  # seen by the next /alive/ poll (30 s)

    _after_posts(rig, upgrade)
    with pytest.raises(WriteTestError, match="web version changed"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"), dwell=40, advance=200)
    assert len(rig.ctrl.writes) == 1
    assert _events(journal)[-1] == "stopped"
    await rig.client.close()


@pytest.mark.parametrize(
    ("path", "alarm_id"),
    [
        ("PROC...WATCHDOG_ALARM", "hub:watchdog"),
        ("DEUM.001..ALLARM_SONDA_RICIRCOLO", "vmc:001:ALLARM_SONDA_RICIRCOLO"),
    ],
)
async def test_new_alarm_during_dwell_stops_without_revert(
    tmp_path: Path, path: str, alarm_id: str
) -> None:
    """A new alarm is caught at once (not only after the 60 s debounce), with no revert."""
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    _after_posts(rig, lambda n, _r: _set(rig, path, "1") if n == 1 else None)
    with pytest.raises(WriteTestError, match=f"new alarm.*{alarm_id}"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert len(rig.ctrl.writes) == 1
    assert _events(journal) == ["planned", "forward_confirmed", "stopped"]
    await rig.client.close()


async def test_new_alarm_after_revert_stops(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    _after_posts(rig, lambda n, _r: _set(rig, "PROC...WATCHDOG_ALARM", "1") if n == 2 else None)
    with pytest.raises(WriteTestError, match="new alarm"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert len(rig.ctrl.writes) == 2
    assert _events(journal) == ["planned", "forward_confirmed", "reverted", "stopped"]
    assert len(open_entries(journal)) == 1
    await rig.client.close()


async def test_alarm_active_at_preflight_is_the_baseline(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    _set(rig, "PROC...WATCHDOG_ALARM", "1")  # flapping, not debounced yet
    await rig.clock.advance(1)
    assert [a.id for a in rig.client.state.alarms] == ["hub:watchdog"]
    journal = tmp_path / "j.jsonl"
    await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert _events(journal)[-1] == "closed"
    await rig.client.close()


async def test_failed_forward_stops(tmp_path: Path) -> None:
    rig = Rig()
    rig.ctrl.echo = False
    rig.ctrl.apply = False
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match="forward write failed"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"), advance=300)
    assert _events(journal) == ["planned", "stopped"]
    await rig.client.close()


async def test_revert_failure_stops_and_prints_the_restore(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    _fail_post(rig, 2, RehomConnectionError("controller unreachable"))
    journal = tmp_path / "j.jsonl"
    lines: list[str] = []
    with pytest.raises(WriteTestError, match="revert failed: RehomConnectionError"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"), lines=lines)
    assert len(rig.ctrl.writes) == 1  # the rejected revert never reached the controller
    assert _events(journal) == ["planned", "forward_confirmed", "stopped"]
    assert "revert failed" in _entries(journal)[-1]["reason"]
    restore = [line for line in lines if line.startswith("restore by hand")]
    assert restore == [
        "restore by hand: VMC 001 fan 1 -> 2 -> back to {'fan': 1} "
        "(raw {'DEUM.001..COM_VENTILA': '1'})"
    ]
    assert len(open_entries(journal)) == 1
    await rig.client.close()


@pytest.mark.parametrize(
    ("post", "match", "events"),
    [
        (1, r"unexpected change\(s\)", ["planned", "forward_confirmed", "stopped"]),
        (2, "state differs after revert", ["planned", "forward_confirmed", "reverted", "stopped"]),
    ],
)
async def test_vmc_mode_leftover_on_the_unit_stops(
    tmp_path: Path, post: int, match: str, events: list[str]
) -> None:
    """The unit's other settings are always checked (no DEUM.<id>..* wildcard)."""
    rig = Rig()
    await rig.connect()
    _after_posts(rig, lambda n, _r: _set(rig, "DEUM.001..COM_VENTILA", "3") if n == post else None)
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match=rf"{match}: DEUM\.001\.\.COM_VENTILA: '1' -> '3'"):
        await _run(rig, journal, execute=True, op=("vmc-mode", "001", "ventilate"), dwell=5.0)
    assert len(rig.ctrl.writes) == post  # no revert after a change seen in the dwell
    assert _events(journal) == events
    await rig.client.close()


async def test_house_setpoint_must_come_back_after_the_revert(tmp_path: Path) -> None:
    rig = Rig()
    _consistent_setpoint(rig)
    await rig.connect()
    _after_posts(rig, lambda n, _r: _set(rig, "REHOM...SET_POINT_TEMP", "26") if n == 2 else None)
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match=r"after revert: REHOM\.\.\.SET_POINT_TEMP"):
        await _run(rig, journal, execute=True, op=("house", None, "auto"))
    assert _events(journal)[-1] == "stopped"
    await rig.client.close()


async def test_forward_that_sends_nothing_stops(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()

    async def no_op(_client: Any) -> bool:
        return False

    def builder(st: Any) -> Operation:
        return dataclasses.replace(build_operation(st, "vmc-fan", "001", "2"), forward=no_op)

    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match="sent nothing"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"), builder=builder)
    assert rig.ctrl.writes == []
    assert _events(journal) == ["planned", "stopped"]
    await rig.client.close()


async def test_cancel_during_dwell_prints_the_restore_then_journals(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    lines: list[tuple[str, int]] = []

    def echo(line: str) -> None:  # what the journal held when each line was printed
        lines.append((line, _events(journal).count("stopped") if journal.exists() else 0))

    task = asyncio.ensure_future(
        run_write_test(
            rig.client,
            lambda st: build_operation(st, "vmc-fan", "001", "2"),
            journal=journal,
            execute=True,
            echo=echo,
            sleep=rig.clock.sleep,
            now=lambda: rig.clock.utcnow() + AHEAD,
            dwell_s=100.0,
        )
    )
    for _ in range(40):
        await rig.clock.advance(0.5)
    assert ("dwell 100 s", 0) in lines
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ("STOPPED: interrupted", 0) in lines
    assert any(line.startswith("restore by hand") and n == 0 for line, n in lines)
    assert _events(journal) == ["planned", "forward_confirmed", "stopped"]
    assert _entries(journal)[-1]["reason"] == "interrupted"
    assert len(rig.ctrl.writes) == 1
    await rig.client.close()


async def test_a_failing_journal_does_not_mask_the_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    lines: list[str] = []
    real_append = writetest._append

    def append(path: Path, entry: dict[str, Any]) -> None:
        if entry["event"] == "stopped":
            raise OSError(28, "No space left on device")
        real_append(path, entry)

    monkeypatch.setattr(writetest, "_append", append)

    async def boom(_seconds: float) -> None:
        raise RuntimeError("unexpected")

    coro = run_write_test(
        rig.client,
        lambda st: build_operation(st, "vmc-fan", "001", "2"),
        journal=journal,
        execute=True,
        echo=lines.append,
        sleep=boom,
        now=lambda: rig.clock.utcnow() + AHEAD,
        dwell_s=1.0,
    )
    with pytest.raises(RuntimeError, match="unexpected"):
        await rig.run(coro)
    assert "STOPPED: RuntimeError" in lines
    assert any("could not journal the stop (OSError)" in line for line in lines)
    assert _events(journal) == ["planned", "forward_confirmed"]  # still open
    await rig.client.close()


# ---------------------------------------------------------------------------
# operations and arguments
# ---------------------------------------------------------------------------


async def test_build_operation_refusals() -> None:
    rig = Rig()
    await rig.connect()
    state = rig.client.state
    with pytest.raises(ValueError, match="unknown operation"):
        build_operation(state, "season", None, "winter")
    with pytest.raises(ValueError, match="rapid"):
        build_operation(state, "vmc-mode", "001", "rapid_renewal")
    with pytest.raises(ValueError, match="--target"):
        build_operation(state, "zone-offset", None, "1")
    op = build_operation(state, "house", None, "auto")
    assert op.summary == "house comfort -> auto"
    assert op.revert_plan(state).records[-1]["Key"] == "SET_POINT_TEMP"
    await rig.client.close()


@pytest.mark.parametrize(
    ("op", "match"),
    [
        (("predictive", None, "yes"), "on or off"),
        (("zone-offset", "1", "1"), "3-digit"),
        (("zone-offset", "099", "1"), "zone 099 is not present"),
        (("zone-offset", "001", "x"), "whole number"),
        (("zone-offset", "001", "1_0"), "whole number"),
        (("zone-mode", "001", "off"), "zone mode"),
        (("vmc-fan", "009", "2"), "VMC 009 is not present"),
        (("vmc-fan", "001", "fast"), "whole number"),
        (("house", None, "off"), "never switches the house off"),
        (("house", None, "cosy"), "house preset"),
        (("comfort-temp", None, "nan"), "delta"),
        (("comfort-temp", None, "inf"), "delta"),
    ],
)
async def test_unknown_targets_and_bad_values_are_value_errors(
    op: tuple[str, str | None, str], match: str
) -> None:
    rig = Rig()
    await rig.connect()
    with pytest.raises(ValueError, match=match):
        build_operation(rig.client.state, *op)
    await rig.client.close()


@pytest.mark.parametrize("raw", ["1.5", ""])
async def test_zone_offset_original_must_be_a_known_whole_number(raw: str) -> None:
    rig = Rig()
    rig.ctrl.set_value("ZONA", "001", "", "DELTA_SETP_CORRENTE", raw)
    await rig.connect()
    with pytest.raises(ValueError, match="not a whole number"):
        build_operation(rig.client.state, "zone-offset", "001", "1")
    await rig.client.close()


@pytest.mark.parametrize("value", ["cool", "dehumidify_cool", "stop", "rapid_renewal", "heat"])
async def test_vmc_mode_targets_only_ventilate_or_standby(value: str) -> None:
    rig = Rig()
    await rig.connect()
    with pytest.raises(ValueError, match="only to"):
        build_operation(rig.client.state, "vmc-mode", "001", value)
    await rig.client.close()


@pytest.mark.parametrize(
    ("raw", "match"), [("6", "RAPID_RENEWAL"), ("0", "STOP"), ("8", "already")]
)
async def test_vmc_mode_refuses_originals_it_must_not_restore(raw: str, match: str) -> None:
    rig = Rig()
    rig.ctrl.set_value("DEUM", "001", "", "ST_MODE", raw)
    await rig.connect()
    with pytest.raises(ValueError, match=match):
        build_operation(rig.client.state, "vmc-mode", "001", "ventilate")
    await rig.client.close()


def test_check_arguments_needs_no_state() -> None:
    check_arguments("zone-offset", "001", "-1")
    check_arguments("predictive", None, "on")
    check_arguments("comfort-temp", None, "-0.1")
    for args, match in [
        (("season", None, "winter"), "unknown operation"),
        (("vmc-fan", None, "2"), "--target VMC"),
        (("vmc-fan", "01", "2"), "3-digit"),
        (("vmc-mode", "001", "cool"), "only to"),
        (("predictive", None, "yes"), "on or off"),
    ]:
        with pytest.raises(ValueError, match=match):
            check_arguments(*args)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_unexpected_changes_ignores_volatile_and_allowed() -> None:
    before = {
        "ZONA.001..TEMP_AMBIENTE": "24.0",
        "DEUM.001..ST_MODE": "1",
        "REHOM...MODO": "1",
        "PROC...TEMP_RASPBERRY": "60",
    }
    after = dict(before)
    after.update({"ZONA.001..TEMP_AMBIENTE": "24.3", "PROC...TEMP_RASPBERRY": "61"})
    assert unexpected_changes(before, after, []) == []
    after["DEUM.001..ST_MODE"] = "8"  # a VMC mode change is never "volatile"
    assert unexpected_changes(before, after, []) == ["DEUM.001..ST_MODE"]
    assert unexpected_changes(before, after, ["DEUM.001..*"]) == []
    after["NEW.001..X"] = "1"
    assert unexpected_changes(before, after, ["DEUM.001..*"]) == ["NEW.001..X"]


def test_unexpected_changes_compares_semantically() -> None:
    offset = "ZONA.001..DELTA_SETP_CORRENTE"
    assert unexpected_changes({offset: "0"}, {offset: "0.0"}, []) == []
    assert unexpected_changes({offset: "-1"}, {offset: "-1.0"}, []) == []
    assert unexpected_changes({offset: "0"}, {offset: "1"}, []) == [offset]
    assert unexpected_changes({offset: "0"}, {}, []) == [offset]  # vanished
    assert unexpected_changes({}, {offset: "0"}, []) == [offset]  # appeared
    preset = "PROG.001.1.PROG_SETT_ESTATE"  # identifiers compare exactly
    assert unexpected_changes({preset: "01"}, {preset: "1"}, []) == [preset]


def test_plant_conf_paths_are_never_volatile() -> None:
    key = "CONF.ATTIVA"  # would match the "*.ATTIVA" record pattern
    assert unexpected_changes({key: "0"}, {key: "1"}, []) == [key]
    assert unexpected_changes({key: "0"}, {key: "0.0"}, []) == []
    assert unexpected_changes({key: "0"}, {key: "1"}, ["CONF.ATTIVA"]) == []


# ---------------------------------------------------------------------------
# journal
# ---------------------------------------------------------------------------


def test_corrupt_journal_line_is_named_and_blocks_everything(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    good = json.dumps({"id": "a", "event": "planned", "summary": "s", "originals": {}})
    journal.write_text(good + "\n" + '{"id": "a", "ev')  # a torn append
    with pytest.raises(WriteTestError, match="corrupt at line 2"):
        open_entries(journal)
    with pytest.raises(WriteTestError, match="corrupt at line 2"):
        close_entry(journal, "a", "restored")
    for bad in ("[1, 2]", '{"event": "planned"}', b"\xff\xfe"):
        journal.write_bytes(bad if isinstance(bad, bytes) else bad.encode())
        with pytest.raises(WriteTestError, match="corrupt at line 1"):
            open_entries(journal)


async def test_corrupt_journal_refuses_a_run(tmp_path: Path) -> None:
    rig = Rig()
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    journal.write_text("not json\n")
    with pytest.raises(WriteTestError, match="corrupt at line 1"):
        await _run(rig, journal, execute=True, op=("vmc-fan", "001", "2"))
    assert rig.ctrl.writes == []
    await rig.client.close()


def test_close_entry_refuses_an_id_that_is_not_open(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match="not an open entry"):
        close_entry(journal, "typo", "restored")
    journal.write_text(
        json.dumps({"id": "a", "event": "planned"})
        + "\n"
        + json.dumps({"id": "a", "event": "closed"})
        + "\n"
    )
    with pytest.raises(WriteTestError, match="not an open entry"):
        close_entry(journal, "a", "already closed")
    assert _events(journal) == ["planned", "closed"]


class _FakeFcntl:
    F_FULLFSYNC = 51

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[int] = []
        self.fail = fail

    def fcntl(self, fd: int, cmd: int) -> int:
        assert cmd == self.F_FULLFSYNC
        self.calls.append(fd)
        if self.fail:
            raise OSError(45, "Operation not supported")
        return 0


def test_append_forces_the_line_and_a_new_directory_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeFcntl()
    fsyncs: list[int] = []
    monkeypatch.setattr(writetest, "fcntl", fake)
    monkeypatch.setattr(writetest.os, "fsync", fsyncs.append)
    journal = tmp_path / "new" / "j.jsonl"
    writetest._append(journal, {"id": "a", "event": "planned"})
    assert len(fake.calls) == 2  # the file, then its (new) directory
    assert oct((tmp_path / "new").stat().st_mode & 0o777) == "0o700"
    writetest._append(journal, {"id": "a", "event": "closed"})
    assert len(fake.calls) == 3  # the file only
    assert fsyncs == []
    assert _events(journal) == ["planned", "closed"]


def test_append_falls_back_to_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fsyncs: list[int] = []
    monkeypatch.setattr(writetest, "fcntl", _FakeFcntl(fail=True))
    monkeypatch.setattr(writetest.os, "fsync", fsyncs.append)
    writetest._append(tmp_path / "j.jsonl", {"id": "a", "event": "planned"})
    assert len(fsyncs) == 2


@pytest.mark.parametrize(
    ("row", "op", "match"),
    [
        (("ZONA", "001", "SETP_CORRENTE", "1"), ("zone-mode", "001", "economy"), "cannot be"),
        (("DEUM", "001", "COM_VENTILA", ""), ("vmc-fan", "001", "2"), "fan value is unknown"),
        (("DEUM", "001", "ST_MODE", ""), ("vmc-mode", "001", "ventilate"), "mode is unknown"),
        (("REHOM", "", "ALG_ATTIVO", ""), ("predictive", None, "off"), "state is unknown"),
        (("REHOM", "", "MODO", "0"), ("house", None, "auto"), "cannot be restored"),
        (("REHOM", "", "TEMP_COM", ""), ("comfort-temp", None, "0.1"), "temperature is unknown"),
    ],
)
async def test_unrestorable_originals_are_refused(
    row: tuple[str, str, str, str], op: tuple[str, str | None, str], match: str
) -> None:
    gruppo, unita, key, value = row
    rig = Rig()
    rig.ctrl.set_value(gruppo, unita, "", key, value)
    await rig.connect()
    with pytest.raises(ValueError, match=match):
        build_operation(rig.client.state, *op)
    await rig.client.close()


async def test_vmc_mode_refusals_that_need_the_state() -> None:
    rig = Rig()
    await rig.connect()
    with pytest.raises(ValueError, match="must be one of"):
        build_operation(rig.client.state, "vmc-mode", "001", "warp")
    with pytest.raises(ValueError, match="runs a schedule"):
        build_operation(rig.client.state, "vmc-mode", "002", "ventilate")  # CICLO_ATTIVO=1
    await rig.client.close()


async def test_planner_refusal_is_reported_before_the_journal(tmp_path: Path) -> None:
    rig = Rig()  # house MANUAL: zone modes need AUTO
    await rig.connect()
    journal = tmp_path / "j.jsonl"
    with pytest.raises(WriteTestError, match=r"refused \(house_not_auto\)"):
        await _run(rig, journal, execute=True, op=("zone-mode", "001", "economy"))
    assert rig.ctrl.writes == [] and not journal.exists()
    await rig.client.close()


async def test_preflight_plant_flags_and_debounced_alarms() -> None:
    rig = Rig()
    rig.ctrl.set_value("REHOM", "", "", "TERMO_READONLY", "1")
    rig.ctrl.set_value("PROC", "", "", "WATCHDOG_ALARM", "1")
    rig.ctrl.plant_conf["CONFIGURA_ON"] = "1"
    await rig.connect()
    await rig.clock.advance(61)  # the alarm is now debounced
    state = rig.client.state
    problems = preflight_problems(state, rig.clock.utcnow() + AHEAD)
    assert "controller read-only flag is set" in problems
    assert "an installer session is active" in problems
    assert "1 active alarm(s)" in problems
    await rig.client.close()


def test_secret_values_are_never_shown() -> None:
    assert writetest._show("UTEN...someone", "pw") == "<redacted>"
    assert writetest._show("CONF.remote_token", "t") == "<redacted>"
    assert writetest._show("CONF.stagione", "1") == "'1'"
    assert writetest._show("UTEN...someone", None) == "None"


def test_blank_journal_lines_are_skipped(tmp_path: Path) -> None:
    journal = tmp_path / "j.jsonl"
    journal.write_text("\n" + json.dumps({"id": "a", "event": "planned"}) + "\n\n")
    assert [e["id"] for e in open_entries(journal)] == ["a"]


def test_append_uses_fsync_without_full_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsyncs: list[int] = []
    monkeypatch.setattr(writetest, "fcntl", object())  # Linux: no F_FULLFSYNC
    monkeypatch.setattr(writetest.os, "fsync", fsyncs.append)
    writetest._append(tmp_path / "j.jsonl", {"id": "a", "event": "planned"})
    assert len(fsyncs) == 2  # the file and its directory
