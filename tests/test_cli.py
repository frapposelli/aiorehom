"""CLI end-to-end tests: aioresponses mocks for HTTP, a 127.0.0.1 server for WebSocket."""

from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import aioresponses

from aiorehom import cli
from aiorehom import credentials as credentials_module
from aiorehom.probe import controller_now, first_zone_id

from .conftest import (
    ALIVE,
    BASE,
    CONFIG,
    DOMOTICA,
    HISTORY,
    INTERFACE,
    ME,
    OVERRIDES,
    PLANT_CONF,
    SECRET_VALUES,
    TEST_HOST,
)

TOKEN = "tok-abcdef0123456789"
KEYCHAIN_PASSWORD = "keychain-pw-!@#"
ATTRS = b'attributes:\n    "acct"<blob>="alice"\n'
PERSONAL = ("Soggiorno di Alice", "TestNet", "B8:27:EB:12:34:56", "A1234567", "Testville")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "SESSION_MIN_INTERVAL_S", 0.0)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run(args: list[str]) -> tuple[int, bytes]:
        calls.append(list(args))
        if "-w" in args:
            return 0, KEYCHAIN_PASSWORD.encode() + b"\n"
        return 0, ATTRS

    monkeypatch.setattr(credentials_module, "_run_security", fake_run)
    return calls


def _register_all(m: aioresponses) -> None:
    m.get(f"{BASE}/api/alive/", payload=ALIVE)
    m.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    m.get(f"{BASE}/api/me/", payload=ME)
    m.get(f"{BASE}/api/config/", payload=CONFIG)
    m.get(f"{BASE}/api/interface/", payload=INTERFACE)  # step 4 (authenticated)
    m.get(f"{BASE}/api/overrides/", payload=OVERRIDES)
    m.get(f"{BASE}/api/plant/conf/", payload=PLANT_CONF)
    for resource, payload in DOMOTICA.items():
        m.get(f"{BASE}/api/domotica/{resource}/", payload=payload)
    m.get(re.compile(r"^http://rehom\.test:8000/api/history/\?.*$"), payload=HISTORY, repeat=True)
    m.get(
        re.compile(r"^http://rehom\.test:8000/api/interface/\?.*$"),
        payload=[r for r in INTERFACE if r["Gruppo"] == "ZONA"],
        repeat=True,
    )


def _calls(m: aioresponses) -> list[tuple[str, Any, dict[str, Any]]]:
    return [
        (method, url, call.kwargs) for (method, url), calls in m.requests.items() for call in calls
    ]


def _assert_private_tree(root: Path) -> None:
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for path in root.rglob("*"):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), (path, oct(mode))


def _all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file())


@pytest.fixture
def session_dir(
    tmp_path: Path, keychain: list[list[str]], capsys: pytest.CaptureFixture[str]
) -> Path:
    out = tmp_path / "captures" / "run1"
    with aioresponses() as m:
        _register_all(m)
        code = cli.main(["--host", TEST_HOST, "--out", str(out), "session"])
        calls = _calls(m)
    assert code == 0
    # exactly the allowlisted requests, the login once
    seen = sorted((method, url.path) for method, url, _k in calls)
    assert seen == sorted(
        [
            ("GET", "/api/alive/"),
            ("POST", "/api/get-token/"),
            ("GET", "/api/me/"),
            ("GET", "/api/config/"),
            ("GET", "/api/interface/"),
            ("GET", "/api/overrides/"),
            ("GET", "/api/plant/conf/"),
            ("GET", "/api/domotica/object/"),
            ("GET", "/api/domotica/rooms/"),
            ("GET", "/api/domotica/scenarios/"),
            ("GET", "/api/domotica/config/"),
            *[("GET", "/api/history/")] * 5,
            ("GET", "/api/interface/"),
            ("GET", "/api/interface/"),
        ]
    )
    history = sorted(
        (url.query["Key"], url.query["Unita"], url.query["Tempo__gte"], url.query["Tempo__lte"])
        for method, url, _k in calls
        if url.path == "/api/history/"
    )
    window = ("2026-09-25 04:15:30", "2026-09-25 10:15:30")
    assert history == sorted(
        [
            ("CONT_CALORIE", "", *window),
            ("CONT_ACQUA_CALDA", "", *window),
            ("TEMP_AMBIENTE", "002", *window),
            ("SETP_CORRENTE", "002", *window),
            ("TEMP_ESTERNA", "", *window),
        ]
    )
    filtered = sorted(
        tuple(sorted(url.query.items())) for _m, url, _k in calls if url.path == "/api/interface/"
    )
    assert filtered == [
        (),
        (("Gruppo", "ZONA"), ("Unita", "002")),
        (("Key__in", "STAGIONE,MODO"),),
    ]
    login = [k for method, _u, k in calls if method == "POST"]
    assert login[0]["json"] == {"username": "alice", "password": KEYCHAIN_PASSWORD}
    assert keychain == [
        ["find-generic-password", "-s", "rehom-api"],
        ["find-generic-password", "-s", "rehom-api", "-a", "alice", "-w"],
    ]
    printed = capsys.readouterr().out
    assert "step 8 (filtered interface): ok" in printed
    assert KEYCHAIN_PASSWORD not in printed and TOKEN not in printed
    return out


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------


