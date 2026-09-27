"""HTTP transport: the only place in aiorehom that issues HTTP requests.

It is read-only unless created with ``allow_writes=True``.

Safety model
------------
* Scheme ``http`` only; host and port are fixed at construction.  Callers never
  pass URLs: each allowed request has its own typed method, and every URL is
  built from the fixed base plus an allowlisted path.
* :func:`check_request` implements an exact ``(method, path, query keys)``
  allowlist.  Paths are validated strictly (no ``..``, ``//``, percent
  encoding, backslashes, non-ASCII or trailing garbage) and compared exactly;
  there is no prefix or glob matching.  Anything else raises
  :class:`~aiorehom.exceptions.ForbiddenRequestError` before any socket
  activity.  The internal ``_request`` re-checks the allowlist itself.
* Redirects are never followed; any 3xx raises.
* One call puts exactly one request on the wire: a per-request middleware
  (:class:`~aiorehom._guard.SingleSendGuard`) refuses any second hop or any
  hop to another method/URL, and aiohttp's transparent resend of idempotent
  requests is also disabled on the session the transport creates.  A
  caller-supplied session (for example Home Assistant's shared one) is never
  modified; the middleware alone refuses the resend there.
* Requests are strictly sequential and at least ``min_interval`` seconds apart.
  :meth:`ReadOnlyTransport.pacing_slot` lets another component (the WebSocket
  handshake) take a turn in the same queue.
* The token is held only in memory and sent only on authenticated allowlist
  entries.  It never appears in logs, exceptions or ``repr()``.
* The two bulk-write paths are refused unless the transport was created with
  ``allow_writes=True`` (a real ``bool``), and every body must pass
  :func:`check_write_body`: captured record shapes only, one per-key value
  domain each, rebuilt from plain ASCII values before it is sent.
* After :meth:`ReadOnlyTransport.close` nothing more is sent, not even a
  request that was already waiting for its turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import quote

import aiohttp
from yarl import URL

from ._guard import SingleSendGuard, disable_transparent_retry
from .exceptions import (
    ForbiddenRequestError,
    RehomAuthenticationError,
    RehomConnectionError,
    RehomError,
    RehomHttpError,
    RehomRedirectError,
    RehomResponseError,
    RehomTimeoutError,
)

__all__ = [
    "DOMOTICA_RESOURCES",
    "HISTORY_TIME_FORMAT",
    "INTERFACE_WRITE_PATH",
    "OVERRIDES_WRITE_PATH",
    "WRITABLE_INTERFACE_KEYS",
    "WRITE_BODY_TEMPLATES",
    "ReadOnlyTransport",
    "RequestLogEntry",
    "check_request",
    "check_write_body",
    "validate_host",
]

DEFAULT_PORT: Final = 8000
HISTORY_TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
MAX_HISTORY_WINDOW: Final = timedelta(hours=24)
LOGIN_PATH: Final = "/api/get-token/"
INTERFACE_WRITE_PATH: Final = "/api/interface/bulk_update/"
OVERRIDES_WRITE_PATH: Final = "/api/overrides/bulk_update/"
#: Most records one bulk write may carry (the largest captured write has 3).
MAX_WRITE_RECORDS: Final = 3
#: (Gruppo, Key) pairs a write may touch at all.
WRITABLE_INTERFACE_KEYS: Final = frozenset(
    {
        ("REHOM", "MODO"),
        ("REHOM", "SET_POINT"),
        ("REHOM", "SET_POINT_TEMP"),
        ("REHOM", "TEMP_COM"),
        ("REHOM", "ALG_ATTIVO"),
        ("ZONA", "SETP_CORRENTE"),
        ("ZONA", "DELTA_SETP_CORRENTE"),
        ("DEUM", "COM_VENTILA"),
        ("DEUM", "ST_MODE"),
    }
)
#: The interface bodies a write may carry: Gruppo -> the allowed ordered Key tuples.
#: Cross-record value rules are in :func:`_check_interface_body`.
WRITE_BODY_TEMPLATES: Final[Mapping[str, frozenset[tuple[str, ...]]]] = MappingProxyType(
    {
        "REHOM": frozenset(
            {
                ("MODO", "SET_POINT"),  # AUTO
                ("MODO", "SET_POINT", "SET_POINT_TEMP"),  # MANUAL at a level
                ("TEMP_COM", "SET_POINT_TEMP"),  # comfort temperature
                ("ALG_ATTIVO",),
            }
        ),
        "ZONA": frozenset({("SETP_CORRENTE",), ("DELTA_SETP_CORRENTE",)}),
        "DEUM": frozenset({("COM_VENTILA",), ("ST_MODE",)}),
    }
)
#: Integer-valued interface keys and the values a write may set.
_WRITE_INT_DOMAINS: Final[Mapping[str, frozenset[int]]] = MappingProxyType(
    {
        "MODO": frozenset({1, 2}),  # MANUAL, AUTO (never OFF)
        "SET_POINT": frozenset({0, 1, 2, 3}),
        "ALG_ATTIVO": frozenset({0, 1}),
        "SETP_CORRENTE": frozenset({0, 2, 3, 4}),  # schedule or a level (never OFF / PROBE_OFF)
        "ST_MODE": frozenset({0, 1, 2, 3, 4, 5, 8}),  # never the rapid modes 6 / 7
        "COM_VENTILA": frozenset(range(101)),
    }
)
_WRITE_TEMPERATURE_KEYS: Final = frozenset({"SET_POINT_TEMP", "TEMP_COM"})
#: Sanity band for a written temperature (°C); the command layer clamps tighter.
WRITE_TEMPERATURE_RANGE: Final = (Decimal("5.0"), Decimal("40.0"))
_INTERFACE_RECORD_FIELDS: Final = frozenset(
    {"Gruppo", "Unita", "SubUni", "Key", "Valore", "Stato", "Flusso", "path"}
)
_OVERRIDE_RECORD_FIELDS: Final = frozenset(
    {"Gruppo", "Unita", "SubUni", "Key", "Valore", "Impostazione", "Scadenza"}
)
# Write-path patterns are ASCII only ([0-9], never \d, which matches any Unicode digit).
_UNIT_RE: Final = re.compile(r"[0-9]{3}")
_MASTER_UNIT: Final = "000"
_OVERRIDE_KEY_RE: Final = re.compile(r"PROG_GIORNO_(?:INVERNO|ESTATE)")
_PRESET_RE: Final = re.compile(r"[1-9][0-9]?")
_PROGRAM_RE: Final = re.compile(r"[0-3](?:,[0-3]){47}")
_WRITE_INT_RE: Final = re.compile(r"0|[1-9][0-9]{0,2}")
_WRITE_TEMPERATURE_RE: Final = re.compile(r"[1-9][0-9]?(?:\.[0-9])?")
_WRITE_OFFSET_RE: Final = re.compile(r"-?[0-3]\.0")
DOMOTICA_RESOURCES: Final = ("object", "rooms", "scenarios", "config")
DEFAULT_MAX_BODY_BYTES: Final = 32 * 1024 * 1024
#: Request-log lines kept by default (the most recent; about 300 B each).
DEFAULT_REQUEST_LOG_SIZE: Final = 10_000

_HOST_RE: Final = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
#: Segments of letters/digits/_/-, each optionally followed by one ".ext".
_PATH_RE: Final = re.compile(r"(?:/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]+)?)+/?")
_QUERY_VALUE_RE: Final = re.compile(r"[A-Za-z0-9_.,: -]{0,256}")
_HISTORY_TIME_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}")
_HISTORY_KEY_RE: Final = re.compile(r"[A-Z0-9_]{1,64}")
_HISTORY_UNITA_RE: Final = re.compile(r"(?:[0-9]{3})?")
_TOKEN_RE: Final = re.compile(r"[\x21-\x7e]{1,1024}")
_ENCODE_SAFE: Final = "-_.!~*'()"  # encodeURIComponent's unreserved set (as superagent)


@dataclass(frozen=True, slots=True)
class _Rule:
    auth: bool
    query: Literal["none", "subset", "exact"]
    keys: frozenset[str] = frozenset()
    write: bool = False


_INTERFACE_KEYS: Final = frozenset({"Gruppo", "Unita", "SubUni", "Key", "Key__in"})
_HISTORY_KEYS: Final = frozenset({"Key", "Unita", "Tempo__gte", "Tempo__lte"})

#: The exact allowlist: (method, path) -> rule.
ALLOWLIST: Final[Mapping[tuple[str, str], _Rule]] = MappingProxyType(
    {
        ("GET", "/api/alive/"): _Rule(auth=False, query="none"),
        ("GET", "/api/me/"): _Rule(auth=True, query="none"),
        ("GET", "/api/overrides/"): _Rule(auth=True, query="none"),
        ("GET", "/api/plant/conf/"): _Rule(auth=True, query="none"),
        ("GET", "/api/config/"): _Rule(auth=True, query="none"),
        ("GET", "/api/interface/"): _Rule(auth=True, query="subset", keys=_INTERFACE_KEYS),
        ("GET", "/api/history/"): _Rule(auth=True, query="exact", keys=_HISTORY_KEYS),
        ("GET", "/api/domotica/object/"): _Rule(auth=True, query="none"),
        ("GET", "/api/domotica/rooms/"): _Rule(auth=True, query="none"),
        ("GET", "/api/domotica/scenarios/"): _Rule(auth=True, query="none"),
        ("GET", "/api/domotica/config/"): _Rule(auth=True, query="none"),
        ("POST", LOGIN_PATH): _Rule(auth=False, query="none"),
        ("POST", INTERFACE_WRITE_PATH): _Rule(auth=True, query="none", write=True),
        ("POST", OVERRIDES_WRITE_PATH): _Rule(auth=True, query="none", write=True),
    }
)


def validate_host(host: object) -> str:
    """Validate a controller host name or IPv4 literal (no scheme, port, path or user info)."""
    if not isinstance(host, str) or not host.isascii() or _HOST_RE.fullmatch(host) is None:
        raise ValueError("invalid host: expected a plain host name or IPv4 address")
    return host.lower()


def _forbid(method: object, path: object, reason: str) -> ForbiddenRequestError:
    shown = repr(path)[:120]
    return ForbiddenRequestError(f"{method!s:.10} {shown} refused: {reason}")


def _validate_path_syntax(method: str, path: object) -> str:
    if not isinstance(path, str):
        raise _forbid(method, path, "path must be a string")
    if not path.isascii() or not path.isprintable():
        raise _forbid(method, path, "non-ASCII or control characters in path")
    for bad in ("..", "//", "%", "\\", "?", "#", " ", ";", "@", "&", "="):
        if bad in path:
            raise _forbid(method, path, f"{bad!r} not allowed in path")
    if not path.startswith("/api/") or _PATH_RE.fullmatch(path) is None:
        raise _forbid(method, path, "malformed path")
    return path


def _check_history(query: Mapping[str, str]) -> None:
    if _HISTORY_KEY_RE.fullmatch(query["Key"]) is None:
        raise _forbid("GET", "/api/history/", "invalid Key")
    if _HISTORY_UNITA_RE.fullmatch(query["Unita"]) is None:
        raise _forbid("GET", "/api/history/", "invalid Unita")
    bounds: list[datetime] = []
    for name in ("Tempo__gte", "Tempo__lte"):
        text = query[name]
        if _HISTORY_TIME_RE.fullmatch(text) is None:
            raise _forbid("GET", "/api/history/", f"{name} must be 'YYYY-MM-DD HH:mm:ss'")
        try:
            bounds.append(datetime.strptime(text, HISTORY_TIME_FORMAT))
        except ValueError:
            raise _forbid("GET", "/api/history/", f"{name} is not a valid time") from None
    window = bounds[1] - bounds[0]
    if window <= timedelta(0):
        raise _forbid("GET", "/api/history/", "window must be positive (Tempo__lte > Tempo__gte)")
    if window > MAX_HISTORY_WINDOW:
        raise _forbid("GET", "/api/history/", "window longer than 24 h")


def check_request(
    method: str,
    path: str,
    query: Mapping[str, str] | None = None,
) -> _Rule:
    """Check one request against the allowlist; raise ForbiddenRequestError if not allowed.

    This is a pure function: it performs no I/O.
    """
    if method not in ("GET", "POST"):
        raise _forbid(method, path, "method not allowed")
    _validate_path_syntax(method, path)
    if query is None:
        query = {}
    if not isinstance(query, Mapping):
        raise _forbid(method, path, "query must be a mapping")
    for key, value in query.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _forbid(method, path, "query keys and values must be strings")
        if not value.isascii() or _QUERY_VALUE_RE.fullmatch(value) is None:
            raise _forbid(method, path, f"invalid characters in query value for {key[:32]!r}")
    rule = ALLOWLIST.get((method, path))
    if rule is None:
        raise _forbid(method, path, "not on the allowlist")
    keys = set(query)
    if rule.query == "none" and keys:
        raise _forbid(method, path, "no query string allowed")
    if rule.query == "subset" and not keys <= rule.keys:
        raise _forbid(method, path, f"query keys must be a subset of {sorted(rule.keys)}")
    if rule.query == "exact" and keys != rule.keys:
        raise _forbid(method, path, f"query keys must be exactly {sorted(rule.keys)}")
    if path == "/api/history/":
        _check_history(query)
    return rule


def _plain_str(path: str, rec: Mapping[str, object], name: str) -> str:
    """``rec[name]`` as an exact, ASCII-only ``str`` (no subclass, no Unicode digits)."""
    value = rec[name]
    if type(value) is not str:
        raise _forbid("POST", path, f"{name} must be a string")
    if not value.isascii():
        raise _forbid("POST", path, f"{name} must be ASCII")
    return value


def _check_write_value(path: str, key: str, value: object) -> tuple[str | int, Decimal]:
    """Check an interface ``Valore`` against its key's domain.

    Returns the value as sent (an exact ``str`` or ``int``) and as a number.
    """
    if type(value) is int:
        if not -999 <= value <= 999:  # bounded before str(): no huge-int conversion
            raise _forbid("POST", path, f"{key} value out of range")
        text = str(value)
    elif type(value) is str:
        if not value.isascii():
            raise _forbid("POST", path, "Valore must be ASCII")
        text = value
    else:
        raise _forbid("POST", path, "Valore must be a string or an int")
    if key in _WRITE_INT_DOMAINS:
        if _WRITE_INT_RE.fullmatch(text) is None or int(text) not in _WRITE_INT_DOMAINS[key]:
            raise _forbid("POST", path, f"{key} value out of range")
        return value, Decimal(int(text))
    if key in _WRITE_TEMPERATURE_KEYS:
        low, high = WRITE_TEMPERATURE_RANGE
        if _WRITE_TEMPERATURE_RE.fullmatch(text) is None or not low <= Decimal(text) <= high:
            raise _forbid("POST", path, f"{key} must be {low}..{high} with at most one decimal")
        return value, Decimal(text)
    if key == "DELTA_SETP_CORRENTE":
        if type(value) is not str or _WRITE_OFFSET_RE.fullmatch(text) is None:
            raise _forbid("POST", path, f"{key} must be whole degrees -3..3 spelled like '1.0'")
        return value, Decimal(text)
    raise _forbid("POST", path, f"{key[:32]} has no value domain")  # pragma: no cover


def _interface_record(path: str, rec: Mapping[str, object]) -> tuple[dict[str, object], Decimal]:
    """Validate one interface record; return a new plain record and its value as a number."""
    if any(type(name) is not str for name in rec) or set(rec) != _INTERFACE_RECORD_FIELDS:
        raise _forbid("POST", path, "interface record fields do not match")
    gruppo, unita, subuni, key, record_path = (
        _plain_str(path, rec, name) for name in ("Gruppo", "Unita", "SubUni", "Key", "path")
    )
    if (gruppo, key) not in WRITABLE_INTERFACE_KEYS:
        raise _forbid("POST", path, f"{gruppo[:16]}.{key[:32]} is not writable")
    if subuni != "":
        raise _forbid("POST", path, "SubUni must be empty")
    if (gruppo == "REHOM") != (unita == ""):
        raise _forbid("POST", path, "Unita does not fit the group")
    if unita and (_UNIT_RE.fullmatch(unita) is None or unita == _MASTER_UNIT):
        raise _forbid("POST", path, "Unita must be a 3-digit id other than 000")
    stato, flusso = rec["Stato"], rec["Flusso"]
    if type(stato) is not int or stato != 1 or type(flusso) is not int or flusso != 0:
        raise _forbid("POST", path, "Stato/Flusso must be the ints 1/0")
    expected_path = f"{gruppo}.{unita}.{subuni}.{key}"
    if record_path != expected_path:
        raise _forbid("POST", path, "path does not match the record")
    valore, number = _check_write_value(path, key, rec["Valore"])
    record: dict[str, object] = {
        "Gruppo": gruppo,
        "Unita": unita,
        "SubUni": subuni,
        "Key": key,
        "Valore": valore,
        "Stato": 1,
        "Flusso": 0,
        "path": expected_path,
    }
    return record, number


def _check_interface_body(
    path: str, records: list[dict[str, object]], values: list[Decimal]
) -> None:
    """The whole body must be one of :data:`WRITE_BODY_TEMPLATES` with consistent values."""
    if len({(r["Gruppo"], r["Unita"]) for r in records}) != 1:
        raise _forbid("POST", path, "a body must touch exactly one Gruppo and one Unita")
    gruppo = str(records[0]["Gruppo"])
    keys = tuple(str(r["Key"]) for r in records)
    if keys not in WRITE_BODY_TEMPLATES.get(gruppo, frozenset()):
        raise _forbid("POST", path, f"{gruppo[:16]} body {keys!r:.120} is not an allowed shape")
    by_key = dict(zip(keys, values, strict=True))
    if keys == ("MODO", "SET_POINT") and not (by_key["MODO"] == 2 and by_key["SET_POINT"] == 0):
        raise _forbid("POST", path, "a MODO/SET_POINT body must be AUTO (2/0)")
    if keys == ("MODO", "SET_POINT", "SET_POINT_TEMP") and not (
        by_key["MODO"] == 1 and 1 <= by_key["SET_POINT"] <= 3
    ):
        raise _forbid("POST", path, "a MODO/SET_POINT/SET_POINT_TEMP body must be MANUAL 1..3")
    if keys == ("TEMP_COM", "SET_POINT_TEMP") and by_key["TEMP_COM"] != by_key["SET_POINT_TEMP"]:
        raise _forbid("POST", path, "TEMP_COM and SET_POINT_TEMP must be equal")


def _override_time(path: str, name: str, text: str) -> datetime:
    if _HISTORY_TIME_RE.fullmatch(text) is None:
        raise _forbid("POST", path, f"{name} must be 'YYYY-MM-DD HH:mm:ss'")
    try:
        return datetime.strptime(text, HISTORY_TIME_FORMAT)
    except ValueError:
        raise _forbid("POST", path, f"{name} is not a valid time") from None


def _override_record(path: str, rec: Mapping[str, object]) -> dict[str, object]:
    """Validate one ``PROG_OVERRIDE`` day-program record; return a new plain record."""
    if any(type(name) is not str for name in rec) or set(rec) != _OVERRIDE_RECORD_FIELDS:
        raise _forbid("POST", path, "override record fields do not match")
    gruppo, unita, subuni, key, valore, impostazione, scadenza = (
        _plain_str(path, rec, name)
        for name in ("Gruppo", "Unita", "SubUni", "Key", "Valore", "Impostazione", "Scadenza")
    )
    if gruppo != "PROG_OVERRIDE" or _OVERRIDE_KEY_RE.fullmatch(key) is None:
        raise _forbid("POST", path, "only PROG_OVERRIDE day programs are writable")
    if _UNIT_RE.fullmatch(unita) is None or unita == _MASTER_UNIT:
        raise _forbid("POST", path, "Unita must be a 3-digit id other than 000")
    if _PRESET_RE.fullmatch(subuni) is None:
        raise _forbid("POST", path, "SubUni must be a preset number 1..99")
    if _PROGRAM_RE.fullmatch(valore) is None:
        raise _forbid("POST", path, "Valore must be 48 slot levels 0-3")
    start = _override_time(path, "Impostazione", impostazione)
    end = _override_time(path, "Scadenza", scadenza)
    if start.date() != end.date():
        raise _forbid("POST", path, "override must start and end on the same day")
    if not timedelta(0) < end - start <= timedelta(hours=24):
        raise _forbid("POST", path, "override window must be 0 < length <= 24 h")
    return {
        "Gruppo": gruppo,
        "Unita": unita,
        "SubUni": subuni,
        "Key": key,
        "Valore": valore,
        "Impostazione": impostazione,
        "Scadenza": scadenza,
    }


def check_write_body(path: str, records: object) -> list[dict[str, object]]:
    """Validate a bulk-write body; return newly built plain records.  Pure, no I/O.

    Only the record shapes seen from the official client are accepted:
    interface records ``{Gruppo, Unita, SubUni, Key, Valore, Stato: 1,
    Flusso: 0, path}`` whose keys form one of :data:`WRITE_BODY_TEMPLATES`
    with every value in its key's domain, and exactly one ``PROG_OVERRIDE``
    day-program record with a same-day ``Impostazione``/``Scadenza`` window.

    The returned records are rebuilt, in the captured key order, from the
    validated plain values (exact ``str``/``int``, ASCII only), so what is sent
    is exactly what was checked; the caller's objects are never passed on.
    """
    if path not in (INTERFACE_WRITE_PATH, OVERRIDES_WRITE_PATH):
        raise _forbid("POST", path, "not a write path")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        raise _forbid("POST", path, "body must be a list of records")
    items = list(records)
    limit = MAX_WRITE_RECORDS if path == INTERFACE_WRITE_PATH else 1
    if not 1 <= len(items) <= limit:
        raise _forbid("POST", path, f"body must hold 1..{limit} records")
    out: list[dict[str, object]] = []
    values: list[Decimal] = []
    for record in items:
        if not isinstance(record, Mapping):
            raise _forbid("POST", path, "each record must be an object")
        rec = dict(record)
        if path == INTERFACE_WRITE_PATH:
            built, number = _interface_record(path, rec)
            out.append(built)
            values.append(number)
        else:
            out.append(_override_record(path, rec))
    if path == INTERFACE_WRITE_PATH:
        _check_interface_body(path, out, values)
    return out


def _check_timeout(timeout: object) -> float:
    """A per-call timeout: a finite number of seconds > 0 (``bool`` refused)."""
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a finite number of seconds > 0")
    return float(timeout)


def format_history_time(value: datetime | str) -> str:
    """Format a controller-local naive datetime as ``YYYY-MM-DD HH:mm:ss``."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("history bounds must be naive controller-local datetimes")
        return value.strftime(HISTORY_TIME_FORMAT)
    return value


