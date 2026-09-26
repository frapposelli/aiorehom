"""Hygiene of the sanitised live fixture.

The fixture is sanitised (redaction + pseudonymisation) and was re-scanned
against the raw capture when it was copied; these checks keep it that way.
The owner still reviews fixtures before any public push (see its README).
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any

import pytest

from aiorehom.redact import is_redacted, is_secret_key, is_secret_record, redact_capture_file

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"
ALLOWED_FILES = {
    "alive.json",
    "config.json",
    "interface.json",
    "overrides.json",
    "plant_conf.json",
    "ws.jsonl",
    "SANITISED.txt",
    "README.md",
}
DOTTED_QUAD = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
#: Markers the fixture may hold: a fixed length, or presence-only forms.
FIXTURE_MARKERS = frozenset(
    {"<redacted len=8>", "<redacted len=0>", "<redacted null>", "<redacted bool>"}
)


def load(name: str) -> Any:
    text = (FIXTURE / name).read_text(encoding="utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def strings(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [s for key, value in obj.items() for s in (str(key), *strings(value))]
    if isinstance(obj, list):
        return [s for value in obj for s in strings(value)]
    return [obj] if isinstance(obj, str) else []


def interface() -> dict[tuple[str, str, str, str], str]:
    return {
        (r["Gruppo"], r["Unita"], r["SubUni"], r["Key"]): r["Valore"]
        for r in load("interface.json")
    }


def test_only_the_allowlisted_files() -> None:
    assert {path.name for path in FIXTURE.iterdir()} == ALLOWED_FILES
    readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
    assert "Reviewed by the maintainer before publication" in readme


def test_no_secret_records() -> None:
    """Secret-only records and fields were removed from the fixture, not just redacted."""
    assert not [key for key in interface() if is_secret_record(key[0], key[3])]
    assert not [key for key in load("config.json") if is_secret_key(key)]


def test_markers_carry_no_secret_length() -> None:
    markers = {
        text
        for name in ("alive.json", "config.json", "interface.json", "overrides.json", "ws.jsonl")
        for text in strings(load(name))
        if is_redacted(text)
    }
    assert markers <= FIXTURE_MARKERS


@pytest.mark.parametrize(
    "name",
    [
        "alive.json",
        "config.json",
        "interface.json",
        "overrides.json",
        "plant_conf.json",
        "ws.jsonl",
    ],
)
def test_nothing_new_to_redact(name: str) -> None:
    _content, count = redact_capture_file(name, load(name))
    assert count == 0


def test_pseudonymised_identity() -> None:
    rows = interface()
    assert rows[("WEBSERVER", "", "", "MacAddress")].startswith("02:00:00:")
    assert load("config.json")["INSTALLATION_ID"].startswith("020000")
    assert rows[("REHOM", "", "", "LOCALITA")] == "City"
    assert rows[("METEO", "", "", "POSIZIONE")] == "45.0000000,10.0000000"
    names = {key: value for key, value in rows.items() if key[3] == "NOME"}
    for (gruppo, unita, _subuni, _key), value in names.items():
        if gruppo == "ZONA":
            assert re.fullmatch(r"Zona \d{3}", value) and value.endswith(unita)
        elif gruppo == "DEUM":
            assert re.fullmatch(r"VMC \d{3}", value) and value.endswith(unita)


def test_ws_positions_and_names() -> None:
    for line in load("ws.jsonl"):
        frame = line.get("frame")
        if not isinstance(frame, dict):
            continue
        path = str(frame.get("path", ""))
        if path.endswith(".POSIZIONE") and frame.get("type") == "update":
            assert frame["value"] == "45.0000000,10.0000000"
        if path.endswith(".NOME"):
            assert re.fullmatch(r"(Zona|VMC) \d{3}", str(frame.get("value")))
        if frame.get("gruppo") == "METEO_DATA":
            item = json.loads(frame["value"])
            assert not {"coord", "city", "name", "lat", "lon"} & set(item)
            # ground-level and sea-level pressure together give the site's elevation
            assert not {"grnd_level", "sea_level"} & set(item.get("main", {}))


def test_addresses_are_documentation_addresses() -> None:
    texts = [
        text
        for name in ("alive.json", "config.json", "interface.json", "plant_conf.json", "ws.jsonl")
        for text in strings(load(name))
    ]
    documentation = ipaddress.IPv4Network("192.0.2.0/24")
    quads = {match for text in texts for match in DOTTED_QUAD.findall(text)}
    assert quads  # the controller's own address, pseudonymised
    for quad in quads:
        assert ipaddress.IPv4Address(quad) in documentation, quad
    joined = "\n".join(texts)
    assert re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", joined) is None
    assert re.search(r"\b[\w-]+\.(?:local|lan|home|internal)\b", joined, re.IGNORECASE) is None


def test_ws_monotonic_clock_is_rebased() -> None:
    lines = load("ws.jsonl")
    assert lines[0]["mono"] == 0.0
    assert lines[0]["event"]["event"] == "connect"
    assert lines[-1]["event"]["event"] == "disconnect"
    assert sum(1 for line in lines if "frame" in line) == 563
