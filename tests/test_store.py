"""RecordStore normalisation, WS application and diff."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from aiorehom.store import (
    RecordStore,
    diff,
    display_path,
    make_path,
    norm,
    normalise_row,
    record_key,
    split_path,
)

from .conftest import INTERFACE, OVERRIDES


def test_norm_never_falsy_or() -> None:
    assert norm(None) == ""
    assert norm(0) == "0"
    assert norm("") == ""
    assert norm(1) == "1"
    assert norm(21.5) == "21.5"


def test_record_key_and_path() -> None:
    row = {"Gruppo": "PROG", "Unita": "001", "SubUni": 0, "Key": "PROG_SETT_INVERNO"}
    assert record_key(row) == ("PROG", "001", "0", "PROG_SETT_INVERNO")
    assert make_path("PROG", "001", 0, "PROG_SETT_INVERNO") == "PROG.001.0.PROG_SETT_INVERNO"
    assert make_path("REHOM", None, None, "STAGIONE") == "REHOM...STAGIONE"
    assert record_key({}) == ("", "", "", "")


def test_split_path() -> None:
    assert split_path("ZONA.001..NOME") == ("ZONA", "001", "", "NOME")
    assert split_path("A.B.C") is None
    assert split_path("A.B.C.D.E") is None
    assert split_path(None) is None
    # UTEN keys are user names and may contain dots
    assert split_path("UTEN...mario.rossi") == ("UTEN", "", "", "mario.rossi")
    assert split_path("UTEN...a.b@example.it") == ("UTEN", "", "", "a.b@example.it")
    assert split_path("UTEN.x") is None
    assert display_path(("UTEN", "", "", "mario.rossi")) == "UTEN...<user>"
    assert display_path(("UTEN", "", "", "admin")) == "UTEN...admin"
    assert display_path(("ZONA", "001", "", "NOME")) == "ZONA.001..NOME"


def test_normalise_row_keeps_received_path_when_different() -> None:
    row = {
        "Gruppo": "PROG",
        "Unita": "001",
        "SubUni": 0,
        "Key": "K",
        "Valore": 5,
        "path": "PROG.001..K",
    }
    out = normalise_row(row)
    assert out["SubUni"] == "0"
    assert out["Valore"] == "5"
    assert out["path"] == "PROG.001.0.K"
    assert out["path_received"] == "PROG.001..K"
    same = normalise_row({"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "K", "path": "G...K"})
    assert "path_received" not in same
    assert same["Valore"] == ""


def test_store_load_normalises(interface_rows: list[dict[str, Any]]) -> None:
    store = RecordStore(interface_rows)
    assert ("PROG", "002", "0", "PROG_SETT_INVERNO") in store
    assert ("PROG", "002", "", "PROG_SETT_INVERNO") not in store
    assert store.value("REHOM", "", "", "MODO") == "2"
    assert store.value("REHOM", "", "", "MISSING") is None
    assert store.get(("X", "", "", "Y")) is None
    rows = store.to_rows()
    assert rows == sorted(rows, key=record_key)
    assert len(store) == len(rows) == len(list(store))
    assert store.keys()[0] == record_key(rows[0])
    store.load([{"Gruppo": "A", "Key": "B"}, "junk"])  # type: ignore[list-item]
    assert len(store) == 1


def test_duplicates_last_wins() -> None:
    store = RecordStore(
        [
            {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "K", "Valore": "1"},
            {"Gruppo": "G", "Unita": None, "SubUni": None, "Key": "K", "Valore": "2"},
            {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "K", "Valore": "3"},
            {"Gruppo": "UTEN", "Unita": "", "SubUni": "", "Key": "bob", "Valore": "x"},
            {"Gruppo": "UTEN", "Unita": "", "SubUni": "", "Key": "bob", "Valore": "y"},
            {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "OTHER", "Valore": "1"},
        ]
    )
    assert len(store) == 3
    assert store.raw_rows == 6
    assert store.value("G", "", "", "K") == "3"
    # duplicates are reported (user names masked), not silently collapsed
    assert store.duplicates == {"G...K": 3, "UTEN...<user>": 2}
    assert RecordStore([{"Gruppo": "G", "Key": "K"}]).duplicates == {}


def test_apply_ws() -> None:
    store = RecordStore(
        [{"Gruppo": "ZONA", "Unita": "001", "SubUni": "", "Key": "T", "Valore": "20"}]
    )
    assert store.apply_ws(
        {"domain": "termo", "type": "update", "path": "ZONA.001..T", "value": "21"}
    )
    assert store.value("ZONA", "001", "", "T") == "21"
    assert not store.apply_ws(
        {"domain": "termo", "type": "update", "path": "ZONA.001..T", "value": "21"}
    )
    assert store.apply_ws({"domain": "termo", "type": "update", "path": "PROG.001.0.K", "value": 3})
    assert store.value("PROG", "001", "0", "K") == "3"
    assert store.get(("PROG", "001", "0", "K"))["path"] == "PROG.001.0.K"  # type: ignore[index]
    assert store.apply_ws({"domain": "termo", "type": "remove", "path": "PROG.001.0.K"})
    assert not store.apply_ws({"domain": "termo", "type": "remove", "path": "PROG.001.0.K"})
    assert not store.apply_ws(
        {"domain": "termo", "type": "update", "path": "BAD.PATH", "value": "x"}
    )
    assert not store.apply_ws({"domain": "termo", "type": "other", "path": "ZONA.001..T"})
    assert not store.apply_ws({"domain": "bus", "type": "update", "key": "k", "value": "v"})
    override = {
        "domain": "termo",
        "type": "update",
        "gruppo": "PROG_OVERRIDE",
        "path": "PROG_OVERRIDE.001.1.PROG_GIORNO_INVERNO",
        "value": "3,3",
        "issuedAt": "a",
        "expiresAt": "b",
    }
    assert store.apply_ws(override)
    row = store.get(("PROG_OVERRIDE", "001", "1", "PROG_GIORNO_INVERNO"))
    assert row is not None
    assert (row["Impostazione"], row["Scadenza"]) == ("a", "b")
    assert not store.apply_ws(override)
    assert store.apply_ws({**override, "expiresAt": "c"})  # timestamp-only change is applied


def test_diff() -> None:
    a = [
        {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "SAME", "Valore": "1"},
        {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "CHANGED", "Valore": "1"},
        {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "GONE", "Valore": "1"},
        {"Gruppo": "O", "Unita": "", "SubUni": "", "Key": "TS", "Valore": "x", "Scadenza": "1"},
    ]
    b = [
        {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "SAME", "Valore": "1", "path": "other"},
        {"Gruppo": "G", "Unita": "", "SubUni": "", "Key": "CHANGED", "Valore": "2"},
        {"Gruppo": "G", "Unita": "", "SubUni": 0, "Key": "NEW", "Valore": "1"},
        {"Gruppo": "O", "Unita": "", "SubUni": "", "Key": "TS", "Valore": "x", "Scadenza": "2"},
    ]
    result = diff(a, RecordStore(b))
    assert [r["Key"] for r in result.added] == ["NEW"]
    assert result.added[0]["SubUni"] == "0"
    assert [r["Key"] for r in result.removed] == ["GONE"]
    changed = {c["path"]: c for c in result.changed}
    assert changed["G...CHANGED"]["old"] == "1"
    assert changed["G...CHANGED"]["new"] == "2"
    assert changed["O...TS"]["fields"] == {"Scadenza": ["1", "2"]}
    assert "G...SAME" not in changed
    assert not result.empty
    assert set(result.to_dict()) == {"added", "removed", "changed"}
    assert diff(a, a).empty


def test_apply_ws_routes_overrides_by_gruppo() -> None:
    """Regression: an override frame with a PROG.* path created a bogus PROG row."""
    overrides = RecordStore(copy.deepcopy(OVERRIDES), role="overrides")
    frame = {
        "domain": "termo",
        "type": "update",
        "gruppo": "PROG_OVERRIDE",
        "path": "PROG.002.1.PROG_GIORNO_INVERNO",
        "value": ",".join(["3"] * 48),
        "issuedAt": "2026-09-25 09:00:00",
        "expiresAt": "2026-09-25 12:00:00",
    }
    assert overrides.apply_ws(frame)
    assert ("PROG", "002", "1", "PROG_GIORNO_INVERNO") not in overrides
    row = overrides.get(("PROG_OVERRIDE", "002", "1", "PROG_GIORNO_INVERNO"))
    assert row is not None
    assert row["Scadenza"] == "2026-09-25 12:00:00"
    assert len(overrides) == 1
    # a new override row keeps the received path for evidence
    assert overrides.apply_ws({**frame, "path": "PROG.003.1.PROG_GIORNO_INVERNO"})
    new = overrides.get(("PROG_OVERRIDE", "003", "1", "PROG_GIORNO_INVERNO"))
    assert new is not None
    assert new["path"] == "PROG_OVERRIDE.003.1.PROG_GIORNO_INVERNO"
    assert new["path_received"] == "PROG.003.1.PROG_GIORNO_INVERNO"
    assert overrides.apply_ws({**frame, "type": "remove"})
    assert ("PROG_OVERRIDE", "002", "1", "PROG_GIORNO_INVERNO") not in overrides
    # an overrides store ignores interface frames ...
    zona = {
        "domain": "termo",
        "type": "update",
        "gruppo": "ZONA",
        "path": "ZONA.002..T",
        "value": "1",
    }
    assert not overrides.apply_ws(zona)
    # ... and an interface store ignores override frames
    interface = RecordStore(copy.deepcopy(INTERFACE), role="interface")
    before = interface.to_rows()
    assert not interface.apply_ws(frame)
    assert interface.to_rows() == before
    assert interface.apply_ws({**zona, "path": "ZONA.002..TEMP_AMBIENTE", "value": "22.0"})
    assert interface.value("ZONA", "002", "", "TEMP_AMBIENTE") == "22.0"
    # without gruppo, a PROG_OVERRIDE path prefix still identifies an override
    assert not interface.apply_ws(
        {"domain": "termo", "type": "update", "path": "PROG_OVERRIDE.002.1.K", "value": "1"}
    )
    with pytest.raises(ValueError, match="role"):
        RecordStore(role="other")  # type: ignore[arg-type]


def test_apply_ws_dotted_user_path() -> None:
    store = RecordStore(role="interface")
    assert store.apply_ws(
        {"domain": "termo", "type": "update", "gruppo": "UTEN", "path": "UTEN...a.b", "value": "v"}
    )
    assert store.value("UTEN", "", "", "a.b") == "v"