def test_session_end_to_end(session_dir: Path) -> None:
    out = session_dir
    names = sorted(p.name for p in out.iterdir())
    assert names == sorted(
        [
            "alive.json",
            "me.json",
            "config.json",
            "interface.json",
            "overrides.json",
            "plant_conf.json",
            "domotica_object.json",
            "domotica_rooms.json",
            "domotica_scenarios.json",
            "domotica_config.json",
            "history_CONT_CALORIE_.json",
            "history_CONT_ACQUA_CALDA_.json",
            "history_TEMP_AMBIENTE_002.json",
            "history_SETP_CORRENTE_002.json",
            "history_TEMP_ESTERNA_.json",
            "interface_filtered_zona_002.json",
            "interface_filtered_key_in.json",
            "requests.jsonl",
            "meta.json",
            "records.tsv",
        ]
    )
    _assert_private_tree(out)
    assert stat.S_IMODE(out.parent.stat().st_mode) == 0o700  # created parent too
    text = _all_text(out)
    for secret in (*SECRET_VALUES, KEYCHAIN_PASSWORD, TOKEN):
        assert secret not in text, secret
    meta = json.loads((out / "meta.json").read_text())
    assert {s["status"] for s in meta["steps"].values()} == {"ok"}
    assert meta["login"]["state"] == "ok"
    assert meta["login"]["username_source"] == "keychain"
    assert meta["redaction_total"] == 6 + 1  # interface secrets + config METEO_KEY
    assert meta["redaction_counts"]["interface.json"] == 6
    assert meta["history_window"]["source"] == "LOCAL_TIME"
    assert meta["requests"] == 18
    requests = [json.loads(line) for line in (out / "requests.jsonl").read_text().splitlines()]
    assert len(requests) == 18
    assert all(
        set(r) >= {"method", "path", "status", "latency_ms", "bytes", "ok"} for r in requests
    )
    assert requests[1]["path"] == "/api/get-token/" and requests[1]["query"] is None
    tsv = (out / "records.tsv").read_text().splitlines()
    assert tsv[0].split("\t")[:5] == ["store", "Gruppo", "Unita", "SubUni", "Key"]
    assert any(line.startswith("interface\tPROG\t002\t0\tPROG_SETT_INVERNO\t1\t") for line in tsv)
    assert any(line.startswith("overrides\tPROG_OVERRIDE\t002\t1\t") for line in tsv)
    interface = json.loads((out / "interface.json").read_text())
    uten = [r for r in interface if r["Gruppo"] == "UTEN"]
    assert {r["Valore"] for r in uten} == {"<redacted len=14>", "<redacted len=8>"}
    config = json.loads((out / "config.json").read_text())
    assert config["METEO_KEY"] == "<redacted len=21>"


def test_session_no_login(tmp_path: Path, keychain: list[list[str]]) -> None:
    out = tmp_path / "nologin"
    with aioresponses() as m:
        m.get(f"{BASE}/api/alive/", payload=ALIVE)
        code = cli.main(["--host", TEST_HOST, "--out", str(out), "--no-login", "session"])
        calls = _calls(m)
    assert code == 0
    assert [(method, url.path) for method, url, _k in calls] == [("GET", "/api/alive/")]
    assert keychain == []
    meta = json.loads((out / "meta.json").read_text())
    for number in range(2, 9):
        assert meta["steps"][str(number)]["status"] == "skipped"
        assert "--no-login" in meta["steps"][str(number)]["detail"][0]
    assert meta["login"] == {
        "enabled": False,
        "state": "disabled",
        "attempted": False,
        "username_source": None,
        "error": None,
    }
    assert not (out / "records.tsv").exists()
    _assert_private_tree(out)


def test_session_login_failure(tmp_path: Path, keychain: list[list[str]], capsys: Any) -> None:
    out = tmp_path / "badlogin"
    with aioresponses() as m:
        m.post(f"{BASE}/api/get-token/", status=400, payload={"non_field_errors": ["bad"]})
        code = cli.main(
            [
                "--host",
                TEST_HOST,
                "--out",
                str(out),
                "--username",
                "me",
                "session",
                "--steps",
                "2-3",
            ]
        )
        calls = _calls(m)
    assert code == 1
    assert len(calls) == 1  # one login attempt only, no retries
    assert keychain == [["find-generic-password", "-s", "rehom-api", "-a", "me", "-w"]]
    meta = json.loads((out / "meta.json").read_text())
    assert meta["login"]["state"] == "failed"
    assert meta["login"]["error"] == "login failed: RehomHttpError (HTTP 400)"
    assert meta["login"]["username_source"] == "override"
    assert meta["steps"]["3"]["status"] == "skipped"
    assert "login failed" in capsys.readouterr().err


def test_session_missing_keychain_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    async def missing(args: list[str]) -> tuple[int, bytes]:
        return 44, b""

    monkeypatch.setattr(credentials_module, "_run_security", missing)
    out = tmp_path / "nokeychain"
    with aioresponses() as m:
        code = cli.main(["--host", TEST_HOST, "--out", str(out), "session", "--steps", "2"])
        assert _calls(m) == []
    assert code == 1
    err = capsys.readouterr().err
    assert "security add-generic-password -s rehom-api -a <username> -w" in err


def test_session_step_errors_and_prerequisites(tmp_path: Path, keychain: list[list[str]]) -> None:
    out = tmp_path / "partial"
    with aioresponses() as m:
        m.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
        m.get(f"{BASE}/api/domotica/object/", status=500)
        m.get(f"{BASE}/api/domotica/rooms/", payload=[])
        m.get(f"{BASE}/api/domotica/scenarios/", payload=[])
        m.get(f"{BASE}/api/domotica/config/", payload={})
        m.get(re.compile(r"^http://rehom\.test:8000/api/interface/\?.*$"), payload=[], repeat=True)
        code = cli.main(["--host", TEST_HOST, "--out", str(out), "session", "--steps", "6-8"])
    assert code == 0
    meta = json.loads((out / "meta.json").read_text())
    steps = meta["steps"]
    assert steps["6"]["status"] == "ok"
    assert "domotica/object" in steps["6"]["detail"][0]
    assert steps["7"]["status"] == "skipped"
    assert "needs step 3" in steps["7"]["detail"][0]
    assert steps["8"]["status"] == "ok"
    assert "no zone known" in steps["8"]["detail"][0]
    assert "9" not in steps


