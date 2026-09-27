"""Gated bulk writes on the transport: shapes, value domains, opt-in and no-I/O refusals."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from aioresponses import aioresponses

from aiorehom.exceptions import (
    ForbiddenRequestError,
    RehomAuthenticationError,
    RehomConnectionError,
)
from aiorehom.transport import (
    INTERFACE_WRITE_PATH,
    OVERRIDES_WRITE_PATH,
    WRITABLE_INTERFACE_KEYS,
    WRITE_BODY_TEMPLATES,
    ReadOnlyTransport,
    check_write_body,
)

from .conftest import BASE, TEST_HOST
from .test_transport_allowlist import no_http  # noqa: F401 - fixture

TOKEN = "tok-abcdef0123456789"


def rec(gruppo: str, unita: str, key: str, valore: object) -> dict[str, object]:
    return {
        "Gruppo": gruppo,
        "Unita": unita,
        "SubUni": "",
        "Key": key,
        "Valore": valore,
        "Stato": 1,
        "Flusso": 0,
        "path": f"{gruppo}.{unita}..{key}",
    }


def house(*pairs: tuple[str, object]) -> list[dict[str, object]]:
    return [rec("REHOM", "", key, value) for key, value in pairs]


def _digits(text: str, zero: int) -> str:
    return "".join(chr(zero + int(c)) if "0" <= c <= "9" else c for c in text)


def arabic_indic(text: str) -> str:
    """``text`` with its ASCII digits replaced by Arabic-Indic ones (``\\d`` matches them)."""
    return _digits(text, 0x0660)


def fullwidth(text: str) -> str:
    """``text`` with its ASCII digits replaced by fullwidth ones (``\\d`` matches them)."""
    return _digits(text, 0xFF10)


PROGRAM = ",".join(["1"] * 12 + ["0"] * 10 + ["1"] * 2 + ["3"] * 20 + ["0"] * 2 + ["1"] * 2)
OVERRIDE: dict[str, object] = {
    "Gruppo": "PROG_OVERRIDE",
    "Unita": "001",
    "SubUni": "1",
    "Key": "PROG_GIORNO_ESTATE",
    "Valore": PROGRAM,
    "Impostazione": "2026-06-15 12:12:15",
    "Scadenza": "2026-06-15 12:42:14",
}

# Payload shapes captured from the official client, plus the ones the
# library sends (house -> COMFORT with its SET_POINT_TEMP, see below).
CAPTURED: list[tuple[str, list[dict[str, object]]]] = [
    (INTERFACE_WRITE_PATH, house(("MODO", "1"), ("SET_POINT", "1"), ("SET_POINT_TEMP", "29"))),
    (INTERFACE_WRITE_PATH, house(("MODO", "1"), ("SET_POINT", "2"), ("SET_POINT_TEMP", "26"))),
    (INTERFACE_WRITE_PATH, house(("MODO", "1"), ("SET_POINT", "3"), ("SET_POINT_TEMP", "24"))),
    (INTERFACE_WRITE_PATH, house(("MODO", "2"), ("SET_POINT", "0"))),
    (INTERFACE_WRITE_PATH, house(("TEMP_COM", "25.9"), ("SET_POINT_TEMP", "25.9"))),
    (INTERFACE_WRITE_PATH, house(("TEMP_COM", "26"), ("SET_POINT_TEMP", "26"))),
    (INTERFACE_WRITE_PATH, house(("ALG_ATTIVO", "0"))),
    (INTERFACE_WRITE_PATH, house(("ALG_ATTIVO", "1"))),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "DELTA_SETP_CORRENTE", "1.0")]),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "DELTA_SETP_CORRENTE", "-1.0")]),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "DELTA_SETP_CORRENTE", "0.0")]),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "SETP_CORRENTE", "2")]),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "SETP_CORRENTE", "4")]),
    (INTERFACE_WRITE_PATH, [rec("ZONA", "001", "SETP_CORRENTE", "0")]),
    (INTERFACE_WRITE_PATH, [rec("DEUM", "001", "COM_VENTILA", "2")]),
    (INTERFACE_WRITE_PATH, [rec("DEUM", "001", "COM_VENTILA", 1)]),
    (INTERFACE_WRITE_PATH, [rec("DEUM", "001", "ST_MODE", "8")]),
    (INTERFACE_WRITE_PATH, [rec("DEUM", "001", "ST_MODE", "1")]),
    (OVERRIDES_WRITE_PATH, [OVERRIDE]),
]


@pytest.mark.parametrize(("path", "records"), CAPTURED)
def test_captured_shapes_are_accepted(path: str, records: list[dict[str, object]]) -> None:
    out = check_write_body(path, records)
    assert out == records
    # Byte-for-byte the same JSON: same key order, same value types.
    assert json.dumps(out) == json.dumps(records)
    assert all(o is not r for o, r in zip(out, records, strict=True))


def test_web_ui_comfort_without_setpoint_is_refused() -> None:
    # The official web UI's house -> COMFORT body omits SET_POINT_TEMP, which can
    # leave the regulated setpoint stale.  The library always carries
    # SET_POINT_TEMP, so the gate deliberately refuses that shape.
    with pytest.raises(ForbiddenRequestError, match=r"must be AUTO \(2/0\)"):
        check_write_body(INTERFACE_WRITE_PATH, house(("MODO", "1"), ("SET_POINT", "3")))


def test_templates_only_use_writable_keys() -> None:
    used = {(g, k) for g, shapes in WRITE_BODY_TEMPLATES.items() for s in shapes for k in s}
    assert used == set(WRITABLE_INTERFACE_KEYS)


def test_output_is_rebuilt_from_plain_values() -> None:
    records = [dict(reversed(list(rec("ZONA", "001", "SETP_CORRENTE", "2").items())))]
    out = check_write_body(INTERFACE_WRITE_PATH, records)
    assert list(out[0]) == ["Gruppo", "Unita", "SubUni", "Key", "Valore", "Stato", "Flusso", "path"]
    records[0]["Valore"] = "1"  # later changes to the caller's objects do not leak through
    assert out[0]["Valore"] == "2"
    override = dict(reversed(list(OVERRIDE.items())))
    out = check_write_body(OVERRIDES_WRITE_PATH, [override])
    assert list(out[0]) == list(OVERRIDE)
    assert out[0] is not override


def test_temperatures_compare_numerically() -> None:
    body = house(("TEMP_COM", "26"), ("SET_POINT_TEMP", "26.0"))
    assert check_write_body(INTERFACE_WRITE_PATH, body) == body
    body = house(("TEMP_COM", 26), ("SET_POINT_TEMP", "26"))
    assert check_write_body(INTERFACE_WRITE_PATH, body) == body
    body = house(("MODO", 1), ("SET_POINT", 3), ("SET_POINT_TEMP", 24))
    assert json.dumps(check_write_body(INTERFACE_WRITE_PATH, body)) == json.dumps(body)


def _with(record: dict[str, object], **changes: object) -> dict[str, object]:
    out = dict(record)
    for key, value in changes.items():
        if value is ...:
            out.pop(key)
        else:
            out[key] = value
    return out


class _LyingStr(str):
    """Validates as one string, serialises as another (json encodes the str data)."""

    __slots__ = ()

    def __str__(self) -> str:
        return "ZONA"


class _LyingInt(int):
    def __str__(self) -> str:
        return "2"


class _AlwaysEqualStr(str):
    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    __hash__ = str.__hash__


BASE_REC = rec("ZONA", "001", "SETP_CORRENTE", "2")
BAD_INTERFACE = {
    "not writable: service stop": rec("REHOM", "", "STOP", "1"),
    "not writable: account": rec("UTEN", "001", "admin", "1"),
    "not writable: wifi": rec("WIFI", "001", "PASSWORD", "1"),
    "not writable: season": rec("REHOM", "", "STAGIONE", "1"),
    "not writable: mastership": rec("REHOM", "", "SERVER_ON", "1"),
    "not writable: zone name": rec("ZONA", "001", "NOME", "1"),
    "extra field": _with(BASE_REC, Extra="x"),
    "missing field": _with(BASE_REC, Flusso=...),
    "SubUni set": _with(BASE_REC, SubUni="1", path="ZONA.001.1.SETP_CORRENTE"),
    "path mismatch": _with(BASE_REC, path="ZONA.002..SETP_CORRENTE"),
    "Stato wrong": _with(BASE_REC, Stato=0),
    "Stato bool": _with(BASE_REC, Stato=True),
    "Stato float": _with(BASE_REC, Stato=1.0),
    "Stato Decimal": _with(BASE_REC, Stato=Decimal(1)),
    "Stato str": _with(BASE_REC, Stato="1"),
    "Flusso wrong": _with(BASE_REC, Flusso=1),
    "Flusso bool": _with(BASE_REC, Flusso=False),
    "Flusso float": _with(BASE_REC, Flusso=0.0),
    "Unita malformed": rec("ZONA", "1", "SETP_CORRENTE", "2"),
    "Unita master": rec("ZONA", "000", "SETP_CORRENTE", "2"),
    "Unita VMC master": rec("DEUM", "000", "ST_MODE", "8"),
    "REHOM with unit": rec("REHOM", "001", "ALG_ATTIVO", "0"),
    "zone without unit": rec("ZONA", "", "SETP_CORRENTE", "2"),
    "Valore bool": _with(BASE_REC, Valore=True),
    "Valore float": _with(BASE_REC, Valore=2.0),
    "Valore Decimal": _with(BASE_REC, Valore=Decimal(2)),
    "Valore None": _with(BASE_REC, Valore=None),
    "Valore text": _with(BASE_REC, Valore="abc"),
    "Valore long": _with(BASE_REC, Valore="12345"),
    "Valore huge int": _with(BASE_REC, Valore=10**5000),
    "Valore two decimals": _with(BASE_REC, Valore="1.25"),
    "Valore padded": _with(BASE_REC, Valore=" 2"),
    "Valore leading zero": _with(BASE_REC, Valore="02"),
    "Valore decimal on int key": _with(BASE_REC, Valore="2.0"),
    "Gruppo not str": _with(BASE_REC, Gruppo=1),
    # value domains (single-record shapes)
    "zone OFF": rec("ZONA", "001", "SETP_CORRENTE", "1"),
    "zone PROBE_OFF": rec("ZONA", "001", "SETP_CORRENTE", "5"),
    "zone level int out of range": rec("ZONA", "001", "SETP_CORRENTE", 6),
    "offset 4": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "4.0"),
    "offset -4": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "-4.0"),
    "offset 99.9": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "99.9"),
    "offset half degree": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "1.5"),
    "offset without decimal": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "1"),
    "offset as int": rec("ZONA", "001", "DELTA_SETP_CORRENTE", 1),
    "offset plus sign": rec("ZONA", "001", "DELTA_SETP_CORRENTE", "+1.0"),
    "VMC rapid renewal": rec("DEUM", "001", "ST_MODE", "6"),
    "VMC rapid heat": rec("DEUM", "001", "ST_MODE", 7),
    "VMC mode 9": rec("DEUM", "001", "ST_MODE", "9"),
    "VMC mode 999": rec("DEUM", "001", "ST_MODE", "999"),
    "fan negative": rec("DEUM", "001", "COM_VENTILA", "-5"),
    "fan 101": rec("DEUM", "001", "COM_VENTILA", "101"),
    "fan int 101": rec("DEUM", "001", "COM_VENTILA", 101),
    "fan leading zeros": rec("DEUM", "001", "COM_VENTILA", "007"),
    "predictive 7": rec("REHOM", "", "ALG_ATTIVO", "7"),
    "predictive 2": rec("REHOM", "", "ALG_ATTIVO", 2),
    "predictive -0": rec("REHOM", "", "ALG_ATTIVO", "-0"),
    # non-ASCII digits (\d would match them)
    "Valore Arabic-Indic": rec("ZONA", "001", "SETP_CORRENTE", arabic_indic("2")),
    "Valore fullwidth": rec("DEUM", "001", "ST_MODE", fullwidth("8")),
    "Unita Arabic-Indic": rec("ZONA", arabic_indic("001"), "SETP_CORRENTE", "2"),
    "Unita fullwidth": rec("ZONA", fullwidth("001"), "SETP_CORRENTE", "2"),
    # str/int subclasses: what is checked must be what is sent
    "Gruppo lying str subclass": _with(BASE_REC, Gruppo=_LyingStr("UTEN")),
    "Gruppo plain str subclass": _with(BASE_REC, Gruppo=_AlwaysEqualStr("ZONA")),
    "Key str subclass": _with(BASE_REC, Key=_AlwaysEqualStr("SETP_CORRENTE")),
    "path always-equal": _with(BASE_REC, path=_AlwaysEqualStr("UTEN.001..admin")),
    "Valore lying int subclass": rec("REHOM", "", "ALG_ATTIVO", _LyingInt(7)),
    "Valore str subclass": _with(BASE_REC, Valore=_AlwaysEqualStr("2")),
    "Stato int subclass": _with(BASE_REC, Stato=_LyingInt(1)),
    "field name str subclass": {_AlwaysEqualStr(k): v for k, v in BASE_REC.items()},
}


@pytest.mark.parametrize("name", sorted(BAD_INTERFACE))
def test_bad_interface_records_are_refused(name: str) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_write_body(INTERFACE_WRITE_PATH, [BAD_INTERFACE[name]])


T26 = ("SET_POINT_TEMP", "26")
BAD_INTERFACE_BODIES: dict[str, list[dict[str, object]]] = {
    "lone MODO": house(("MODO", "2")),
    "lone SET_POINT": house(("SET_POINT", "0")),
    "lone SET_POINT_TEMP": house(T26),
    "lone TEMP_COM": house(("TEMP_COM", "26")),
    "house OFF": house(("MODO", "0"), ("SET_POINT", "0")),
    "house OFF with level": house(("MODO", "0"), ("SET_POINT", "3"), T26),
    "MODO 007": house(("MODO", "007"), ("SET_POINT", "0")),
    "MODO -0": house(("MODO", "-0"), ("SET_POINT", "0")),
    "MODO int -1": house(("MODO", -1), ("SET_POINT", "0")),
    "MODO fullwidth": house(("MODO", fullwidth("2")), ("SET_POINT", "0")),
    "AUTO with a level": house(("MODO", "2"), ("SET_POINT", "1")),
    "AUTO with a temperature": house(("MODO", "2"), ("SET_POINT", "0"), T26),
    "MANUAL without a level": house(("MODO", "1"), ("SET_POINT", "0"), T26),
    "MANUAL level 4": house(("MODO", "1"), ("SET_POINT", "4"), T26),
    "keys out of order": house(("SET_POINT", "0"), ("MODO", "2")),
    "temperature first": house(T26, ("TEMP_COM", "26")),
    "duplicate MODO": house(("MODO", "1"), ("MODO", "2")),
    "duplicate zone record": [BASE_REC, rec("ZONA", "001", "SETP_CORRENTE", "4")],
    "zone mode and offset": [BASE_REC, rec("ZONA", "001", "DELTA_SETP_CORRENTE", "1.0")],
    "two zones": [BASE_REC, rec("ZONA", "002", "SETP_CORRENTE", "2")],
    "two VMCs": [rec("DEUM", "001", "ST_MODE", "8"), rec("DEUM", "002", "ST_MODE", "8")],
    "mixed groups": [rec("REHOM", "", "ALG_ATTIVO", "0"), BASE_REC],
    "comfort temperatures differ": house(("TEMP_COM", "25.9"), ("SET_POINT_TEMP", "26")),
    "temperature -99.9": house(("TEMP_COM", "-99.9"), ("SET_POINT_TEMP", "-99.9")),
    "temperature 999": house(("MODO", "1"), ("SET_POINT", "3"), ("SET_POINT_TEMP", "999")),
    "temperature 0": house(("TEMP_COM", "0"), ("SET_POINT_TEMP", "0")),
    "temperature 4.9": house(("TEMP_COM", "4.9"), ("SET_POINT_TEMP", "4.9")),
    "temperature 40.1": house(("TEMP_COM", "40.1"), ("SET_POINT_TEMP", "40.1")),
    "temperature int 41": house(("TEMP_COM", 41), ("SET_POINT_TEMP", 41)),
    "temperature two decimals": house(("TEMP_COM", "25.95"), ("SET_POINT_TEMP", "25.95")),
    "temperature leading zero": house(("TEMP_COM", "026"), ("SET_POINT_TEMP", "026")),
    "temperature exponent": house(("TEMP_COM", "2e1"), ("SET_POINT_TEMP", "2e1")),
    "temperature Arabic-Indic": house(
        ("TEMP_COM", arabic_indic("26")), ("SET_POINT_TEMP", arabic_indic("26"))
    ),
    "four records": [rec("DEUM", "001", "ST_MODE", "8")] * 4,
}


@pytest.mark.parametrize("name", sorted(BAD_INTERFACE_BODIES))
def test_bad_interface_bodies_are_refused(name: str) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_write_body(INTERFACE_WRITE_PATH, BAD_INTERFACE_BODIES[name])


def test_lying_subclass_would_have_sent_another_group() -> None:
    # What the old gate let through: validated as ZONA, serialised as UTEN.
    sneaky = _LyingStr("UTEN")
    assert str(sneaky) == "ZONA" and json.dumps(sneaky) == '"UTEN"'
    with pytest.raises(ForbiddenRequestError, match="Gruppo must be a string"):
        check_write_body(INTERFACE_WRITE_PATH, [_with(BASE_REC, Gruppo=sneaky)])


BAD_OVERRIDE = {
    "wrong group": _with(OVERRIDE, Gruppo="PROG"),
    "wrong key": _with(OVERRIDE, Key="PROG_SETT_ESTATE"),
    "short program": _with(OVERRIDE, Valore="3,3,3"),
    "level out of range": _with(OVERRIDE, Valore=PROGRAM[:-1] + "4"),
    "program not a string": _with(OVERRIDE, Valore=PROGRAM.split(",")),
    "program str subclass": _with(OVERRIDE, Valore=_AlwaysEqualStr(PROGRAM)),
    "bad time": _with(OVERRIDE, Scadenza="2026-06-15T12:42:14"),
    "time not a string": _with(OVERRIDE, Scadenza=None),
    "time str subclass": _with(OVERRIDE, Scadenza=_AlwaysEqualStr("2026-06-15 12:42:14")),
    "impossible time": _with(OVERRIDE, Scadenza="2026-13-45 99:99:99"),
    "impossible date": _with(OVERRIDE, Impostazione="2026-02-30 12:12:15"),
    "Arabic-Indic year": _with(
        OVERRIDE,
        Impostazione=arabic_indic("2026") + "-06-15 12:12:15",
        Scadenza=arabic_indic("2026") + "-06-15 12:42:14",
    ),
    "all digits Arabic-Indic": _with(
        OVERRIDE,
        Scadenza=arabic_indic("2026-06-15 12:42:14"),
    ),
    "negative window": _with(OVERRIDE, Scadenza="2026-06-15 12:00:00"),
    "empty window": _with(OVERRIDE, Scadenza="2026-06-15 12:12:15"),
    "window > 24 h": _with(OVERRIDE, Scadenza="2026-06-16 12:42:15"),
    "crosses midnight": _with(
        OVERRIDE, Impostazione="2026-06-15 23:30:00", Scadenza="2026-06-16 00:29:59"
    ),
    "interface fields": _with(OVERRIDE, Stato=1),
    "missing field": _with(OVERRIDE, Scadenza=...),
    "bad unit": _with(OVERRIDE, Unita="01"),
    "master unit": _with(OVERRIDE, Unita="000"),
    "unit Arabic-Indic": _with(OVERRIDE, Unita=arabic_indic("001")),
    "bad preset": _with(OVERRIDE, SubUni="abc"),
    "preset 0": _with(OVERRIDE, SubUni="0"),
    "preset leading zero": _with(OVERRIDE, SubUni="01"),
    "preset three digits": _with(OVERRIDE, SubUni="100"),
    "preset Arabic-Indic": _with(OVERRIDE, SubUni=arabic_indic("1")),
    "preset int": _with(OVERRIDE, SubUni=1),
}


@pytest.mark.parametrize("name", sorted(BAD_OVERRIDE))
def test_bad_override_records_are_refused(name: str) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_write_body(OVERRIDES_WRITE_PATH, [BAD_OVERRIDE[name]])


def test_override_body_carries_exactly_one_record() -> None:
    other = _with(OVERRIDE, Unita="002")
    with pytest.raises(ForbiddenRequestError, match=r"1\.\.1 records"):
        check_write_body(OVERRIDES_WRITE_PATH, [OVERRIDE, other])
    with pytest.raises(ForbiddenRequestError):
        check_write_body(OVERRIDES_WRITE_PATH, [OVERRIDE, OVERRIDE])


@pytest.mark.parametrize(
    "body",
    [[], [BASE_REC] * 2, [BASE_REC] * 4, [BASE_REC] * 9, "x", b"x", {"a": 1}, [1], None],
)
def test_bad_bodies_are_refused(body: Any) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_write_body(INTERFACE_WRITE_PATH, body)


def test_records_cannot_cross_paths() -> None:
    with pytest.raises(ForbiddenRequestError):
        check_write_body(OVERRIDES_WRITE_PATH, [BASE_REC])
    with pytest.raises(ForbiddenRequestError):
        check_write_body(INTERFACE_WRITE_PATH, [OVERRIDE])
    with pytest.raises(ForbiddenRequestError):
        check_write_body("/api/interface/bulk_delete/", [BASE_REC])


# ---------------------------------------------------------------------------
# The opt-in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["false", "no", "true", "", 1, 0, 1.0, None, [True]])
def test_allow_writes_must_be_a_real_bool(no_http: Any, flag: Any) -> None:  # noqa: F811
    with pytest.raises(TypeError, match="allow_writes must be a bool"):
        ReadOnlyTransport(TEST_HOST, allow_login=True, allow_writes=flag)
    no_http[0].assert_not_called()


def test_allow_writes_accepts_bools() -> None:
    assert ReadOnlyTransport(TEST_HOST, allow_writes=True).allow_writes is True
    assert ReadOnlyTransport(TEST_HOST, allow_writes=False).allow_writes is False
    assert ReadOnlyTransport(TEST_HOST).allow_writes is False


async def test_default_transport_refuses_writes_without_io(no_http: Any) -> None:  # noqa: F811
    transport = ReadOnlyTransport(TEST_HOST)
    try:
        with pytest.raises(ForbiddenRequestError, match="writes not enabled"):
            await transport.post_bulk_update(INTERFACE_WRITE_PATH, [BASE_REC])
        with pytest.raises(ForbiddenRequestError, match="writes not enabled"):
            await transport.post_bulk_update(OVERRIDES_WRITE_PATH, [OVERRIDE])
        assert transport.request_log == []
    finally:
        await transport.close()


async def test_write_needs_a_token_without_io(no_http: Any) -> None:  # noqa: F811
    transport = ReadOnlyTransport(TEST_HOST, allow_writes=True)
    try:
        with pytest.raises(RehomAuthenticationError):
            await transport.post_bulk_update(INTERFACE_WRITE_PATH, [BASE_REC])
        assert transport.request_log == []
    finally:
        await transport.close()


async def test_bad_body_refused_before_io_even_when_enabled(no_http: Any) -> None:  # noqa: F811
    transport = ReadOnlyTransport(TEST_HOST, allow_writes=True)
    t: Any = transport
    try:
        with pytest.raises(ForbiddenRequestError):
            await transport.post_bulk_update(INTERFACE_WRITE_PATH, [rec("REHOM", "", "STOP", "1")])
        with pytest.raises(ForbiddenRequestError):
            await transport.post_bulk_update(
                INTERFACE_WRITE_PATH, house(("MODO", "0"), ("SET_POINT", "0"))
            )
        with pytest.raises(ForbiddenRequestError):
            await t._request(
                "POST", INTERFACE_WRITE_PATH, write_body=[rec("UTEN", "001", "x", "1")]
            )
        with pytest.raises(ForbiddenRequestError):
            await t._request("GET", "/api/alive/", write_body=[BASE_REC])
        with pytest.raises(ForbiddenRequestError):
            await t._request("POST", INTERFACE_WRITE_PATH)
    finally:
        await transport.close()


@pytest.fixture
def mocked() -> Iterator[aioresponses]:
    with aioresponses() as m:
        yield m


def _posts(mocked: aioresponses, path: str) -> list[Any]:
    return [
        call
        for (method, url), items in mocked.requests.items()
        if method == "POST" and url.path == path
        for call in items
    ]


async def test_enabled_write_sends_exact_body_once(mocked: aioresponses) -> None:
    mocked.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    mocked.post(f"{BASE}{INTERFACE_WRITE_PATH}", status=204)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, allow_login=True, allow_writes=True)
    await transport.login("user", "pw")
    records = CAPTURED[0][1]
    assert await transport.post_bulk_update(INTERFACE_WRITE_PATH, records) == 204
    await transport.close()
    calls = _posts(mocked, INTERFACE_WRITE_PATH)
    assert len(calls) == 1
    assert calls[0].kwargs["json"] == records
    assert json.dumps(calls[0].kwargs["json"]) == json.dumps(records)
    assert all(
        sent is not given for sent, given in zip(calls[0].kwargs["json"], records, strict=True)
    )
    assert calls[0].kwargs["headers"]["Authorization"] == f"Token {TOKEN}"
    entry = transport.request_log[-1]
    assert entry["method"] == "POST" and entry["path"] == INTERFACE_WRITE_PATH
    assert entry["query"] is None and entry["status"] == 204
    assert set(entry) == {
        "method",
        "path",
        "query_keys",
        "query",
        "status",
        "latency_ms",
        "bytes",
        "ok",
        "error",
        "mono",
    }
    assert "Valore" not in json.dumps(entry) and "SET_POINT_TEMP" not in json.dumps(entry)


async def test_failed_write_is_not_retried(mocked: aioresponses) -> None:
    mocked.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    mocked.post(f"{BASE}{OVERRIDES_WRITE_PATH}", status=500)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, allow_login=True, allow_writes=True)
    await transport.login("user", "pw")
    with pytest.raises(Exception, match="HTTP 500"):
        await transport.post_bulk_update(OVERRIDES_WRITE_PATH, [OVERRIDE])
    await transport.close()
    assert len(_posts(mocked, OVERRIDES_WRITE_PATH)) == 1


# ---------------------------------------------------------------------------
# close() stops a request still waiting for its turn
# ---------------------------------------------------------------------------


class _HeldPacing:
    """A fake clock/sleep pair: pacing blocks until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def clock(self) -> float:
        return 100.0

    async def sleep(self, seconds: float) -> None:
        assert seconds > 0
        self.entered.set()
        await self.release.wait()


