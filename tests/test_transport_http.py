"""Transport behaviour against aioresponses mocks (no real sockets)."""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import CallbackResult, aioresponses
from yarl import URL

from aiorehom import transport as transport_module
from aiorehom.exceptions import (
    ForbiddenRequestError,
    RehomAuthenticationError,
    RehomConnectionError,
    RehomHttpError,
    RehomRedirectError,
    RehomResponseError,
    RehomTimeoutError,
)
from aiorehom.transport import ReadOnlyTransport

from .conftest import BASE, CONFIG, INTERFACE, TEST_HOST

TOKEN = "tok-abcdef0123456789"
PASSWORD = "s3cret-user-pw"


def _url(path: str) -> str:
    return f"{BASE}{path}"


def _requests(mocked: aioresponses) -> list[tuple[str, URL, dict[str, Any]]]:
    out = []
    for (method, url), calls in mocked.requests.items():
        for call in calls:
            out.append((method, url, call.kwargs))
    return out


def _headers_for(mocked: aioresponses, path: str) -> list[dict[str, str]]:
    return [
        dict(kwargs.get("headers") or {})
        for _method, url, kwargs in _requests(mocked)
        if url.path == path
    ]


@pytest.fixture
def mocked() -> Any:
    with aioresponses() as m:
        yield m


async def _logged_in(mocked: aioresponses, **kwargs: Any) -> ReadOnlyTransport:
    mocked.post(_url("/api/get-token/"), payload={"token": TOKEN})
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, allow_login=True, **kwargs)
    await transport.login("alice", PASSWORD)
    return transport


async def test_login_sends_json_body_once_and_holds_token(mocked: aioresponses) -> None:
    transport = await _logged_in(mocked)
    assert transport.has_token
    ((method, url, kwargs),) = _requests(mocked)
    assert method == "POST"
    assert url.path == "/api/get-token/"
    assert kwargs["json"] == {"username": "alice", "password": PASSWORD}
    assert kwargs["allow_redirects"] is False
    assert "Authorization" not in kwargs["headers"]
    with pytest.raises(ForbiddenRequestError, match="at most once"):
        await transport.login("alice", PASSWORD)
    assert len(_requests(mocked)) == 1
    log = transport.request_log
    assert log[0]["query"] is None
    assert log[0]["query_keys"] == []
    await transport.close()
    assert not transport.has_token


async def test_failed_login_counts_as_the_one_attempt(mocked: aioresponses) -> None:
    mocked.post(_url("/api/get-token/"), status=400, body='{"non_field_errors":["bad"]}')
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, allow_login=True)
    with pytest.raises(RehomHttpError) as info:
        await transport.login("alice", PASSWORD)
    assert info.value.status == 400
    assert "bad" not in str(info.value)
    with pytest.raises(ForbiddenRequestError):
        await transport.login("alice", PASSWORD)
    await transport.close()


@pytest.mark.parametrize(
    "body",
    ['{"nope": 1}', "not json", '{"token": ""}', '{"token": "a\\r\\nX-Evil: 1"}', "[1]"],
)
async def test_login_bad_token_response(mocked: aioresponses, body: str) -> None:
    mocked.post(_url("/api/get-token/"), status=200, body=body)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, allow_login=True)
    with pytest.raises(RehomResponseError):
        await transport.login("alice", PASSWORD)
    assert not transport.has_token
    await transport.close()


async def test_login_requires_allow_login(mocked: aioresponses) -> None:
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0)
    with pytest.raises(ForbiddenRequestError, match="login not enabled"):
        await transport.login("alice", PASSWORD)
    assert _requests(mocked) == []
    with pytest.raises(TypeError):
        await transport.login("alice", None)  # type: ignore[arg-type]
    await transport.close()


