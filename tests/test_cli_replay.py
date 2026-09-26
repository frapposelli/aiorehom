"""``rehom-probe replay``, offline under the socket guard."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from aiorehom import cli
from aiorehom.exceptions import RehomConnectionError
from aiorehom.replay import ReplayTransport

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"


def T(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-09-25T{text}+00:00")


def run_json(capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[list[dict[str, Any]], str]:
    assert cli.main(["replay", str(FIXTURE), "--json", *extra]) == 0
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines()], out


def changes_of(lines: list[dict[str, Any]], path: str) -> list[tuple[datetime, Any, Any]]:
    return [
        (datetime.fromisoformat(line["t"]), change["old"], change["new"])
        for line in lines
        if "t" in line
        for change in line["changes"]
        if change["path"] == path
    ]


def test_replay_json(capsys: pytest.CaptureFixture[str]) -> None:
    lines, out = run_json(capsys)
    summary = lines[-1]["summary"]
    assert summary["frames_received"] == 563
    assert summary["noop_updates"] == 169
    assert summary["changed_updates"] == 325
    assert summary["removes_coalesced"] == 29
    assert summary["removes_applied"] == 0
    assert summary["forecast_frames"] == 40
    assert summary["frames_ignored"] == 0
    assert (summary["syncs"], summary["resyncs"], summary["fallbacks"]) == (1, 2, 0)
    assert summary["updates"]["resync"] == 0
    assert summary["updates"]["sync"] == 1
    assert (summary["alarms_raised"], summary["alarms_debounced"]) == (1, 0)
    assert summary["frames_dropped"] == 0
    assert summary["virtual_seconds"] == pytest.approx(1791.362)
    updates = lines[:-1]
    assert updates[0]["reason"] == "sync"
    # TEMP_COM and SET_POINT_TEMP both became 24.1 within 3 ms at 10:41:06.43:
    # the mismatch clears with that batch (100 ms later), not at the 24.0 echo.
    mismatch = changes_of(updates, "plant.setpoint_mismatch")
    assert [(old, new) for _t, old, new in mismatch] == [("<absent>", True), (True, False)]
    assert T("10:41:06.431") < mismatch[1][0] <= T("10:41:11")
    assert mismatch[1][0] == T("10:41:06.528")
    calling = [(t, new) for t, _old, new in changes_of(updates, "zones.001.calling")]
    assert calling[-1][1] is True
    assert T("10:41:33.668") <= calling[-1][0] <= T("10:41:34")
    for text in ("VMC 001", "Zona 0", "len="):
        assert text not in out
    zone_name = changes_of(updates, "zones.001.name")
    assert zone_name == [(T("10:22:09.906"), "<absent>", "<name>")]
    assert all(record == sorted(record) for record in (u["records"] for u in updates))
    frames = [u for u in updates if u["reason"] == "frames"]
    assert any("ZONA.003..UMIDITA" in u["records"] for u in frames)


def test_replay_json_show_names_and_ignore(capsys: pytest.CaptureFixture[str]) -> None:
    lines, out = run_json(capsys, "--show-names", "--ignore", "zones.*", "--ignore", "forecast*")
    assert "VMC 001" in out
    updates = lines[:-1]
    assert not changes_of(updates, "zones.001.name")
    assert changes_of(updates, "vmcs.001.name")[0][2] == "VMC 001"
    assert not any(c["path"].startswith("forecast") for u in updates for c in u["changes"])


def test_replay_text(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--host", "192.0.2.1", "replay", str(FIXTURE), "--instant"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0] == (
        "2026-09-25T10:22:09.906Z sync     zones=001,002,003,009,010,011 vmcs=001,002 "
        "fancoils=- preset=comfort season=summer setpoint_mismatch=True alarms=0"
    )
    assert (
        "2026-09-25T10:24:34.706Z frames   plant.demand: False -> True; zones.002.calling: "
        "False -> True; zones.002.hvac_action: idle -> cooling"
    ) in lines
    assert any("... (+" in line for line in lines)  # the forecast burst is truncated
    assert lines[-1].startswith(
        "replayed 563 frames (169 no-op, 29 coalesced removes, 40 forecast) over 29m51s "
        "virtual; updates: sync 1, frames "
    )
    assert "resync 0, clock " in lines[-1]
    assert lines[-1].endswith("; alarms raised 1 (debounced 0)")
    for text in ("VMC 001", "Zona 0", "len=", "<absent> -> ;"):
        assert text not in out


def test_replay_speed_and_until(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["replay", str(FIXTURE), "--speed", "1000", "--until", "10:22:12"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert " sync     zones=001,002,003,009,010,011 " in out[0]
    # frames up to 10:22:12: 08.026, 08.138, 11.181, 11.185 (real time: batches may be cut)
    assert out[-1].startswith("replayed 4 frames (3 no-op, 0 coalesced removes, 0 forecast)")
    assert "over 0m05s virtual; updates: sync 1, " in out[-1]


def test_replay_until_before_the_first_sync(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["replay", str(FIXTURE), "--until", "10:22:08"]) == 1
    assert "before the first sync completed" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--until", "11:00:00"], "after the end of the capture"),
        (["--alarm-debounce", "10"], "alarm_debounce"),
    ],
)
def test_replay_bad_options(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    assert cli.main(["replay", str(FIXTURE), *argv]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv", [["--until", "later"], ["--until", "10:00:00+01:00"], ["--speed", "0"]]
)
def test_replay_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["replay", str(FIXTURE), *argv])
    assert exc.value.code == 2


def test_replay_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "fx"
    shutil.copytree(FIXTURE, target)
    (target / "ws.jsonl").unlink()
    assert cli.main(["replay", str(target)]) == 2
    assert "missing file(s)" in capsys.readouterr().err
    assert cli.main(["replay", str(tmp_path / "nope")]) == 2


def test_replay_connect_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refuse(self: ReplayTransport, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        raise RehomConnectionError("replay test: interface unavailable")

    monkeypatch.setattr(ReplayTransport, "get_interface", refuse)
    assert cli.main(["replay", str(FIXTURE)]) == 1
    assert "replay failed: RehomConnectionError" in capsys.readouterr().err


def test_duration_format() -> None:
    assert cli._duration(1791.4) == "29m51s"
    assert cli._duration(3700) == "1h01m40s"
    assert cli._duration(-1) == "0m00s"