@dataclass(frozen=True, slots=True)
class RequestLogEntry:
    """One line of the request log.  Never contains headers or bodies."""

    method: str
    path: str
    query_keys: list[str]
    query: dict[str, str] | None
    status: int | None
    latency_ms: float
    bytes: int
    ok: bool
    error: str | None
    mono: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Raw:
    status: int
    content_type: str | None
    body: bytes


class _BodyTooLargeError(Exception):
    pass


class ReadOnlyTransport:
    """Sequential, paced, allowlisted HTTP client for one Rehom controller.

    ``session``: an optional caller-owned ``aiohttp.ClientSession``.  It is used
    as is and never modified or closed (the per-request send guard makes the
    one-request-per-call guarantee independent of the session's settings).
    Without one, the transport creates and owns a private session.

    ``request_log_size``: how many :class:`RequestLogEntry` lines
    :attr:`request_log` keeps (the most recent ones; ``None`` = unbounded, only
    for short probe runs).  The default bounds memory in a long-lived process.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        session: aiohttp.ClientSession | None = None,
        min_interval: float = 1.0,
        timeout: float = 10,
        *,
        allow_login: bool = False,
        allow_writes: bool = False,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_log_size: int | None = DEFAULT_REQUEST_LOG_SIZE,
    ) -> None:
        self._host = validate_host(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("invalid port")
        self._port = port
        if min_interval < 0 or timeout <= 0:
            raise ValueError("min_interval must be >= 0 and timeout > 0")
        # The one explicit write opt-in: a real bool only ("false", 1, None... are refused).
        if allow_writes is not True and allow_writes is not False:
            raise TypeError("allow_writes must be a bool")
        if request_log_size is not None and (
            isinstance(request_log_size, bool)
            or not isinstance(request_log_size, int)
            or request_log_size < 0
        ):
            raise ValueError("request_log_size must be None or an int >= 0")
        if session is not None:
            for header in ("Authorization", "Cookie", "Proxy-Authorization"):
                if header in session.headers:
                    raise ValueError(f"session must not carry a default {header} header")
        self._session = session
        self._owns_session = session is None
        self._min_interval = float(min_interval)
        self._timeout = float(timeout)
        self._allow_login = allow_login
        self._allow_writes = allow_writes is True
        self._closed = False
        self._login_attempted = False
        self._token: str | None = None
        self._max_body = max_body_bytes
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_done: float | None = None
        self._log: deque[RequestLogEntry] = deque(maxlen=request_log_size)

    # -- housekeeping ------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"ReadOnlyTransport(host={self._host!r}, port={self._port}, "
            f"allow_login={self._allow_login}, allow_writes={self._allow_writes}, "
            f"has_token={self._token is not None})"
        )

    async def __aenter__(self) -> ReadOnlyTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session if the transport created it; forget the token.

        The transport cannot be used again: every later request is refused with
        :class:`~aiorehom.exceptions.RehomConnectionError`, including one already
        waiting for its turn in the queue, and no new session is ever created.  A
        request already on the wire when ``close()`` runs is aborted only when the
        transport owns its session; on a caller-owned session it runs to
        completion.  Either way it may or may not have reached the controller.
        """
        self._closed = True
        self._token = None
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def has_token(self) -> bool:
        return self._token is not None

    @property
    def allow_writes(self) -> bool:
        return self._allow_writes

    @property
    def login_attempted(self) -> bool:
        return self._login_attempted

    @property
    def request_log(self) -> list[dict[str, Any]]:
        """Copy of the request log (method, path, query, status, latency, size).

        Only the most recent ``request_log_size`` requests are kept.
        """
        return [entry.to_dict() for entry in self._log]

    @property
    def last_request(self) -> dict[str, Any] | None:
        return self._log[-1].to_dict() if self._log else None

    @contextlib.asynccontextmanager
    async def pacing_slot(self) -> AsyncIterator[None]:
        """Take a turn in this transport's sequential, paced request queue.

        Nothing is sent here.  The caller makes its own request (the WebSocket
        handshake of ``watch --with-latency``) inside the block, so it is at
        least ``min_interval`` seconds away from every request of this
        transport, and they never overlap.
        """
        async with self._lock:
            await self._pace()
            try:
                yield
            finally:
                self._last_done = self._clock()

    # -- typed, allowlisted requests ----------------------------------------

    async def login(self, username: str, password: str) -> None:
        """``POST /api/get-token/`` once; keep the token in memory only.

        Allowed only when the transport was created with ``allow_login=True``,
        and at most once per transport instance (a failed attempt counts).
        """
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("username and password must be strings")
        raw = await self._request(
            "POST", LOGIN_PATH, json_body={"username": username, "password": password}
        )
        try:
            data = json.loads(raw.body)
        except ValueError:
            raise RehomResponseError(raw.status, "login response is not valid JSON") from None
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
            raise RehomResponseError(raw.status, "login response did not contain a usable token")
        self._token = token

    async def get_alive(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        """``GET /api/alive/`` (never sends the token)."""
        return await self._get_json("/api/alive/", timeout=timeout)

    async def get_me(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        return await self._get_json("/api/me/", timeout=timeout)

    async def get_config(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        """``GET /api/config/`` with no query string (``?reload=1`` is never sent)."""
        return await self._get_json("/api/config/", timeout=timeout)

    async def get_interface(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        **filters: str | Sequence[str],
    ) -> Any:
        """``GET /api/interface/`` with optional django-filter style filters.

        Allowed filter names: ``Gruppo``, ``Unita``, ``SubUni``, ``Key``,
        ``Key__in`` (a string or a sequence joined with ``,``).  ``timeout``
        (seconds) overrides the transport default for this call only.
        """
        query: dict[str, str] = {}
        for name, value in filters.items():
            if isinstance(value, str):
                query[name] = value
            elif isinstance(value, Sequence) and all(isinstance(v, str) for v in value):
                query[name] = ",".join(value)
            else:
                raise _forbid("GET", "/api/interface/", "filter values must be strings")
        return await self._get_json("/api/interface/", query=query, timeout=timeout)

    async def get_overrides(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        return await self._get_json("/api/overrides/", timeout=timeout)

    async def get_plant_conf(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        return await self._get_json("/api/plant/conf/", timeout=timeout)

    async def get_history(
        self,
        key: str,
        unita: str,
        gte: datetime | str,
        lte: datetime | str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> Any:
        """``GET /api/history/`` for one series over a window of at most 24 h."""
        query = {
            "Key": key,
            "Unita": unita,
            "Tempo__gte": format_history_time(gte),
            "Tempo__lte": format_history_time(lte),
        }
        return await self._get_json("/api/history/", query=query, timeout=timeout)

    async def get_domotica(self, resource: str, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
        """``GET /api/domotica/<resource>/`` for object, rooms, scenarios or config."""
        if resource not in DOMOTICA_RESOURCES:
            raise _forbid("GET", f"/api/domotica/{resource!s:.40}/", "unknown domotica resource")
        return await self._get_json(f"/api/domotica/{resource}/", timeout=timeout)

    async def post_bulk_update(
        self,
        path: str,
        records: Sequence[Mapping[str, object]],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> int:
        """Send one bulk write (``interface`` or ``overrides``); return the HTTP status.

        Refused before any I/O unless the transport was created with
        ``allow_writes=True`` and the body passes :func:`check_write_body`.
        Never retried.  The response body is ignored.
        """
        body = check_write_body(path, records)
        raw = await self._request("POST", path, write_body=body, timeout=timeout)
        return raw.status

    # -- internals -----------------------------------------------------------

    async def _get_json(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> Any:
        raw = await self._request("GET", path, query=query, timeout=timeout)
        try:
            return json.loads(raw.body)
        except ValueError:
            raise RehomResponseError(
                raw.status, f"GET {path}: response is not valid JSON"
            ) from None

    def _build_url(self, path: str, query: Mapping[str, str]) -> URL:
        qs = "&".join(
            f"{quote(k, safe=_ENCODE_SAFE)}={quote(v, safe=_ENCODE_SAFE)}" for k, v in query.items()
        )
        raw = f"http://{self._host}:{self._port}{path}" + (f"?{qs}" if qs else "")
        url = URL(raw, encoded=True)
        if (
            url.scheme != "http"
            or url.host != self._host
            or url.explicit_port != self._port
            or url.raw_path != path
            or url.raw_query_string != qs
            or url.user is not None
            or url.fragment
        ):
            raise _forbid("GET", path, "URL construction mismatch")
        return url

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:  # never (re)create a session after close()
            raise RehomConnectionError("the transport is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.DummyCookieJar(), trust_env=False
            )
            disable_transparent_retry(self._session)
            self._owns_session = True
        return self._session

    async def _pace(self) -> None:
        if self._last_done is None:
            return
        wait = self._last_done + self._min_interval - self._clock()
        if wait > 0:
            await self._sleep(wait)

    async def _read_body(self, resp: aiohttp.ClientResponse) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.content.iter_chunked(65536):
            total += len(chunk)
            if total > self._max_body:
                raise _BodyTooLargeError
            chunks.append(chunk)
        return b"".join(chunks)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        json_body: Mapping[str, str] | None = None,
        write_body: list[dict[str, object]] | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> _Raw:
        total = self._timeout if timeout is None else _check_timeout(timeout)
        # 1. Allowlist (pure, no I/O).  Re-checked here for every request.
        query_dict = dict(query) if query is not None else {}
        rule = check_request(method, path, query_dict)
        is_login = method == "POST" and path == LOGIN_PATH
        if json_body is not None and not is_login:
            raise _forbid(method, path, "request bodies are only allowed for login")
        if rule.write:
            if self._allow_writes is not True:
                raise _forbid(method, path, "writes not enabled for this transport")
            if write_body is None:
                raise _forbid(method, path, "a write needs a body")
            write_body = check_write_body(path, write_body)
        elif write_body is not None:
            raise _forbid(method, path, "write bodies are only allowed on write paths")
        if self._closed:
            raise RehomConnectionError(f"{method} {path}: the transport is closed")
        if is_login:
            if not self._allow_login:
                raise _forbid(method, path, "login not enabled for this transport")
            if self._login_attempted:
                raise _forbid(method, path, "login already attempted (at most once per transport)")
            if json_body is None or set(json_body) != {"username", "password"}:
                raise _forbid(method, path, "login body must be exactly {username, password}")
            self._login_attempted = True
        if rule.auth and self._token is None:
            raise RehomAuthenticationError(None, f"{method} {path}: not logged in")
        headers: dict[str, str] = {"Accept": "application/json"}
        url = self._build_url(path, query_dict)
        guard = SingleSendGuard(method, url)

        # 2. Paced, sequential I/O.
        failure: RehomError | None = None
        status: int | None = None
        content_type: str | None = None
        body = b""
        async with self._lock:
            await self._pace()
            # Re-checked after the wait (close() may have run meanwhile); nothing
            # below awaits before the send, so close() cannot slip in between.
            if self._closed:
                raise RehomConnectionError(f"{method} {path}: the transport is closed")
            if rule.auth:
                if self._token is None:
                    raise RehomAuthenticationError(None, f"{method} {path}: not logged in")
                headers["Authorization"] = f"Token {self._token}"
            session = self._ensure_session()
            start = self._clock()
            error: str | None = None
            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=(
                        dict(json_body)
                        if json_body is not None
                        else write_body
                        if write_body is not None
                        else None
                    ),
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=total),
                    middlewares=(guard,),
                ) as resp:
                    status = resp.status
                    content_type = resp.content_type
                    body = await self._read_body(resp)
            except ForbiddenRequestError:
                error = "forbidden"
                raise
            except TimeoutError:
                error = "timeout"
                failure = RehomTimeoutError(f"{method} {path}: timed out after {total:g} s")
            except _BodyTooLargeError:
                error = "body_too_large"
                failure = RehomResponseError(status, f"{method} {path}: response body too large")
            except (aiohttp.ClientError, OSError) as err:
                error = type(err).__name__
                failure = RehomConnectionError(f"{method} {path}: connection failed ({error})")
            finally:
                end = self._clock()
                self._last_done = end
                ok = error is None and status is not None and 200 <= status < 300
                if error is None and status is not None and not ok:
                    error = f"http_{status}"
                self._log.append(
                    RequestLogEntry(
                        method=method,
                        path=path,
                        query_keys=sorted(query_dict),
                        query=None if is_login or rule.write else dict(query_dict),
                        status=status,
                        latency_ms=round((end - start) * 1000.0, 1),
                        bytes=len(body),
                        ok=ok,
                        error=error,
                        mono=round(end, 3),
                    )
                )
        if failure is not None:
            raise failure
        if status is None:  # pragma: no cover - aiohttp always sets a status
            raise RehomConnectionError(f"{method} {path}: no response")

        # 3. Status handling (bodies never go into exception messages).
        if 300 <= status < 400:
            raise RehomRedirectError(status)
        if status in (401, 403):
            raise RehomAuthenticationError(status, f"{method} {path}: HTTP {status}")
        if not 200 <= status < 300:
            raise RehomHttpError(status, f"{method} {path}: HTTP {status}")
        return _Raw(status, content_type, body)