async def test_auth_header_only_on_authenticated_endpoints(mocked: aioresponses) -> None:
    transport = await _logged_in(mocked)
    mocked.get(_url("/api/alive/"), payload={"version": "1"})
    mocked.get(_url("/api/me/"), payload={"username": "alice"})
    mocked.get(_url("/api/config/"), payload=CONFIG)
    mocked.get(_url("/api/interface/"), payload=INTERFACE)
    mocked.get(re.compile(r".*/api/interface/\?.*"), payload=[])
    mocked.get(_url("/api/overrides/"), payload=[])
    mocked.get(_url("/api/plant/conf/"), payload={})
    mocked.get(re.compile(r".*/api/history/\?.*"), payload=[])
    for resource in ("object", "rooms", "scenarios", "config"):
        mocked.get(_url(f"/api/domotica/{resource}/"), payload=[])

    assert await transport.get_alive() == {"version": "1"}
    assert await transport.get_me() == {"username": "alice"}
    assert (await transport.get_config())["TIMEZONE"] == "Europe/Rome"
    assert len(await transport.get_interface()) == len(INTERFACE)
    assert await transport.get_interface(Gruppo="ZONA", Unita="002") == []
    assert await transport.get_overrides() == []
    assert await transport.get_plant_conf() == {}
    assert (
        await transport.get_history(
            "CONT_CALORIE", "", "2026-09-25 04:15:30", "2026-09-25 10:15:30"
        )
        == []
    )
    for resource in ("object", "rooms", "scenarios", "config"):
        await transport.get_domotica(resource)

    for method, url, kwargs in _requests(mocked):
        headers = kwargs.get("headers") or {}
        assert kwargs["allow_redirects"] is False, url
        if url.path in ("/api/alive/", "/api/get-token/"):
            assert "Authorization" not in headers, (method, url)
        else:
            assert headers.get("Authorization") == f"Token {TOKEN}", (method, url)
    assert all("Authorization" not in h for h in _headers_for(mocked, "/api/alive/"))
    await transport.close()


async def test_query_encoding_matches_superagent() -> None:
    """The raw query on the wire is encodeURIComponent-style (``%20``, ``%3A``, ``%2C``).

    aioresponses re-encodes URLs, so this uses a real 127.0.0.1 server and
    compares the raw request target byte for byte.
    """
    seen: list[tuple[str, str | None]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.raw_path, request.headers.get("Authorization")))
        if request.path == "/api/get-token/":
            return web.json_response({"token": TOKEN})
        return web.json_response([])

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    assert server.port is not None
    try:
        async with ReadOnlyTransport(
            "127.0.0.1", server.port, min_interval=0, allow_login=True
        ) as transport:
            await transport.login("alice", PASSWORD)
            await transport.get_history(
                "TEMP_AMBIENTE", "002", "2026-09-25 04:15:30", "2026-09-25 10:15:30"
            )
            await transport.get_interface(Key__in=["STAGIONE", "MODO"])
            await transport.get_interface(Gruppo="ZONA", Unita="002")
    finally:
        await server.close()
    assert seen == [
        ("/api/get-token/", None),
        (
            "/api/history/?Key=TEMP_AMBIENTE&Unita=002"
            "&Tempo__gte=2026-09-25%2004%3A15%3A30&Tempo__lte=2026-09-25%2010%3A15%3A30",
            f"Token {TOKEN}",
        ),
        ("/api/interface/?Key__in=STAGIONE%2CMODO", f"Token {TOKEN}"),
        ("/api/interface/?Gruppo=ZONA&Unita=002", f"Token {TOKEN}"),
    ]


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirects_raise_and_are_not_followed(mocked: aioresponses, status: int) -> None:
    mocked.get(_url("/api/alive/"), status=status, headers={"Location": "/api/plant/msg/"})
    mocked.get(_url("/api/plant/msg/"), payload={"oops": True})
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0)
    with pytest.raises(RehomRedirectError) as info:
        await transport.get_alive()
    assert info.value.status == status
    paths = [url.path for _m, url, _k in _requests(mocked)]
    assert paths == ["/api/alive/"]
    assert transport.request_log[-1]["ok"] is False
    await transport.close()