def test_session_http_error_marks_step(tmp_path: Path, keychain: list[list[str]]) -> None:
    out = tmp_path / "err"
    with aioresponses() as m:
        m.get(f"{BASE}/api/alive/", status=502)
        code = cli.main(
            ["--host", TEST_HOST, "--out", str(out), "--no-login", "session", "--steps", "1"]
        )
    assert code == 1
    meta = json.loads((out / "meta.json").read_text())
    assert meta["steps"]["1"]["status"] == "error"
    assert "HTTP 502" in meta["steps"]["1"]["detail"][0]


def test_session_safety_guard_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from aiorehom.probe import ProbeSession

    async def evil_step(self: ProbeSession, step: Any) -> None:
        transport: Any = self.transport
        await transport._request("GET", "/api/plant/conf/export/")

    monkeypatch.setattr(ProbeSession, "_step1", evil_step)
    with aioresponses() as m:
        code = cli.main(
            [
                "--host",
                TEST_HOST,
                "--out",
                str(tmp_path / "g"),
                "--no-login",
                "session",
                "--steps",
                "1",
            ]
        )
        assert _calls(m) == []
    assert code == 3
    assert "safety guard" in capsys.readouterr().err
    meta = json.loads((tmp_path / "g" / "meta.json").read_text())
    assert meta["steps"]["1"]["status"] == "error"