@pytest.mark.parametrize("kind", ["write", "authenticated read", "read"])
async def test_close_refuses_a_request_waiting_for_its_turn(
    mocked: aioresponses, kind: str
) -> None:
    mocked.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    mocked.post(f"{BASE}{INTERFACE_WRITE_PATH}", status=204)
    mocked.get(f"{BASE}/api/interface/", payload=[])
    mocked.get(f"{BASE}/api/alive/", payload={})
    pacing = _HeldPacing()
    transport = ReadOnlyTransport(
        TEST_HOST,
        min_interval=1.0,
        allow_login=True,
        allow_writes=True,
        clock=pacing.clock,
        sleep=pacing.sleep,
    )
    t: Any = transport
    await transport.login("user", "pw")  # the first request is not paced
    if kind == "write":
        pending = asyncio.ensure_future(
            transport.post_bulk_update(INTERFACE_WRITE_PATH, [BASE_REC])
        )
    elif kind == "authenticated read":
        pending = asyncio.ensure_future(transport.get_interface())
    else:
        pending = asyncio.ensure_future(transport.get_alive())
    await pacing.entered.wait()  # queued: inside the lock, waiting for its slot
    await transport.close()
    assert t._session is None and not transport.has_token
    pacing.release.set()
    with pytest.raises(RehomConnectionError, match="transport is closed"):
        await pending
    assert t._session is None  # no new session was created
    assert _posts(mocked, INTERFACE_WRITE_PATH) == []
    sent = [(method, url.path) for (method, url) in mocked.requests]
    assert sent == [("POST", "/api/get-token/")]
    assert [entry["path"] for entry in transport.request_log] == ["/api/get-token/"]


async def test_requests_after_close_are_refused_without_io(no_http: Any) -> None:  # noqa: F811
    transport = ReadOnlyTransport(TEST_HOST, allow_login=True, allow_writes=True)
    t: Any = transport
    await transport.close()
    with pytest.raises(RehomConnectionError, match="transport is closed"):
        await transport.get_alive()
    with pytest.raises(RehomConnectionError, match="transport is closed"):
        await transport.login("user", "pw")
    t._token = TOKEN  # even with a token, nothing is sent after close()
    with pytest.raises(RehomConnectionError, match="transport is closed"):
        await transport.post_bulk_update(INTERFACE_WRITE_PATH, [BASE_REC])
    with pytest.raises(RehomConnectionError, match="transport is closed"):
        t._ensure_session()  # the backstop behind the request-path checks
    assert t._session is None
    assert transport.request_log == []
    no_http[0].assert_not_called()
