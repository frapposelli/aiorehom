"""``aiorehom.replay``: the fake device, its transport and connection, driver and clocks."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aiorehom.clock import VirtualClock
from aiorehom.exceptions import RehomConnectionError
from aiorehom.replay import (
    DEFAULT_LATENCY,
    ReplayConnection,
    ReplayData,
    ReplayDevice,
    ReplayDriver,
    ReplayError,
    ScaledClock,
)

from .conftest import rec

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"
T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)


def iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def termo(path: str, value: Any = None, *, kind: str = "update", **extra: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {"domain": "termo", "type": kind, "path": path}
    frame["gruppo"] = extra.pop("gruppo", path.split(".", 1)[0])
    if kind == "update":
        frame["value"] = value
    frame.update(extra)
    return frame


def write_capture(
    directory: Path,
    lines: list[dict[str, Any]],
    *,
    interface: Any = None,
    overrides: Any = None,
    plant_conf: Any = None,
    config: Any = None,
    alive: Any = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payloads = {
        "interface.json": interface
        if interface is not None
        else [
            rec("REHOM", "", "", "A", "1"),
            rec("REHOM", "", "", "B", "2"),
            rec("UTEN", "", "", "alice", "plain-password"),
        ],
        "overrides.json": overrides if overrides is not None else [],
        "plant_conf.json": plant_conf if plant_conf is not None else {"k": "1"},
        "config.json": config if config is not None else {"TIMEZONE": "Europe/Rome"},
    }
    if alive is not None:
        payloads["alive.json"] = alive
    for name, payload in payloads.items():
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    (directory / "ws.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return directory


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def line(seconds: float, frame: dict[str, Any] | None = None, **event: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"t": iso(at(seconds)), "mono": seconds}
    if frame is not None:
        out["frame"] = frame
    else:
        out["event"] = event
    return out


STANDARD_LINES = [
    line(0, event="connect", url_path="/ws/"),
    line(1, termo("REHOM...A", "3")),
    line(2, termo("REHOM...B", kind="remove")),
    line(3, termo("REHOM...C", "new")),
    line(4, termo("METEO_DATA...dt1", "{}")),
    line(5, {"domain": "bus", "type": "update", "key": "k", "value": 2}),
    line(5, {"domain": "bus", "type": "update", "key": "RIS_SCAN", "value": "x"}),
    line(5, {"domain": "bus", "type": "update", "key": "", "value": "x"}),
    line(5, {"domotica": {"x": 1}}),
    line(5, termo("bad-path", "1")),
    line(6, termo("PROG.001.1.PROG_GIORNO_ESTATE", "3", gruppo="PROG_OVERRIDE")),
    line(7, {"domain": "bus", "type": "remove", "key": "k"}),
    line(8, {"domain": "bus", "type": "update", "key": "n", "value": None}),
    line(8, {"domain": "bus", "type": "other", "key": "n"}),
    line(9, event="disconnect", reason="max_duration"),
]


def values(data: ReplayData) -> dict[str, str]:
    return {row["path"]: row["Valore"] for row in data.interface}


def test_load_detects_start_end_and_redacts(tmp_path: Path) -> None:
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", STANDARD_LINES))
    assert device.start == at(0)
    assert device.end == at(9)
    assert len(device.frames) == 13
    assert device.data.alive == {"version": "unknown"}
    uten = next(r for r in device.data.interface if r["Gruppo"] == "UTEN")
    assert uten["Valore"] == "<redacted len=14>"  # re-redacted on load


def test_state_at_uses_record_store_semantics(tmp_path: Path) -> None:
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", STANDARD_LINES))
    assert values(device.state_at(at(0)))["REHOM...A"] == "1"
    mid = device.state_at(at(2))
    assert values(mid)["REHOM...A"] == "3"
    assert "REHOM...B" not in values(mid)  # removes are immediate on the device side
    end = device.state_at(at(9))
    assert [row["path"] for row in end.interface] == ["REHOM...A", "UTEN...alice", "REHOM...C"]
    assert not any(row["Gruppo"] == "METEO_DATA" for row in end.interface)
    assert end.plant_conf == {"n": ""}  # k updated at 5 s then removed at 7 s; RIS_* ignored
    assert device.state_at(at(5)).plant_conf == {"k": "2"}
    assert [(r["Gruppo"], r["Valore"]) for r in end.overrides] == [("PROG_OVERRIDE", "3")]
    assert device.data.interface[0]["Valore"] == "1"  # the snapshot itself is untouched


def test_no_connect_event_starts_one_second_before_the_first_frame(tmp_path: Path) -> None:
    lines = [line(5, termo("REHOM...A", "3")), line(7, event="other")]
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", lines, alive={"version": "9"}))
    assert (device.start, device.end) == (at(4), at(7))
    assert device.data.alive == {"version": "9"}


def test_patch_edits_the_snapshot(tmp_path: Path) -> None:
    def patch(data: ReplayData) -> None:
        data.interface[0]["Valore"] = "patched"

    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", STANDARD_LINES), patch=patch)
    assert values(device.state_at(at(0)))["REHOM...A"] == "patched"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: (d / "config.json").unlink(), "missing file"),
        (lambda d: (d / "interface.json").write_text("{nope"), "invalid JSON"),
        (lambda d: (d / "interface.json").write_text("{}"), "expected a JSON list"),
        (lambda d: (d / "alive.json").write_text("[]"), "alive.json: expected a JSON dict"),
        (lambda d: (d / "ws.jsonl").write_text("[1]\n"), "not an object"),
        (lambda d: (d / "ws.jsonl").write_text('{"t": "yesterday"}\n'), "invalid timestamp"),
        (lambda d: (d / "ws.jsonl").write_text('{"frame": {}}\n'), "missing timestamp"),
        (lambda d: (d / "ws.jsonl").write_text(""), "empty"),
        (
            lambda d: (d / "ws.jsonl").write_text('{"t": "2026-09-25T10:00:00Z", "event": {}}\n'),
            "no connect event and no frame",
        ),
    ],
)
def test_invalid_captures(tmp_path: Path, mutate: Any, message: str) -> None:
    directory = write_capture(tmp_path / "c", STANDARD_LINES, alive={"version": "1"})
    mutate(directory)
    with pytest.raises(ReplayError, match=message):
        ReplayDevice.from_dir(directory)


def test_not_a_directory(tmp_path: Path) -> None:
    with pytest.raises(ReplayError, match="not a directory"):
        ReplayDevice.from_dir(tmp_path / "missing")


def test_end_before_start_is_rejected() -> None:
    data = ReplayData(interface=[], overrides=[], plant_conf={}, config={}, alive={})
    with pytest.raises(ReplayError, match="ends before"):
        ReplayDevice(data, [], start=at(5), end=at(1))


def test_naive_timestamps_are_utc(tmp_path: Path) -> None:
    lines = [{"t": "2026-09-25T10:00:00", "event": {"event": "connect"}}]
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", lines))
    assert device.start == T0


async def test_transport_answers_from_the_call_start(tmp_path: Path) -> None:
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", STANDARD_LINES))
    clock = VirtualClock(at(0))
    transport = device.transport(clock, latency={"get_config": 0.5})
    assert transport.has_token is False
    task = asyncio.create_task(transport.get_interface())
    await clock.advance(3.0)  # frames at 1 s and 2 s arrive during the 2.4 s latency
    interface = await task
    assert {row["path"]: row["Valore"] for row in interface}["REHOM...A"] == "1"
    task_config = asyncio.create_task(transport.get_config())
    await clock.advance(0.5)
    config = await task_config
    assert config["LOCAL_TIME"] == "2026-09-25T12:00:03"  # Europe/Rome at return time
    for name in ("get_alive", "get_overrides", "get_plant_conf"):
        call = asyncio.create_task(getattr(transport, name)())
        await clock.advance(DEFAULT_LATENCY[name])
        await call
    history = asyncio.create_task(transport.get_history("K", "", "a", "b"))
    login = asyncio.create_task(transport.login("u", "p"))
    await clock.advance(1.0)
    assert await history == []
    await login
    await transport.close()
    assert device.transport_calls == [
        "get_interface",
        "get_config",
        "get_alive",
        "get_overrides",
        "get_plant_conf",
        "get_history",
        "login",
        "close",
    ]


def test_local_time_falls_back_to_utc(tmp_path: Path) -> None:
    for config in ({"TIMEZONE": "Not/AZone"}, {}):
        device = ReplayDevice.from_dir(
            write_capture(tmp_path / str(len(config)), STANDARD_LINES, config=config)
        )
        assert device.local_time(at(1)) == "2026-09-25T10:00:01"


async def test_connections_are_fed_by_the_driver(tmp_path: Path) -> None:
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", STANDARD_LINES))
    clock = VirtualClock(at(0))
    driver = ReplayDriver(device, clock)
    await driver.run_until(at(1))
    assert device.frames_dropped == 1  # nobody connected yet
    connect = device.ws_connector(clock)
    first = await connect()
    second = await connect()  # a new connection ends the previous one
    assert await first.receive() is None
    await driver.run_until(at(3))
    assert driver.delivered == 3
    assert (await second.receive())["path"] == "REHOM...B"
    assert (await second.receive())["path"] == "REHOM...C"
    device.disconnect()
    assert await second.receive() is None
    await driver.run_to_end()
    assert device.frames_dropped == 1 + 10
    assert device.connections == 2
    await clock.advance(1.0)
    with pytest.raises(RehomConnectionError):
        await connect()


async def test_replay_connection_semantics() -> None:
    ReplayDevice(
        ReplayData(interface=[], overrides=[], plant_conf={}, config={}, alive={}),
        [],
        start=T0,
        end=T0,
    ).disconnect()  # no connection: nothing to end
    conn = ReplayConnection()
    conn.end()
    conn.end()
    assert await conn.receive() is None
    conn = ReplayConnection()
    conn.push({"a": 1})
    await conn.close()
    conn.push({"b": 2})  # ignored once closed
    assert conn.closed is True
    assert await conn.receive() == {"a": 1}
    assert await conn.receive() is None
    assert await conn.receive() is None


async def test_driver_with_a_scaled_clock(tmp_path: Path) -> None:
    lines = [
        line(0, event="connect"),
        line(0.5, termo("REHOM...A", "3")),
        line(1.0, termo("REHOM...A", "4")),
    ]
    device = ReplayDevice.from_dir(write_capture(tmp_path / "c", lines))
    clock = ScaledClock(device.start, 1000.0)
    connection = await device.ws_connector(clock)()
    driver = ReplayDriver(device, clock)
    await driver.run_to_end()
    assert clock.utcnow() >= device.end
    assert (await connection.receive())["value"] == "3"
    assert (await connection.receive())["value"] == "4"


async def test_scaled_clock() -> None:
    clock = ScaledClock(T0, 500.0)
    assert clock.speed == 500.0
    mono, now = clock.monotonic(), clock.utcnow()
    await clock.sleep(1.0)  # 2 ms real
    assert clock.monotonic() - mono >= 0.9
    assert clock.utcnow() - now >= timedelta(seconds=0.9)
    await clock.sleep(-1)
    with pytest.raises(ValueError, match="speed"):
        ScaledClock(T0, 0)
    with pytest.raises(ValueError, match="speed"):
        ScaledClock(T0, 1001)
    with pytest.raises(ValueError, match="aware"):
        ScaledClock(datetime(2026, 1, 1), 1.0)


def test_fixture_snapshot_has_no_forecast_rows_and_rome_local_time() -> None:
    device = ReplayDevice.from_dir(FIXTURE)
    end = device.state_at(device.end)
    assert not any(row["Gruppo"] == "METEO_DATA" for row in end.interface)
    assert device.local_time(datetime(2026, 9, 25, 10, 22, 9, 900000, tzinfo=UTC)) == (
        "2026-09-25T12:22:09"
    )
    # the METEO rows removed and re-added at 10:34:03 are back in the snapshot
    paths = {row["path"] for row in end.interface}
    assert "METEO...DATA" in paths
    assert values(end)["METEO...DATA"] == "2026-09-25 12:34:02"