def test_default_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REHOM_PROBE_CAPTURE_ROOT", str(tmp_path / "root"))
    with aioresponses() as m:
        m.get(f"{BASE}/api/alive/", payload=ALIVE)
        assert cli.main(["--host", TEST_HOST, "--no-login", "session", "--steps", "1"]) == 0
    (run,) = list((tmp_path / "root").iterdir())
    assert re.fullmatch(r"\d{8}T\d{6}Z", run.name)
    assert (run / "alive.json").is_file()
    _assert_private_tree(tmp_path / "root")


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def test_parse_steps() -> None:
    assert cli._parse_steps("1,3-5, 8") == [1, 3, 4, 5, 8]
    assert cli._parse_steps("2,2,1") == [1, 2]
    for bad in ("0", "9", "x", "3-x", "", ",", "1-9"):
        with pytest.raises(Exception, match="step"):
            cli._parse_steps(bad)


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "--minutes", "121"],
        ["watch", "--minutes", "0"],
        ["watch", "--minutes", "abc"],
        ["latency", "--seconds", "601"],
        ["--port", "70000", "session"],
        ["--port", "x", "session"],
        ["session", "--steps", "12"],
        ["raw", "GET", "/api/plant/msg/"],
        [],
    ],
)
def test_bad_arguments_exit(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2


def test_invalid_host_is_reported(tmp_path: Path, capsys: Any) -> None:
    code = cli.main(["--host", "http://evil", "--out", str(tmp_path / "x"), "session"])
    assert code == 2
    assert "invalid host" in capsys.readouterr().err


def test_no_raw_request_command() -> None:
    parser = cli.build_parser()
    commands = set(parser._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    assert commands == {
        "session",
        "watch",
        "latency",
        "inventory",
        "diff",
        "redact-har",
        "sanitize-fixtures",
        "replay",  # offline: injected replay transport, never a socket
    }


# ---------------------------------------------------------------------------
# inventory / diff / sanitize (offline)
# ---------------------------------------------------------------------------


def test_inventory(
    session_dir: Path, tmp_path: Path, mini_catalog: dict[str, Any], capsys: Any
) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(mini_catalog))
    assert cli.main(["inventory", str(session_dir), "--catalog", str(catalog), "--show-names"]) == 0
    text = capsys.readouterr().out
    assert "Zone units (2 with rows or flagged; 2 flagged present in PRESENZA_SONDE):" in text
    assert "002  present=True online=True name=Soggiorno di Alice temp=21.4" in text
    assert "003  present=True online=False name=Camera" in text
    assert (
        "001  present=True online=True name=VMC Casa ST_MODE=1 COM_VENTILA=2 ST_STATO_DEUM=1"
        in text
    )
    assert "MODO = 2" in text and "TERMO_READONLY = 0" in text and "ABILITA_DOMOTICA = 0" in text
    assert "[interface] REHOM...WEBSERVER = 1" in text
    assert "CONFIGURA_ON = 0" in text and "CONF_ZONE_ON = (absent)" in text
    # secrets: presence only, never a length
    assert "UTEN.<user #1> = present (same as /me/ username: True)" in text
    assert "UTEN.admin = present" in text
    assert "WIFI.PASSWORD = present" in text
    assert "config.json:METEO_KEY = present" in text
    assert "len=" not in text
    assert "alice" not in text.replace("Alice", "")
    assert "interface records NOT seen (1): REHOM.NEVER_THERE" in text
    assert "plant-conf keys NOT seen (1): CONF_ZONE_ON" in text
    assert "brand_new_key" in text
    assert "config fields NOT seen (1): MISSING_FIELD" in text
    assert "history_TEMP_AMBIENTE_002.json: 2 points" in text
    for secret in SECRET_VALUES:
        assert secret not in text

    for flags in ([], ["--hide-names"]):  # hidden by default
        assert cli.main(["inventory", str(session_dir), "--catalog", str(catalog), *flags]) == 0
        hidden = capsys.readouterr().out
        assert "Soggiorno" not in hidden and "name=<hidden>" in hidden
        assert "VMC Casa" not in hidden and "Camera" not in hidden

    assert (
        cli.main(["inventory", str(session_dir), "--catalog", str(tmp_path / "nope"), "--json"])
        == 0
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "catalog" not in data
    assert "coverage skipped" in captured.err
    assert cli.main(["inventory", str(tmp_path / "missing")]) == 2


def test_inventory_with_real_catalog(session_dir: Path, capsys: Any) -> None:
    catalog = cli.default_catalog()
    if catalog is None or not catalog.is_file():
        pytest.skip("record catalogue not available")
    assert cli.main(["inventory", str(session_dir)]) == 0
    text = capsys.readouterr().out
    assert "Record catalogue coverage:" in text
    assert "REHOM.STAGIONE" in text


def test_diff(session_dir: Path, tmp_path: Path, capsys: Any) -> None:
    a = tmp_path / "A"
    b = tmp_path / "B"
    shutil.copytree(session_dir, a)
    shutil.copytree(session_dir, b)
    interface = json.loads((b / "interface.json").read_text())
    for row in interface:
        if (row["Gruppo"], row["Unita"], row["Key"]) == ("ZONA", "002", "SETP_CORRENTE"):
            row["Valore"] = "4"
        if (row["Gruppo"], row["Unita"], row["Key"]) == ("ZONA", "002", "TEMP_AMBIENTE"):
            row["Valore"] = "22.0"
    interface = [r for r in interface if r["Key"] != "ALG_ATTIVO"]
    interface.append(
        {"Gruppo": "ZONA", "Unita": "002", "SubUni": "", "Key": "SET_FORZ", "Valore": "1"}
    )
    (b / "interface.json").write_text(json.dumps(interface))
    config = json.loads((b / "config.json").read_text())
    config["LOCAL_TIME"] = "2026-09-25 11:00:00"
    config["NEW"] = {"x": 1}
    (b / "config.json").write_text(json.dumps(config))
    overrides = json.loads((b / "overrides.json").read_text())
    overrides[0]["Scadenza"] = "2026-09-25 12:00:00"
    (b / "overrides.json").write_text(json.dumps(overrides))

    assert cli.main(["diff", str(a), str(b)]) == 0
    text = capsys.readouterr().out
    assert "~ interface ZONA.002..SETP_CORRENTE: '0' -> '4'" in text
    assert "~ interface ZONA.002..TEMP_AMBIENTE: '21.4' -> '22.0'" in text
    assert "- interface REHOM...ALG_ATTIVO (was '0')" in text
    assert "+ interface ZONA.002..SET_FORZ = '1'" in text
    assert "~ config CONFIG_API...LOCAL_TIME" in text
    assert "+ config CONFIG_API...NEW = '{\"x\": 1}'" in text
    assert "Scadenza: '2026-09-25 11:00:00' -> '2026-09-25 12:00:00'" in text
    assert "7 difference(s)" in text

    volatile = tmp_path / "volatile.txt"
    volatile.write_text("# volatile keys\nZONA.*..TEMP_AMBIENTE\nconfig:*LOCAL_TIME\n\n")
    assert cli.main(["diff", str(a), str(b), "--ignore-volatile", str(volatile)]) == 0
    text = capsys.readouterr().out
    assert "TEMP_AMBIENTE" not in text and "LOCAL_TIME" not in text
    assert "5 difference(s)" in text

    volatile.write_text(json.dumps(["*.SETP_CORRENTE", "interface:*ALG_ATTIVO", "", 5]))
    assert cli.main(["diff", str(a), str(b), "--ignore-volatile", str(volatile), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [c["path"] for c in data["interface"]["changed"]] == ["ZONA.002..TEMP_AMBIENTE"]
    assert data["interface"]["removed"] == []
    assert cli.main(["diff", str(a), str(tmp_path / "missing")]) == 2
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["diff", str(empty), str(empty)]) == 0
    assert "0 difference(s)" in capsys.readouterr().out


def test_sanitize_fixtures(session_dir: Path, tmp_path: Path, capsys: Any) -> None:
    (session_dir / "extra.bin").write_bytes(b"\x00")
    out = tmp_path / "fixtures"
    assert cli.main(["sanitize-fixtures", str(session_dir), str(out), "--salt", "s1"]) == 0
    printed = capsys.readouterr().out
    assert "skipped: extra.bin" in printed
    text = _all_text(out)
    for value in (*SECRET_VALUES, *PERSONAL, "alice@example.org", TEST_HOST):
        assert value not in text, value
    interface = json.loads((out / "interface.json").read_text())
    names = {r["Valore"] for r in interface if r["Key"] == "NOME"}
    assert names == {"Zona 002", "Zona 003", "VMC 001"}
    assert (out / "records.tsv").is_file()
    assert (out / "SANITISED.txt").is_file()
    assert (out / "requests.jsonl").is_file()
    _assert_private_tree(out)
    first = (out / "interface.json").read_text()
    out2 = tmp_path / "fixtures2"
    assert cli.main(["sanitize-fixtures", str(session_dir), str(out2), "--salt", "s1"]) == 0
    assert (out2 / "interface.json").read_text() == first
    out3 = tmp_path / "fixtures3"
    assert cli.main(["sanitize-fixtures", str(session_dir), str(out3), "--random-salt"]) == 0
    assert "random one-off salt" in capsys.readouterr().out
    assert cli.main(["sanitize-fixtures", str(session_dir), str(session_dir)]) == 2
    assert cli.main(["sanitize-fixtures", str(tmp_path / "missing"), str(out3)]) == 2


def test_sanitize_capture_reredacts_leaks(tmp_path: Path) -> None:
    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "interface.json").write_text(json.dumps(INTERFACE))  # unredacted on purpose
    (capture / "config.json").write_text(json.dumps(CONFIG))
    (capture / "ws.jsonl").write_text(
        json.dumps(
            {
                "t": "x",
                "mono": 1,
                "frame": {"domain": "termo", "path": "X...API_TOKEN", "value": "ws-secret-123"},
            }
        )
        + "\n"
        + json.dumps({"t": "x", "mono": 2, "event": {"event": "connect", "token": "t0k"}})
        + "\n\n"
    )
    (capture / "broken.json").write_text("{not json")
    from aiorehom.fixtures import sanitize_capture

    summary = sanitize_capture(capture, tmp_path / "fx", "salt")
    text = _all_text(tmp_path / "fx")
    for value in (*SECRET_VALUES, "ws-secret-123", "t0k", *PERSONAL):
        assert value not in text, value
    assert summary["redaction_counts"]["interface.json"] == 6
    assert summary["redaction_counts"]["ws.jsonl"] == 2
    assert json.loads((tmp_path / "fx" / "broken.json").read_text()) == {"_unreadable": True}


# ---------------------------------------------------------------------------
# redact-har
# ---------------------------------------------------------------------------


def test_redact_har_command(tmp_path: Path, capsys: Any) -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "http://198.51.100.8:8000/api/get-token/",
                        "headers": [{"name": "Content-Type", "value": "application/json"}],
                        "postData": {
                            "mimeType": "application/json",
                            "text": json.dumps({"username": "u", "password": "p4ss"}),
                        },
                    },
                    "response": {
                        "headers": [],
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps({"token": TOKEN}),
                        },
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "http://198.51.100.8:8000/api/interface/",
                        "headers": [{"name": "Authorization", "value": f"Token {TOKEN}"}],
                    },
                    "response": {
                        "headers": [],
                        "content": {"mimeType": "application/json", "text": json.dumps(INTERFACE)},
                    },
                },
            ]
        }
    }
    src = tmp_path / "in.har"
    src.write_text(json.dumps(har))
    dst = tmp_path / "sub" / "out.har"
    assert cli.main(["redact-har", str(src), str(dst)]) == 0
    text = dst.read_text()
    for value in (*SECRET_VALUES, "p4ss", TOKEN):
        assert value not in text
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert '"login_bodies_dropped": 2' in capsys.readouterr().out
    assert cli.main(["redact-har", str(src), str(src)]) == 2
    bad = tmp_path / "bad.har"
    bad.write_text("[1, 2]")
    assert cli.main(["redact-har", str(bad), str(tmp_path / "o.har")]) == 2
    bad.write_text("{nope")
    assert cli.main(["redact-har", str(bad), str(tmp_path / "o.har")]) == 2
    assert (
        cli.main(["redact-har", str(src), str(tmp_path / "o2.har"), "--controller-host", "other"])
        == 0
    )
    assert TOKEN not in (tmp_path / "o2.har").read_text()