async def test_http_errors_never_include_body_or_token(mocked: aioresponses) -> None:
    transport = await _logged_in(mocked)
    secret_body = json.dumps({"detail": "Invalid token tok-abcdef0123456789 SECRETBODY"})
    mocked.get(_url("/api/me/"), status=401, body=secret_body)
    mocked.get(_url("/api/config/"), status=403, body=secret_body)
    mocked.get(_url("/api/overrides/"), status=500, body=secret_body)
    mocked.get(_url("/api/plant/conf/"), status=200, body="SECRETBODY not json")
    mocked.get(_url("/api/interface/"), exception=aiohttp.ClientConnectionError("boom"))
    mocked.get(_url("/api/alive/"), exception=TimeoutError())
    mocked.get(_url("/api/domotica/rooms/"), exception=aiohttp.ClientPayloadError("x"))
    errors: list[BaseException] = []
    for coro, exc_type in (
        (transport.get_me(), RehomAuthenticationError),
        (transport.get_config(), RehomAuthenticationError),
        (transport.get_overrides(), RehomHttpError),
        (transport.get_plant_conf(), RehomResponseError),
        (transport.get_interface(), RehomConnectionError),
        (transport.get_alive(), RehomTimeoutError),
        (transport.get_domotica("rooms"), RehomConnectionError),
    ):
        with pytest.raises(exc_type) as info:
            await coro
        errors.append(info.value)
    assert [getattr(e, "status", None) for e in errors[:3]] == [401, 403, 500]
    for err in errors:
        text = str(err) + repr(err)
        assert "SECRETBODY" not in text
        assert TOKEN not in text
        assert PASSWORD not in text
        assert err.__cause__ is None
        assert err.__context__ is None or not isinstance(err.__context__, aiohttp.ClientError)
    log_text = json.dumps(transport.request_log)
    assert TOKEN not in log_text
    assert PASSWORD not in log_text
    assert "SECRETBODY" not in log_text
    assert TOKEN not in repr(transport)
    assert "has_token=True" in repr(transport)
    await transport.close()


async def test_request_log_fields(mocked: aioresponses) -> None:
    transport = await _logged_in(mocked)
    mocked.get(re.compile(r".*/api/interface/\?.*"), payload=[{"a": 1}])
    await transport.get_interface(Gruppo="ZONA", Unita="002")
    entry = transport.last_request
    assert entry is not None
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/interface/"
    assert entry["query_keys"] == ["Gruppo", "Unita"]
    assert entry["query"] == {"Gruppo": "ZONA", "Unita": "002"}
    assert entry["status"] == 200
    assert entry["ok"] is True
    assert entry["bytes"] > 0
    assert entry["latency_ms"] >= 0
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
    await transport.close()


async def test_body_size_cap(mocked: aioresponses) -> None:
    mocked.get(_url("/api/alive/"), body=b"x" * 2048)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, max_body_bytes=1024)
    with pytest.raises(RehomResponseError, match="too large"):
        await transport.get_alive()
    await transport.close()


async def test_pacing_with_fake_clock(mocked: aioresponses) -> None:
    now = [100.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    for _ in range(3):
        mocked.get(_url("/api/alive/"), payload={})
    transport = ReadOnlyTransport(TEST_HOST, min_interval=1.0, clock=clock, sleep=fake_sleep)
    await transport.get_alive()
    now[0] += 0.25
    await transport.get_alive()
    now[0] += 5.0
    await transport.get_alive()
    assert sleeps == [pytest.approx(0.75)]
    await transport.close()


async def test_pacing_real_clock_and_sequential(mocked: aioresponses) -> None:
    starts: list[float] = []
    active = [0]
    overlap = [False]

    async def callback(url: URL, **kwargs: Any) -> CallbackResult:
        active[0] += 1
        if active[0] > 1:
            overlap[0] = True
        starts.append(time.monotonic())
        await asyncio.sleep(0.02)
        active[0] -= 1
        return CallbackResult(status=200, payload={"version": "1"})

    mocked.get(_url("/api/alive/"), callback=callback, repeat=True)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0.05)
    await asyncio.gather(*(transport.get_alive() for _ in range(4)))
    assert not overlap[0]
    assert len(starts) == 4
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    # each start is >= min_interval after the previous request *finished*
    assert all(gap >= 0.05 + 0.02 - 0.005 for gap in gaps), gaps
    await transport.close()


