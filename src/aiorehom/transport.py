"""Read-only HTTP transport: the only place in aiorehom that issues HTTP requests.

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
    "ReadOnlyTransport",
    "RequestLogEntry",
    "check_request",
    "validate_host",
]

DEFAULT_PORT: Final = 8000
HISTORY_TIME_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
MAX_HISTORY_WINDOW: Final = timedelta(hours=24)
LOGIN_PATH: Final = "/api/get-token/"
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
_HISTORY_TIME_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_HISTORY_KEY_RE: Final = re.compile(r"[A-Z0-9_]{1,64}")
_HISTORY_UNITA_RE: Final = re.compile(r"(?:\d{3})?")
_TOKEN_RE: Final = re.compile(r"[\x21-\x7e]{1,1024}")
_ENCODE_SAFE: Final = "-_.!~*'()"  # encodeURIComponent's unreserved set (as superagent)


@dataclass(frozen=True, slots=True)
class _Rule:
    auth: bool
    query: Literal["none", "subset", "exact"]
    keys: frozenset[str] = frozenset()


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
        raise _forbid(method, path, "not on the read-only allowlist")
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
            f"allow_login={self._allow_login}, has_token={self._token is not None})"
        )

    async def __aenter__(self) -> ReadOnlyTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the session if the transport created it; forget the token."""
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
        timeout: float | None = None,  # noqa: ASYNC109 - per-request aiohttp ClientTimeout
    ) -> _Raw:
        total = self._timeout if timeout is None else _check_timeout(timeout)
        # 1. Allowlist (pure, no I/O).  Re-checked here for every request.
        query_dict = dict(query) if query is not None else {}
        rule = check_request(method, path, query_dict)
        is_login = method == "POST" and path == LOGIN_PATH
        if json_body is not None and not is_login:
            raise _forbid(method, path, "request bodies are only allowed for login")
        if is_login:
            if not self._allow_login:
                raise _forbid(method, path, "login not enabled for this transport")
            if self._login_attempted:
                raise _forbid(method, path, "login already attempted (at most once per transport)")
            if json_body is None or set(json_body) != {"username", "password"}:
                raise _forbid(method, path, "login body must be exactly {username, password}")
            self._login_attempted = True
        headers: dict[str, str] = {"Accept": "application/json"}
        if rule.auth:
            if self._token is None:
                raise RehomAuthenticationError(None, f"{method} {path}: not logged in")
            headers["Authorization"] = f"Token {self._token}"
        url = self._build_url(path, query_dict)
        guard = SingleSendGuard(method, url)

        # 2. Paced, sequential I/O.
        failure: RehomError | None = None
        status: int | None = None
        content_type: str | None = None
        body = b""
        async with self._lock:
            await self._pace()
            session = self._ensure_session()
            start = self._clock()
            error: str | None = None
            try:
                async with session.request(
                    method,
                    url,
                    headers=headers,
                    json=dict(json_body) if json_body is not None else None,
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
                        query=None if is_login else dict(query_dict),
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
