"""``rehom-probe write-test`` wiring: client construction, waits, journal location, exit codes.

``RehomClient`` is replaced by a recorder that drives a :class:`Rig` (the
fixture-backed fake controller on a virtual clock); the Keychain read and the
command's clock and sleep are replaced too.  Nothing leaves the process.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from aiorehom import cli
from aiorehom.exceptions import (
    CredentialsError,
    ForbiddenRequestError,
    RehomConnectionError,
)

from .test_client_writes import Rig


class Recorder:
    """Stands in for ``RehomClient``: records its arguments, delegates to a :class:`Rig`."""

    instances: ClassVar[list[Recorder]] = []
    connect_error: ClassVar[BaseException | None] = None

    def __init__(
        self,
        host: str,
        *,
        port: int,
        username: str,
        password: str,
        allow_writes: bool,
    ) -> None:
        self.kwargs = {"host": host, "port": port, "username": username}
        self.allow_writes = allow_writes
        self.rig = Rig(allow_writes=allow_writes)
        self.closed = False
        Recorder.instances.append(self)

    async def connect(self) -> None:
        if Recorder.connect_error is not None:
            raise Recorder.connect_error
        await self.rig.connect()

    async def close(self) -> None:
        self.closed = True
        await self.rig.client.close()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.rig.client, name)
        if not name.startswith("set_"):
            return attr

        async def driven(*args: Any) -> Any:  # the virtual clock runs while the write waits
            return await self.rig.run(attr(*args), advance=120)

        return driven


class Harness:
    def __init__(self) -> None:
        self.sleeps: list[float] = []
        self.keychain_reads = 0

    @property
    def client(self) -> Recorder:
        (client,) = Recorder.instances
        return client


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    Recorder.instances = []
    Recorder.connect_error = None
    h = Harness()

    async def credentials(_service: str, *, username: str | None = None) -> tuple[str, str]:
        h.keychain_reads += 1
        return username or "alice", "not-a-real-password"

    async def sleep(seconds: float) -> None:
        h.sleeps.append(seconds)
        await h.client.rig.clock.advance(seconds)

    def now() -> datetime:  # 12:10 local on the rig's clock
        return h.client.rig.clock.utcnow() + timedelta(minutes=10)

    monkeypatch.setattr(cli, "RehomClient", Recorder)
    monkeypatch.setattr(cli, "read_keychain_credentials", credentials)
    monkeypatch.setattr(cli, "_wall_sleep", sleep)
    monkeypatch.setattr(cli, "_wall_now", now)
    yield h
    Recorder.instances = []
    Recorder.connect_error = None


def _events(journal: Path) -> list[str]:
    return [json.loads(line)["event"] for line in journal.read_text().splitlines()]


def _write_test(journal: Path, *args: str) -> list[str]:
    return ["write-test", "--journal", str(journal), *args]


FAN_UP = ("--op", "vmc-fan", "--target", "001", "--value", "2")


def test_dry_run_is_read_only(harness: Harness, tmp_path: Path, capsys: Any) -> None:
    journal = tmp_path / "j.jsonl"
    assert cli.main(_write_test(journal, *FAN_UP)) == 0
    client = harness.client
    assert client.allow_writes is False
    assert client.rig.client.allow_writes is False
    assert harness.sleeps == []  # no alarm-debounce wait, no dwell
    assert client.rig.ctrl.writes == []
    assert client.closed
    assert not journal.exists()
    out = capsys.readouterr().out
    assert f"journal: {journal.resolve()}" in out
    assert 'revert   -> /api/interface/bulk_update/: [{"Gruppo": "DEUM"' in out
    assert "dry run: nothing sent" in out


def test_execute_waits_for_the_debounce_then_runs(
    harness: Harness, tmp_path: Path, capsys: Any
) -> None:
    journal = tmp_path / "j.jsonl"
    assert cli.main(_write_test(journal, *FAN_UP, "--execute", "--dwell", "5")) == 0
    client = harness.client
    assert client.allow_writes is True
    wait = client.rig.client.options.alarm_debounce + 5
    assert harness.sleeps == [wait, 5.0, 15.0]  # debounce, dwell, post-revert settle
    values = [w[1][0]["Valore"] for w in client.rig.ctrl.writes]
    assert values == ["2", "1"]
    assert _events(journal) == ["planned", "forward_confirmed", "reverted", "closed"]
    out = capsys.readouterr().out
    assert f"waiting {wait:g} s" in out
    assert "reverted and verified" in out


def test_execute_refused_exits_4(harness: Harness, tmp_path: Path, capsys: Any) -> None:
    journal = tmp_path / "j.jsonl"
    journal.write_text(json.dumps({"id": "x", "event": "planned", "summary": "s"}) + "\n")
    assert cli.main(_write_test(journal, *FAN_UP, "--execute")) == 4
    assert harness.client.allow_writes is True
    assert harness.sleeps == [harness.client.rig.client.options.alarm_debounce + 5]
    assert harness.client.rig.ctrl.writes == []
    assert harness.client.closed
    assert "open entry" in capsys.readouterr().err


def test_connection_error_exits_1(harness: Harness, tmp_path: Path, capsys: Any) -> None:
    Recorder.connect_error = RehomConnectionError("controller unreachable")
    assert cli.main(_write_test(tmp_path / "j.jsonl", *FAN_UP)) == 1
    assert harness.client.closed
    assert "RehomConnectionError: controller unreachable" in capsys.readouterr().err


def test_missing_credentials_is_a_clean_error(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    async def missing(_service: str, *, username: str | None = None) -> tuple[str, str]:
        raise CredentialsError("no Keychain item")

    monkeypatch.setattr(cli, "read_keychain_credentials", missing)
    assert cli.main(_write_test(tmp_path / "j.jsonl", *FAN_UP)) == 1
    assert Recorder.instances == []
    err = capsys.readouterr().err
    assert "CredentialsError: no Keychain item" in err
    assert "Traceback" not in err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("--op", "predictive", "--value", "yes"), "on or off"),
        (("--op", "zone-offset", "--target", "1", "--value", "1"), "3-digit"),
        (("--op", "zone-offset", "--value", "1"), "--target zone"),
        (("--op", "vmc-fan", "--target", "001", "--value", "1_0"), "whole number"),
        (("--op", "house", "--value", "off"), "never switches the house off"),
        (("--op", "vmc-mode", "--target", "001", "--value", "cool"), "only to"),
        (
            ("--op", "vmc-mode", "--target", "001", "--value", "ventilate", "--dwell", "60"),
            "at least 600",
        ),
        (("--op", "vmc-fan", "--target", "001"), "--op and --value are required"),
    ],
)
def test_bad_arguments_exit_2_before_the_keychain(
    harness: Harness, tmp_path: Path, capsys: Any, args: tuple[str, ...], message: str
) -> None:
    assert cli.main(_write_test(tmp_path / "j.jsonl", *args, "--execute")) == 2
    assert harness.keychain_reads == 0
    assert Recorder.instances == []
    assert message in capsys.readouterr().err


def test_unknown_target_exits_2_without_writing(
    harness: Harness, tmp_path: Path, capsys: Any
) -> None:
    journal = tmp_path / "j.jsonl"
    args = ("--op", "zone-offset", "--target", "099", "--value", "1", "--execute")
    assert cli.main(_write_test(journal, *args)) == 2
    assert harness.client.rig.ctrl.writes == []
    assert harness.client.closed
    assert not journal.exists()
    assert "zone 099 is not present" in capsys.readouterr().err


@pytest.mark.parametrize("dwell", ["nan", "inf", "0", "0.5", "3601", "-5", "soon"])
def test_dwell_is_bounded_by_the_parser(harness: Harness, dwell: str) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["write-test", *FAN_UP, "--dwell", dwell])
    assert info.value.code == 2
    assert harness.keychain_reads == 0


def test_list_status_and_close(harness: Harness, tmp_path: Path, capsys: Any) -> None:
    journal = tmp_path / "j.jsonl"
    assert cli.main(["write-test", "--list"]) == 0
    assert "zone-offset" in capsys.readouterr().out

    assert cli.main(_write_test(journal, "--status")) == 0
    assert f"0 open entries in {journal.resolve()}" in capsys.readouterr().out

    journal.write_text(
        json.dumps({"id": "x", "event": "planned", "summary": "s", "originals": {"fan": 1}}) + "\n"
    )
    assert cli.main(_write_test(journal, "--status")) == 1
    assert "OPEN x: s originals={'fan': 1}" in capsys.readouterr().out

    assert cli.main(_write_test(journal, "--close", "typo")) == 4
    assert "not an open entry" in capsys.readouterr().err
    assert cli.main(_write_test(journal, "--close", "x")) == 0
    assert "closed x" in capsys.readouterr().out
    assert cli.main(_write_test(journal, "--status")) == 0
    assert Recorder.instances == [] and harness.keychain_reads == 0


def test_corrupt_journal_is_reported_not_a_traceback(
    harness: Harness, tmp_path: Path, capsys: Any
) -> None:
    journal = tmp_path / "j.jsonl"
    journal.write_text('{"id": "x", "event": "planned"}\n{"id": "x", "ev')
    assert cli.main(_write_test(journal, "--status")) == 4
    assert "corrupt at line 2" in capsys.readouterr().err
    assert cli.main(_write_test(journal, "--close", "x")) == 4
    assert cli.main(_write_test(journal, *FAN_UP)) == 4
    assert harness.client.rig.ctrl.writes == []


def _journal_for(argv: list[str]) -> Path:
    return cli._journal_path(cli.build_parser().parse_args(argv))


def test_default_journal_is_absolute_and_ignores_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the default was ./write-tests/journal.jsonl, so a cd hid open entries."""
    monkeypatch.delenv(cli.WRITE_JOURNAL_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    expected = (tmp_path / "state" / "aiorehom" / "write-tests" / "journal.jsonl").resolve()
    seen: list[Path] = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        monkeypatch.chdir(tmp_path / name)
        seen.append(_journal_for(["write-test", "--status"]))
    assert seen == [expected, expected]

    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")  # ignored, as XDG requires
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        cli.default_write_journal()
        == (
            tmp_path / "home" / ".local" / "state" / "aiorehom" / "write-tests" / "journal.jsonl"
        ).resolve()
    )
    monkeypatch.delenv("XDG_STATE_HOME")
    assert cli.default_write_journal().is_absolute()

    monkeypatch.setenv(cli.WRITE_JOURNAL_ENV, "rel/j.jsonl")  # would depend on the cwd again
    with pytest.raises(ValueError, match="absolute"):
        cli.default_write_journal()
    assert cli.main(["write-test", "--status"]) == 2
    monkeypatch.setenv(cli.WRITE_JOURNAL_ENV, str(tmp_path / "abs" / "j.jsonl"))
    assert cli.default_write_journal() == (tmp_path / "abs" / "j.jsonl").resolve()
    assert (
        _journal_for(["write-test", "--journal", "x.jsonl", "--status"])
        == (tmp_path / "b" / "x.jsonl").resolve()
    )


def test_status_prints_the_absolute_default_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.delenv(cli.WRITE_JOURNAL_ENV, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["write-test", "--status"]) == 0
    journal = (tmp_path / "state" / "aiorehom" / "write-tests" / "journal.jsonl").resolve()
    assert f"0 open entries in {journal}" in capsys.readouterr().out


def test_safety_guard_keeps_its_exit_code(harness: Harness, tmp_path: Path, capsys: Any) -> None:
    Recorder.connect_error = ForbiddenRequestError("GET /api/plant/msg/ is not allowlisted")
    assert cli.main(_write_test(tmp_path / "j.jsonl", *FAN_UP)) == 3
    assert harness.client.closed
    assert "safety guard" in capsys.readouterr().err


async def test_wall_clock_seams() -> None:
    before = datetime.now(UTC)
    assert before <= cli._wall_now() <= datetime.now(UTC)
    await cli._wall_sleep(0)