async def test_supplied_session_is_used_and_not_closed(mocked: aioresponses) -> None:
    mocked.get(_url("/api/alive/"), payload={})
    async with aiohttp.ClientSession() as session:
        async with ReadOnlyTransport(TEST_HOST, session=session, min_interval=0) as transport:
            await transport.get_alive()
        assert not session.closed
    assert transport.host == TEST_HOST
    assert transport.port == 8000


async def _drop_first_connection_server() -> tuple[asyncio.Server, list[tuple[float, bytes]]]:
    """Raw 127.0.0.1 server: closes the first connection after the request head, then answers."""
    heads: list[tuple[float, bytes]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append((time.monotonic(), head.split(b"\r\n", 1)[0]))
        if len(heads) > 1:
            body = b'{"version":"t"}'
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, heads


@pytest.mark.parametrize("disable_retry", [True, False])
async def test_dropped_connection_is_not_transparently_resent(
    monkeypatch: pytest.MonkeyPatch, disable_retry: bool
) -> None:
    """Regression: aiohttp resent a GET ~1 ms later on a new connection, unpaced and unlogged.

    ``disable_retry=False`` simulates an aiohttp that ignores the private flag:
    the per-request send guard must still refuse the second send.
    """
    if not disable_retry:
        monkeypatch.setattr(transport_module, "disable_transparent_retry", lambda _s: None)
    server, heads = await _drop_first_connection_server()
    port = server.sockets[0].getsockname()[1]
    try:
        async with ReadOnlyTransport("127.0.0.1", port, min_interval=1.0) as transport:
            with pytest.raises(RehomConnectionError) as info:
                await transport.get_alive()
            log = transport.request_log
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()
    assert [h for _t, h in heads] == [b"GET /api/alive/ HTTP/1.1"]
    assert len(log) == 1
    expected = "ServerDisconnectedError" if disable_retry else "ResendRefusedError"
    assert log[0]["error"] == expected
    assert expected in str(info.value)


async def test_a_supplied_session_is_never_modified() -> None:
    """Regression: the caller's (e.g. Home Assistant's shared) session kept its resend."""
    async with aiohttp.ClientSession() as session:
        assert session._retry_connection is True
        ReadOnlyTransport(TEST_HOST, session=session)
        assert session._retry_connection is True


async def test_a_supplied_session_still_sends_one_request_per_call() -> None:
    """The per-request guard alone refuses aiohttp's resend on a session we do not own."""
    server, heads = await _drop_first_connection_server()
    port = server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession() as session:
            transport = ReadOnlyTransport("127.0.0.1", port, session=session, min_interval=1.0)
            with pytest.raises(RehomConnectionError, match="ResendRefusedError"):
                await transport.get_alive()
            assert session._retry_connection is True
            log = transport.request_log
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()
    assert [h for _t, h in heads] == [b"GET /api/alive/ HTTP/1.1"]
    assert len(log) == 1


async def test_request_log_is_bounded(mocked: aioresponses) -> None:
    """Regression: a long-lived transport's log grew by one entry per request forever."""
    mocked.get(_url("/api/alive/"), payload={"version": "t"}, repeat=True)
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, request_log_size=3)
    for _ in range(5):
        await transport.get_alive()
    assert len(transport.request_log) == 3
    assert transport.last_request is not None
    unbounded = ReadOnlyTransport(TEST_HOST, min_interval=0, request_log_size=None)
    for _ in range(5):
        await unbounded.get_alive()
    assert len(unbounded.request_log) == 5
    for bad in (-1, 1.5, True):
        with pytest.raises(ValueError, match="request_log_size"):
            ReadOnlyTransport(TEST_HOST, request_log_size=bad)  # type: ignore[arg-type]
    await transport.close()
    await unbounded.close()


async def test_pacing_slot_shares_the_queue() -> None:
    now = [100.0]
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    transport = ReadOnlyTransport(
        TEST_HOST, min_interval=1.0, clock=lambda: now[0], sleep=fake_sleep
    )
    async with transport.pacing_slot():
        now[0] += 0.2  # the external request takes 0.2 s
    async with transport.pacing_slot():
        pass
    assert sleeps == [pytest.approx(1.0)]
    await transport.close()
