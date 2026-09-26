"""Probe tooling regression tests: unit ids, secrets by presence only, fixture salts."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from aiorehom import cli
from aiorehom.inventory import (
    build_inventory,
    diff_captures,
    format_diff,
    load_capture,
    mask_diff,
    secret_presence,
)

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"


def _record(gruppo: str, key: str, value: str) -> dict[str, str]:
    return {
        "Gruppo": gruppo,
        "Unita": "",
        "SubUni": "",
        "Key": key,
        "Valore": value,
        "path": f"{gruppo}...{key}",
    }


def _with_secrets(directory: Path) -> Path:
    """A copy of the fixture plus synthetic secret records and one secret config field."""
    shutil.copytree(FIXTURE, directory)
    rows = json.loads((directory / "interface.json").read_text())
    rows += [_record("X", "API_TOKEN", "secret-value-1"), _record("X", "PASSWORD", "pw-0")]
    (directory / "interface.json").write_text(json.dumps(rows))
    config = json.loads((directory / "config.json").read_text())
    config["API_KEY"] = "key-0123"
    (directory / "config.json").write_text(json.dumps(config))
    return directory


# ---------------------------------------------------------------------------
# inventory: unit ids
# ---------------------------------------------------------------------------


def test_stray_deum_rows_are_not_vmcs() -> None:
    inventory = build_inventory(load_capture(FIXTURE))
    assert inventory["vmcs_flagged_present"] == 2
    assert inventory["zones_flagged_present"] == 6
    assert inventory["stray_units"] == {"ZONA": [], "DEUM": ["000", "1", "2"]}
    by_id = {vmc["id"]: vmc for vmc in inventory["vmcs"]}
    for stray in ("000", "1", "2"):
        assert (by_id[stray]["present"], by_id[stray]["online"]) == (None, None)
    assert (by_id["001"]["present"], by_id["002"]["present"]) == (True, True)
    assert by_id["003"]["present"] is False  # canonical id, PRESENZA_DEUM "1,1,0"


def test_stray_zone_ids(capsys: pytest.CaptureFixture[str]) -> None:
    capture = load_capture(FIXTURE)
    capture["interface.json"] = [
        *capture["interface.json"],
        {"Gruppo": "ZONA", "Unita": "1", "SubUni": "", "Key": "NOME", "Valore": "x"},
        {"Gruppo": "ZONA", "Unita": "025", "SubUni": "", "Key": "NOME", "Valore": "y"},
        {"Gruppo": "ZONA", "Unita": "000", "SubUni": "2", "Key": "ICO_IMG", "Valore": "sun"},
    ]
    inventory = build_inventory(capture)
    assert inventory["stray_units"]["ZONA"] == ["000", "025", "1"]
    assert inventory["zones_flagged_present"] == 6
    zones = {zone["id"]: zone for zone in inventory["zones"]}
    assert zones["1"]["present"] is None
    assert zones["025"]["online"] is None
    assert "000" not in zones  # the master pseudo-zone is never listed as a zone
    assert cli.main(["inventory", str(FIXTURE), "--catalog", "none.json"]) == 0
    text = capsys.readouterr().out
    assert "VMC units (6 with rows or flagged; 2 flagged present in PRESENZA_DEUM):" in text
    assert "Stray unit rows (not a unit id of the PRESENZA vectors; never a unit): " in text
    assert "ZONA: -; DEUM: 000, 1, 2" in text


# ---------------------------------------------------------------------------
# secrets by presence only; markers never printed with a length
# ---------------------------------------------------------------------------


def test_inventory_prints_presence_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capture = _with_secrets(tmp_path / "capture")
    assert cli.main(["inventory", str(capture), "--catalog", "none.json"]) == 0
    text = capsys.readouterr().out
    assert "len=" not in text
    assert "X.API_TOKEN = present" in text
    assert "config.json:API_KEY = present" in text
    assert cli.main(["inventory", str(capture), "--catalog", "none.json", "--json"]) == 0
    out = capsys.readouterr().out
    assert "len=" not in out
    data = json.loads(out)
    assert data["secrets"]
    for secret in data["secrets"]:
        assert "value" not in secret
        assert secret["presence"] == "present"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("<redacted len=12>", "present"),
        ("<redacted len=0>", "empty"),
        ("<redacted null>", "null"),
        ("<redacted int len=4>", "int"),
        ("<redacted list>", "list"),
        ("", "empty"),
        (None, "empty"),
        ("not-a-marker", "present"),
    ],
)
def test_secret_presence(value: Any, expected: str) -> None:
    assert secret_presence(value) == expected


def test_diff_hides_marker_lengths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _with_secrets(tmp_path / "a")
    b = _with_secrets(tmp_path / "b")
    rows = json.loads((b / "interface.json").read_text())
    for row in rows:
        if row["Key"] == "API_TOKEN":
            row["Valore"] = "a-much-longer-new-secret"  # re-redacted on load
        if row["Key"] == "PASSWORD":
            row["Impostazione"] = "<redacted len=3>"
    rows.append(_record("X", "NEW_TOKEN", "tok"))
    (b / "interface.json").write_text(json.dumps(rows))
    for flags in ([], ["--show-personal"], ["--json"]):
        assert cli.main(["diff", str(a), str(b), *flags]) == 0
        out = capsys.readouterr().out
        assert "len=" not in out, flags
        assert "a-much-longer" not in out
    assert cli.main(["diff", str(a), str(b)]) == 0
    text = capsys.readouterr().out
    assert "~ interface X...API_TOKEN: '<redacted>' (changed)" in text
    assert "+ interface X...NEW_TOKEN = '<redacted>'" in text
    # format_diff also hides markers of a diff that was never masked
    raw = format_diff(diff_captures(a, b))
    assert "len=" not in raw
    assert "(changed)" in raw
    removed = format_diff(mask_diff(diff_captures(b, a), personal=False))
    assert "- interface X...NEW_TOKEN (was '<redacted>')" in removed


# ---------------------------------------------------------------------------
# reproducible sanitising needs an explicit salt
# ---------------------------------------------------------------------------


def test_salt_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("REHOM_FIXTURE_SALT", raising=False)
    out = tmp_path / "out"
    assert cli.main(["sanitize-fixtures", str(FIXTURE), str(out)]) == 2
    assert "a salt is required" in capsys.readouterr().err
    assert not out.exists()
    monkeypatch.setenv("REHOM_FIXTURE_SALT", "env-salt")
    assert cli.main(["sanitize-fixtures", str(FIXTURE), str(out)]) == 0
    first = (out / "interface.json").read_text()
    out2 = tmp_path / "out2"
    assert cli.main(["sanitize-fixtures", str(FIXTURE), str(out2), "--salt", "env-salt"]) == 0
    assert (out2 / "interface.json").read_text() == first  # reproducible
    with pytest.raises(SystemExit) as exc:
        cli.main(["sanitize-fixtures", str(FIXTURE), str(out2), "--salt", "s", "--random-salt"])
    assert exc.value.code == 2