# ---------------------------------------------------------------------------
# watch / latency against a 127.0.0.1 server
# ---------------------------------------------------------------------------


async def _local_server(send_frames: int = 2) -> TestServer:
    async def ws_handler(request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(
            json.dumps(
                {
                    "domain": "termo",
                    "type": "update",
                    "path": "X...API_TOKEN",
                    "value": "secret-value-1",
                }
            )
        )
        for i in range(send_frames - 1):
            await ws.send_str(
                json.dumps({"domain": "bus", "type": "update", "key": "k", "value": i})
            )
        async for _msg in ws:
            pass
        return ws

    async def alive_handler(request: web.Request) -> web.Response:
        assert "Authorization" not in request.headers
        return web.json_response({"version": "t"})

    app = web.Application()
    app.router.add_get("/ws/", ws_handler)
    app.router.add_get("/api/alive/", alive_handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server


async def test_watch_with_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "WATCH_ALIVE_PERIOD_S", 0.1)
    server = await _local_server()
    out = tmp_path / "watch"
    args = cli.build_parser().parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            str(server.port),
            "--out",
            str(out),
            "watch",
            "--minutes",
            "0.01",
            "--with-latency",
        ]
    )
    try:
        assert await cli.cmd_watch(args) == 0
    finally:
        await server.close()
    lines = [json.loads(line) for line in (out / "ws.jsonl").read_text().splitlines()]
    frames = [line["frame"] for line in lines if "frame" in line]
    events = [line["event"]["event"] for line in lines if "event" in line]
    assert frames[0]["value"] == "<redacted len=14>"
    assert len(frames) == 2
    assert events[0] == "connect"
    assert all(
        set(line) == {"t", "mono", "frame"} or set(line) == {"t", "mono", "event"} for line in lines
    )
    assert all(line["t"].endswith("Z") for line in lines)
    alive = [json.loads(line) for line in (out / "alive_latency.jsonl").read_text().splitlines()]
    assert alive and all(a["alive"]["ok"] for a in alive)
    meta = json.loads((out / "watch_meta.json").read_text())
    assert meta["ws"]["frames"] == 2
    assert meta["ws"]["redacted_values"] == 1
    assert meta["alive_latency"]["count"] == len(alive)
    assert "secret-value-1" not in _all_text(out)
    _assert_private_tree(out)


async def test_watch_without_latency(tmp_path: Path) -> None:
    server = await _local_server(send_frames=1)
    out = tmp_path / "watch2"
    args = cli.build_parser().parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            str(server.port),
            "--out",
            str(out),
            "watch",
            "--minutes",
            "0.005",
        ]
    )
    try:
        assert await cli.cmd_watch(args) == 0
    finally:
        await server.close()
    assert not (out / "alive_latency.jsonl").exists()
    meta = json.loads((out / "watch_meta.json").read_text())
    assert meta["alive_latency"] is None


async def test_latency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "LATENCY_PERIOD_S", 0.05)
    server = await _local_server()
    out = tmp_path / "latency"
    args = cli.build_parser().parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            str(server.port),
            "--out",
            str(out),
            "latency",
            "--seconds",
            "0.3",
        ]
    )
    try:
        assert await cli.cmd_latency(args) == 0
    finally:
        await server.close()
    samples = [json.loads(line) for line in (out / "latency.jsonl").read_text().splitlines()]
    assert 2 <= len(samples) <= 8
    assert all(s["alive"]["ok"] for s in samples)
    meta = json.loads((out / "latency_meta.json").read_text())
    assert meta["samples"] == len(samples)
    assert meta["summary"]["count"] == len(samples)
    _assert_private_tree(out)


async def test_latency_records_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(cli, "LATENCY_PERIOD_S", 0.05)
    out = tmp_path / "latency_fail"
    args = cli.build_parser().parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--out",
            str(out),
            "latency",
            "--seconds",
            "0.1",
        ]
    )
    assert await cli.cmd_latency(args) == 0
    samples = [json.loads(line) for line in (out / "latency.jsonl").read_text().splitlines()]
    assert samples and not samples[0]["alive"]["ok"]
    assert samples[0]["alive"]["error"] == "RehomConnectionError"
    assert json.loads((out / "latency_meta.json").read_text())["summary"] == {"count": 0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_controller_now_variants() -> None:
    assert controller_now({"LOCAL_TIME": "2026-09-25 10:15:30.123"})[0].isoformat() == (  # type: ignore[index]
        "2026-09-25T10:15:30"
    )
    utc = controller_now({"LOCAL_TIME": "2026-09-25T08:15:30Z", "TIMEZONE": "Europe/Rome"})
    assert utc is not None and utc[0].isoformat() == "2026-09-25T10:15:30"
    offset = controller_now({"LOCAL_TIME": "2026-09-25T08:15:30+00:00"})
    assert offset is not None and offset[0].isoformat() == "2026-09-25T08:15:30"
    fallback = controller_now({"LOCAL_TIME": "garbage", "TIMEZONE": "Europe/Rome"})
    assert fallback is not None and fallback[1] == "host clock in TIMEZONE"
    assert controller_now({"LOCAL_TIME": "garbage", "TIMEZONE": "Not/AZone"}) is None
    assert controller_now({}) is None
    assert controller_now(None) is None


def test_first_zone() -> None:
    assert first_zone_id(INTERFACE) == "002"
    rows = [
        {"Gruppo": "ZONA", "Unita": "007", "SubUni": "", "Key": "NOME", "Valore": "x"},
        {"Gruppo": "ZONA", "Unita": "000", "SubUni": "1", "Key": "ICO_IMG", "Valore": "a"},
    ]
    assert first_zone_id(rows) == "007"
    assert first_zone_id([]) is None


def test_io_error_is_reported(tmp_path: Path, capsys: Any) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    code = cli.main(["--host", TEST_HOST, "--out", str(blocker / "sub"), "--no-login", "session"])
    assert code == 1
    assert "I/O error" in capsys.readouterr().err


def test_inventory_masks_user_names_in_coverage() -> None:
    from aiorehom.inventory import build_inventory, format_inventory

    capture = {"interface.json": INTERFACE, "me.json": {"username": "someone-else"}}
    inventory = build_inventory(capture, {"interface_records": []})
    text = format_inventory(inventory)
    assert "UTEN.<user>" in text
    assert "UTEN.admin" in text
    assert "alice" not in text
    assert "(same as /me/ username: False)" in text


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


async def test_watch_with_latency_paces_ws_handshake_and_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the /ws/ handshake and the first /api/alive/ were sent together."""
    import time

    monkeypatch.setattr(cli, "SESSION_MIN_INTERVAL_S", 0.3)
    monkeypatch.setattr(cli, "WATCH_ALIVE_PERIOD_S", 5.0)
    arrivals: list[tuple[float, str]] = []

    async def ws_handler(request: web.Request) -> web.StreamResponse:
        arrivals.append((time.monotonic(), "/ws/"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _msg in ws:
            pass
        return ws

    async def alive_handler(request: web.Request) -> web.Response:
        arrivals.append((time.monotonic(), "/api/alive/"))
        return web.json_response({"version": "t"})

    app = web.Application()
    app.router.add_get("/ws/", ws_handler)
    app.router.add_get("/api/alive/", alive_handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    args = cli.build_parser().parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            str(server.port),
            "--out",
            str(tmp_path / "w"),
            "watch",
            "--minutes",
            "0.02",
            "--with-latency",
        ]
    )
    try:
        assert await cli.cmd_watch(args) == 0
    finally:
        await server.close()
    paths = sorted(p for _t, p in arrivals)
    assert paths == ["/api/alive/", "/ws/"]
    times = sorted(t for t, _p in arrivals)
    assert times[1] - times[0] >= 0.3 - 0.02, arrivals


def _probe_mocks(m: aioresponses) -> None:
    m.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    m.get(f"{BASE}/api/interface/", payload=INTERFACE)


def test_session_sub_requests_are_independent(tmp_path: Path, keychain: list[list[str]]) -> None:
    """Regression: a failing first sub-request of steps 5 and 8 skipped the second one."""
    out = tmp_path / "subreq"
    with aioresponses() as m:
        _probe_mocks(m)
        m.get(f"{BASE}/api/overrides/", status=500)
        m.get(f"{BASE}/api/plant/conf/", payload=PLANT_CONF)
        m.get(re.compile(r"^http://rehom\.test:8000/api/interface/\?Gruppo=.*$"), status=400)
        m.get(re.compile(r"^http://rehom\.test:8000/api/interface/\?Key__in=.*$"), payload=[])
        code = cli.main(["--host", TEST_HOST, "--out", str(out), "session", "--steps", "4,5,8"])
        calls = _calls(m)
    assert code == 1
    requested = [(method, url.path, tuple(sorted(url.query))) for method, url, _k in calls]
    assert ("GET", "/api/plant/conf/", ()) in requested
    assert ("GET", "/api/interface/", ("Key__in",)) in requested
    meta = json.loads((out / "meta.json").read_text())
    steps = meta["steps"]
    for number in ("5", "8"):
        assert steps[number]["status"] == "error", steps[number]
    assert "plant_conf.json" in steps["5"]["files"]
    assert any(d.startswith("overrides: RehomHttpError") for d in steps["5"]["detail"])
    assert "interface_filtered_key_in.json" in steps["8"]["files"]
    assert any("?Gruppo=ZONA&Unita=002" in d for d in steps["8"]["detail"])
    assert (out / "plant_conf.json").is_file()


def _write_capture(root: Path, name: str, files: dict[str, Any]) -> Path:
    directory = root / name
    directory.mkdir()
    for file_name, content in files.items():
        (directory / file_name).write_text(json.dumps(content))
    return directory


def test_inventory_json_honours_hide_names_and_reredacts(tmp_path: Path, capsys: Any) -> None:
    """Regressions: --json ignored --hide-names; secrets were printed verbatim if unredacted."""
    cap = _write_capture(tmp_path, "raw", {"interface.json": INTERFACE, "config.json": CONFIG})
    for flags in ([], ["--hide-names"]):
        assert cli.main(["inventory", str(cap), "--json", "--catalog", "none.json", *flags]) == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert {z["NOME"] for z in data["zones"]} == {"<hidden>"}
        assert {v["NOME"] for v in data["vmcs"]} == {"<hidden>"}
        assert data["names_hidden"] is True
        for leaked in ("Soggiorno", "Camera", "VMC Casa", *SECRET_VALUES):
            assert leaked not in out, leaked
    assert cli.main(["inventory", str(cap), "--json", "--show-names", "--catalog", "x"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {z["NOME"] for z in data["zones"]} == {"Soggiorno di Alice", "Camera"}
    # text mode on the same unredacted capture: markers only under "(redacted)"
    assert cli.main(["inventory", str(cap), "--catalog", "x"]) == 0
    text = capsys.readouterr().out
    for leaked in SECRET_VALUES:
        assert leaked not in text, leaked
    assert "UTEN.<user #1> = present" in text
    assert "WIFI.PASSWORD = present" in text
    assert "UTEN.admin = present" in text
    assert "config.json:METEO_KEY = present" in text
    assert "len=" not in text


def test_inventory_override_coverage_and_counts(
    tmp_path: Path, mini_catalog: dict[str, Any], capsys: Any
) -> None:
    """Regressions: override rows were matched against interface_records; zone count and
    duplicate rows were misleading."""
    interface = [
        *INTERFACE,
        *[
            {"Gruppo": "ZONA", "Unita": f"{i:03d}", "SubUni": "", "Key": "NOME", "Valore": "z"}
            for i in range(4, 25)
        ],
        {"Gruppo": "REHOM", "Unita": "", "SubUni": "", "Key": "MODO", "Valore": "9"},
    ]
    cap = _write_capture(
        tmp_path, "cap", {"interface.json": interface, "overrides.json": OVERRIDES}
    )
    catalog = {
        **mini_catalog,
        "override_records": [
            {"gruppo": "PROG_OVERRIDE", "key": "PROG_GIORNO_{season:INVERNO|ESTATE}"},
            {"gruppo": "PROG_OVERRIDE", "key": "NEVER_THERE"},
        ],
    }
    catalog_file = tmp_path / "catalog.json"
    catalog_file.write_text(json.dumps(catalog))
    assert cli.main(["inventory", str(cap), "--catalog", str(catalog_file), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    coverage = data["catalog"]
    assert coverage["overrides_seen"] == ["PROG_OVERRIDE.PROG_GIORNO_{season:INVERNO|ESTATE}"]
    assert coverage["overrides_not_seen"] == ["PROG_OVERRIDE.NEVER_THERE"]
    assert coverage["overrides_uncatalogued"] == []
    assert not any("PROG_OVERRIDE" in k for k in coverage["interface_uncatalogued"])
    assert data["zones_flagged_present"] == 2
    assert len(data["zones"]) == 23
    assert data["records"]["interface"]["raw_rows"] == len(interface)
    assert data["records"]["interface"]["duplicates"] == {"REHOM...MODO": 2}
    assert cli.main(["inventory", str(cap), "--catalog", str(catalog_file)]) == 0
    text = capsys.readouterr().out
    assert "Zone units (23 with rows or flagged; 2 flagged present in PRESENZA_SONDE):" in text
    assert "duplicate identities (last row wins): [interface] REHOM...MODO x2" in text
    assert f"raw rows: interface={len(interface)} overrides=1" in text
    assert "override records seen (1): PROG_OVERRIDE.PROG_GIORNO_" in text
    assert "override records NOT seen (1): PROG_OVERRIDE.NEVER_THERE" in text


def test_inventory_real_catalog_covers_overrides() -> None:
    catalog_path = cli.default_catalog()
    if catalog_path is None or not catalog_path.is_file():
        pytest.skip("record catalogue not available")
    from aiorehom.inventory import build_inventory

    catalog = json.loads(catalog_path.read_text())
    inventory = build_inventory({"interface.json": INTERFACE, "overrides.json": OVERRIDES}, catalog)
    coverage = inventory["catalog"]
    assert "PROG_OVERRIDE.PROG_GIORNO_INVERNO" not in coverage["interface_uncatalogued"]
    assert any(label.startswith("PROG_OVERRIDE.") for label in coverage["overrides_seen"])


def test_diff_masks_personal_values_and_user_names(tmp_path: Path, capsys: Any) -> None:
    """Regression: diff printed zone names, locality, SSID, MAC and UTEN user names."""

    def rows(values: tuple[str, str, str, str, str, str]) -> list[Any]:
        name, city, ssid, mac, ip, pw = values
        return [
            {"Gruppo": "ZONA", "Unita": "001", "SubUni": "", "Key": "NOME", "Valore": name},
            {"Gruppo": "REHOM", "Unita": "", "SubUni": "", "Key": "LOCALITA", "Valore": city},
            {"Gruppo": "WIFI", "Unita": "", "SubUni": "", "Key": "SSID", "Valore": ssid},
            {"Gruppo": "WEBSERVER", "Unita": "", "SubUni": "", "Key": "MacAddress", "Valore": mac},
            {"Gruppo": "ETH", "Unita": "", "SubUni": "", "Key": "IP", "Valore": ip},
            {"Gruppo": "UTEN", "Unita": "", "SubUni": "", "Key": "mario.rossi", "Valore": pw},
            {"Gruppo": "REHOM", "Unita": "", "SubUni": "", "Key": "MODO", "Valore": name[:1]},
        ]

    a = _write_capture(
        tmp_path,
        "A",
        {
            "interface.json": rows(
                (
                    "Camera di Mario",
                    "Oldtown",
                    "HomeNet",
                    "B8:27:EB:AA:BB:CC",
                    "198.51.100.8",
                    "old-password",
                )
            )
        },
    )
    b = _write_capture(
        tmp_path,
        "B",
        {
            "interface.json": rows(
                (
                    "Soffitta di Mario",
                    "Newtown",
                    "HomeNet5G",
                    "B8:27:EB:AA:BB:CD",
                    "198.51.100.9",
                    "new-password-x",
                )
            )
        },
    )
    assert cli.main(["diff", str(a), str(b)]) == 0
    text = capsys.readouterr().out
    for leaked in (
        "Mario",
        "mario",
        "Oldtown",
        "Newtown",
        "HomeNet",
        "B8:27",
        "198.51",
        "old-password",
        "new-password",
    ):
        assert leaked not in text, leaked
    assert "~ interface ZONA.001..NOME: '<personal>' (changed)" in text
    assert "~ interface ETH...IP: '<ip>' (changed)" in text
    assert "~ interface UTEN...<user>: '<redacted>' (changed)" in text
    assert "len=" not in text
    assert "~ interface REHOM...MODO: 'C' -> 'S'" in text
    assert cli.main(["diff", str(a), str(b), "--json"]) == 0
    raw = capsys.readouterr().out
    assert "Mario" not in raw and "mario" not in raw and "HomeNet" not in raw
    assert cli.main(["diff", str(a), str(b), "--show-personal"]) == 0
    shown = capsys.readouterr().out
    assert "'Camera di Mario' -> 'Soffitta di Mario'" in shown
    assert "mario" not in shown  # user names stay masked
    assert "UTEN...<user>" in shown


def test_diff_skips_stores_missing_on_one_side(tmp_path: Path, capsys: Any) -> None:
    """Regression: a store captured on one side only looked like a mass removal."""
    a = _write_capture(tmp_path, "A", {"interface.json": INTERFACE, "config.json": CONFIG})
    b = _write_capture(tmp_path, "B", {"config.json": CONFIG})
    assert cli.main(["diff", str(a), str(b)]) == 0
    text = capsys.readouterr().out
    assert "! interface: not compared (interface.json missing in B)" in text
    assert "0 difference(s)" in text
    assert "- interface" not in text
    assert cli.main(["diff", str(b), str(a), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["not_compared"] == {"interface": "interface.json missing in A"}
    assert "interface" not in data
    assert data["config"] == {"added": [], "removed": [], "changed": []}


def test_volatile_patterns_char_class_first_line(tmp_path: Path, capsys: Any) -> None:
    """Regression: a pattern file starting with '[' was parsed as JSON and diff aborted."""
    from aiorehom.inventory import load_volatile_patterns

    plain = tmp_path / "volatile.txt"
    plain.write_text("[ZP]*.TEMP_AMBIENTE\n# comment\nconfig:*LOCAL_TIME\n")
    assert load_volatile_patterns(plain) == ["[ZP]*.TEMP_AMBIENTE", "config:*LOCAL_TIME"]
    listed = tmp_path / "volatile.lst"
    listed.write_text('["a", "b"]')
    assert load_volatile_patterns(listed) == ["a", "b"]
    as_json = tmp_path / "volatile.json"
    as_json.write_text('{"not": "a list"}')
    with pytest.raises(ValueError, match="JSON list"):
        load_volatile_patterns(as_json)
    a = _write_capture(tmp_path, "A", {"interface.json": INTERFACE})
    b_rows = [dict(r) for r in INTERFACE]
    for row in b_rows:
        if (row["Gruppo"], row["Unita"], row["Key"]) == ("ZONA", "002", "TEMP_AMBIENTE"):
            row["Valore"] = "25.0"
    b = _write_capture(tmp_path, "B", {"interface.json": b_rows})
    plain.write_text("[ZP]*.TEMP_AMBIENTE\n")
    assert cli.main(["diff", str(a), str(b), "--ignore-volatile", str(plain)]) == 0
    assert "0 difference(s)" in capsys.readouterr().out


def test_default_locations_are_not_hard_coded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Regression: the default capture root was an absolute path in the author's home."""
    monkeypatch.delenv("REHOM_PROBE_CAPTURE_ROOT", raising=False)
    monkeypatch.delenv("REHOM_PROBE_CATALOG", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.default_capture_root() == elsewhere.resolve() / "captures"
    assert cli.default_catalog() is None
    monkeypatch.setenv("REHOM_PROBE_CAPTURE_ROOT", str(tmp_path / "env"))
    monkeypatch.setenv("REHOM_PROBE_CATALOG", str(tmp_path / "cat.json"))
    assert cli.default_capture_root() == tmp_path / "env"
    assert cli.default_catalog() == tmp_path / "cat.json"
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    assert "/Users/" not in help_text
    assert not hasattr(cli, "PROJECT_ROOT")
    source = Path(cli.__file__).read_text()
    assert "/Users/" not in source
