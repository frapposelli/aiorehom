"""Allowlist accept/reject matrix; rejected requests never reach aiohttp."""

from __future__ import annotations

from typing import Any
from unittest import mock

import aiohttp
import pytest

from aiorehom.exceptions import ForbiddenRequestError, RehomAuthenticationError
from aiorehom.transport import ALLOWLIST, ReadOnlyTransport, check_request, validate_host

from .conftest import TEST_HOST

HISTORY_OK = {
    "Key": "CONT_CALORIE",
    "Unita": "",
    "Tempo__gte": "2026-09-25 04:00:00",
    "Tempo__lte": "2026-09-25 10:00:00",
}


# ---------------------------------------------------------------------------
# Pure allowlist checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "query"),
    [
        ("GET", "/api/alive/", {}),
        ("GET", "/api/me/", {}),
        ("GET", "/api/overrides/", {}),
        ("GET", "/api/plant/conf/", {}),
        ("GET", "/api/config/", {}),
        ("GET", "/api/config/", None),
        ("GET", "/api/interface/", {}),
        ("GET", "/api/interface/", {"Gruppo": "ZONA", "Unita": "001"}),
        ("GET", "/api/interface/", {"Key__in": "STAGIONE,MODO"}),
        ("GET", "/api/interface/", {"SubUni": "0", "Key": "NOME"}),
        ("GET", "/api/history/", HISTORY_OK),
        ("GET", "/api/history/", {**HISTORY_OK, "Key": "TEMP_AMBIENTE", "Unita": "001"}),
        (
            "GET",
            "/api/history/",
            {
                **HISTORY_OK,
                "Tempo__gte": "2026-09-24 10:00:00",
                "Tempo__lte": "2026-09-25 10:00:00",
            },
        ),
        ("GET", "/api/domotica/object/", {}),
        ("GET", "/api/domotica/rooms/", {}),
        ("GET", "/api/domotica/scenarios/", {}),
        ("GET", "/api/domotica/config/", {}),
        ("POST", "/api/get-token/", {}),
    ],
)
def test_allowlist_accepts(method: str, path: str, query: dict[str, str] | None) -> None:
    check_request(method, path, query)


def test_auth_flags() -> None:
    assert ALLOWLIST[("GET", "/api/alive/")].auth is False
    assert ALLOWLIST[("POST", "/api/get-token/")].auth is False
    for (method, path), rule in ALLOWLIST.items():
        if path not in ("/api/alive/", "/api/get-token/"):
            assert rule.auth, (method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/interface/"),
        ("DELETE", "/api/plant/conf/"),
        ("PATCH", "/api/interface/"),
        ("OPTIONS", "/api/alive/"),
        ("HEAD", "/api/alive/"),
        ("get", "/api/alive/"),
        ("POST", "/api/interface/"),
        ("POST", "/api/interface/bulk_update/"),
        ("POST", "/api/interface/bulk_delete/"),
        ("POST", "/api/overrides/bulk_update/"),
        ("POST", "/api/plant/conf/"),
        ("POST", "/api/plant/msg/"),
        ("POST", "/api/plant/conf/import/"),
        ("POST", "/api/set_ip/"),
        ("POST", "/api/set_date/"),
        ("POST", "/api/domotica/reset/"),
        ("POST", "/api/alive/"),
        ("GET", "/api/get-token/"),
        ("GET", "/api/plant/conf/export/"),
        ("GET", "/api/plant/conf/export"),
        ("GET", "/api/history/aggregate/"),
        ("GET", "/api/domotica/schema/"),
        ("GET", "/api/domotica/knx-devices/"),
        ("GET", "/api/domotica/object/1/"),
        ("GET", "/api/"),
        ("GET", "/api"),
        ("GET", "/"),
        ("GET", ""),
        ("GET", "/www/index.html"),
        ("GET", "/ws/"),
        ("GET", "/api/help/x.png"),
        ("GET", "/api/alive"),
        ("GET", "/api/alive/x"),
        ("GET", "/api/alive//"),
        ("GET", "/api//alive/"),
        ("GET", "//api/alive/"),
        ("GET", "/api/interface/../plant/msg/"),
        ("GET", "/api/interface/./"),
        ("GET", "/api/%2e%2e/plant/msg/"),
        ("GET", "/api/interface%2F"),
        ("GET", "/api/%61live/"),
        ("GET", "/api\\alive/"),
        ("GET", "/api/alive/?reload=1"),
        ("GET", "/api/config/?reload=1"),
        ("GET", "/api/config/#frag"),
        ("GET", "/api/alive/ "),
        ("GET", " /api/alive/"),
        ("GET", "/api/alive/\n"),
        ("GET", "/api/alive/\x00"),
        ("GET", "/api/alivé/"),
        ("GET", "/API/ALIVE/"),
        ("GET", "http://evil.example/api/alive/"),
        ("GET", "//evil.example/api/alive/"),
        ("GET", "/api/alive/;x=1"),
        ("GET", "/api/alive/@evil"),
        ("GET", "/api/dynicons/fan_on.png"),
    ],
)
def test_allowlist_rejects(method: str, path: str) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_request(method, path, {})


def test_path_must_be_string() -> None:
    with pytest.raises(ForbiddenRequestError):
        check_request("GET", b"/api/alive/", {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/alive/", {"x": "1"}),
        ("/api/me/", {"format": "json"}),
        ("/api/overrides/", {"Gruppo": "PROG_OVERRIDE"}),
        ("/api/plant/conf/", {"key": "stagione"}),
        ("/api/config/", {"reload": "1"}),
        ("/api/config/", {"reload": ""}),
        ("/api/config/", {"format": "json"}),
        ("/api/domotica/config/", {"a": "b"}),
        ("/api/interface/", {"Valore": "1"}),
        ("/api/interface/", {"Gruppo": "ZONA", "reload": "1"}),
        ("/api/interface/", {"format": "json"}),
        ("/api/interface/", {"Gruppo": "ZONA&reload=1"}),
        ("/api/interface/", {"Gruppo": "ZO/NA"}),
        ("/api/interface/", {"Gruppo": "ZÖNA"}),
        ("/api/interface/", {"Gruppo": "%41"}),
        ("/api/interface/", {"Gruppo": "a" * 257}),
        ("/api/history/", {k: v for k, v in HISTORY_OK.items() if k != "Unita"}),
        ("/api/history/", {k: v for k, v in HISTORY_OK.items() if k != "Tempo__lte"}),
        ("/api/history/", {**HISTORY_OK, "operator": "sum"}),
        ("/api/history/", {**HISTORY_OK, "Gruppo": "ZONA"}),
        ("/api/history/", {}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-09-24 09:59:59"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-09-20 00:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-09-25 10:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-09-25 11:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-09-25T04:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-9-25 04:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__gte": "2026-02-30 04:00:00"}),
        ("/api/history/", {**HISTORY_OK, "Tempo__lte": "2026-09-25 10:00:00Z"}),
        ("/api/history/", {**HISTORY_OK, "Key": "cont calorie"}),
        ("/api/history/", {**HISTORY_OK, "Unita": "1"}),
    ],
)
def test_query_rejects(path: str, query: dict[str, str]) -> None:
    with pytest.raises(ForbiddenRequestError):
        check_request("GET", path, query)


def test_query_types_rejected() -> None:
    with pytest.raises(ForbiddenRequestError):
        check_request("GET", "/api/interface/", {"Gruppo": 1})  # type: ignore[dict-item]
    with pytest.raises(ForbiddenRequestError):
        check_request("GET", "/api/interface/", [("Gruppo", "ZONA")])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "host",
    ["http://x", "x/y", "x:80", "user@x", "", "a..b", "-x", "x.", "exämple", "x y", "[::1]", 5],
)
def test_invalid_hosts(host: Any) -> None:
    with pytest.raises(ValueError, match="invalid host"):
        validate_host(host)


def test_valid_hosts() -> None:
    assert validate_host("RehomServer.local") == "rehomserver.local"
    assert validate_host("198.51.100.8") == "198.51.100.8"


@pytest.mark.parametrize("port", [0, 65536, -1, True, "8000"])
def test_invalid_port(port: Any) -> None:
    with pytest.raises(ValueError, match="port"):
        ReadOnlyTransport(TEST_HOST, port)


def test_invalid_intervals() -> None:
    with pytest.raises(ValueError, match="min_interval"):
        ReadOnlyTransport(TEST_HOST, min_interval=-1)
    with pytest.raises(ValueError, match="timeout"):
        ReadOnlyTransport(TEST_HOST, timeout=0)


# ---------------------------------------------------------------------------
# No socket activity on reject (through the transport)
# ---------------------------------------------------------------------------


@pytest.fixture
def no_http() -> Any:
    """Patch aiohttp so that any request attempt fails the test."""
    with (
        mock.patch.object(aiohttp.ClientSession, "_request", autospec=True) as request,
        mock.patch.object(aiohttp.ClientSession, "_ws_connect", autospec=True) as ws_connect,
        mock.patch.object(aiohttp.TCPConnector, "connect", autospec=True) as connect,
    ):
        request.side_effect = AssertionError("HTTP request attempted")
        ws_connect.side_effect = AssertionError("WS connect attempted")
        connect.side_effect = AssertionError("socket connect attempted")
        yield request, ws_connect, connect


async def _expect_forbidden(coro: Any) -> None:
    with pytest.raises(ForbiddenRequestError):
        await coro


async def test_rejects_do_not_touch_the_network(no_http: Any) -> None:
    transport = ReadOnlyTransport(TEST_HOST, allow_login=False)
    t: Any = transport
    try:
        await _expect_forbidden(t._request("GET", "/api/plant/conf/export/"))
        await _expect_forbidden(t._request("GET", "/api/interface/../plant/msg/"))
        await _expect_forbidden(t._request("GET", "/api/%2e%2e/plant/msg/"))
        await _expect_forbidden(t._request("GET", "/api//alive/"))
        await _expect_forbidden(t._request("GET", "/api/config/", query={"reload": "1"}))
        await _expect_forbidden(t._request("POST", "/api/plant/msg/", json_body={"key": "x"}))
        await _expect_forbidden(t._request("POST", "/api/interface/bulk_update/"))
        await _expect_forbidden(t._request("DELETE", "/api/plant/conf/"))
        await _expect_forbidden(t._request("GET", "/api/alive/", json_body={"a": "b"}))
        await _expect_forbidden(t._request("GET", "/api/dynicons/x.png"))
        await _expect_forbidden(transport.get_interface(reload="1"))
        await _expect_forbidden(transport.get_interface(Gruppo=5))  # type: ignore[arg-type]
        await _expect_forbidden(transport.get_domotica("reset"))
        await _expect_forbidden(transport.get_domotica("../plant/msg"))
        await _expect_forbidden(
            transport.get_history("CONT_CALORIE", "", "2026-09-24 00:00:00", "2026-09-25 10:00:00")
        )
        await _expect_forbidden(
            transport.get_history("CONT_CALORIE", "", "2026-09-25 10:00:00", "2026-09-25 09:00:00")
        )
        await _expect_forbidden(transport.login("user", "pw"))
    finally:
        await transport.close()
    request, ws_connect, connect = no_http
    request.assert_not_called()
    ws_connect.assert_not_called()
    connect.assert_not_called()
    assert transport.request_log == []


async def test_second_login_and_bad_login_body_rejected(no_http: Any) -> None:
    transport = ReadOnlyTransport(TEST_HOST, allow_login=True)
    t: Any = transport
    await _expect_forbidden(t._request("POST", "/api/get-token/", json_body={"username": "u"}))
    t._login_attempted = True  # simulate a completed first attempt
    await _expect_forbidden(transport.login("user", "pw"))
    await transport.close()
    no_http[0].assert_not_called()


async def test_authenticated_call_without_token_does_not_send(no_http: Any) -> None:
    transport = ReadOnlyTransport(TEST_HOST)
    with pytest.raises(RehomAuthenticationError, match="not logged in"):
        await transport.get_me()
    await transport.close()
    no_http[0].assert_not_called()


def test_history_naive_datetimes_required() -> None:
    from datetime import UTC, datetime

    from aiorehom.transport import format_history_time

    assert format_history_time(datetime(2026, 9, 25, 4, 5, 6)) == "2026-09-25 04:05:06"
    with pytest.raises(ValueError, match="naive"):
        format_history_time(datetime(2026, 9, 25, tzinfo=UTC))


def test_session_with_default_auth_header_rejected() -> None:
    async def build() -> None:
        session = aiohttp.ClientSession(headers={"Authorization": "Token x"})
        try:
            with pytest.raises(ValueError, match="Authorization"):
                ReadOnlyTransport(TEST_HOST, session=session)
        finally:
            await session.close()

    import asyncio

    asyncio.run(build())


# ---------------------------------------------------------------------------
# Per-request send guard (aiohttp middleware)
# ---------------------------------------------------------------------------


async def test_send_guard_refuses_other_hops_and_second_send() -> None:
    from types import SimpleNamespace

    from yarl import URL

    from aiorehom._guard import ResendRefusedError, SingleSendGuard

    url = URL("http://rehom.test:8000/api/alive/")
    sent: list[str] = []

    async def handler(request: Any) -> Any:
        sent.append(str(request.url))
        return SimpleNamespace(status=200)

    guard = SingleSendGuard("GET", url)
    other = SimpleNamespace(method="GET", url=URL("http://rehom.test:8000/api/config/?reload=1"))
    with pytest.raises(ForbiddenRequestError, match="redirects are never followed"):
        await guard(other, handler)  # type: ignore[arg-type]
    with pytest.raises(ForbiddenRequestError):
        await guard(SimpleNamespace(method="POST", url=url), handler)  # type: ignore[arg-type]
    assert sent == []
    await guard(SimpleNamespace(method="GET", url=url), handler)  # type: ignore[arg-type]
    assert guard.sent == 1
    with pytest.raises(ResendRefusedError):
        await guard(SimpleNamespace(method="GET", url=url), handler)  # type: ignore[arg-type]
    assert sent == [str(url)]
    guard.arm()
    await guard(SimpleNamespace(method="GET", url=url), handler)  # type: ignore[arg-type]
    assert len(sent) == 2


async def test_guard_mismatch_fails_closed_through_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If aiohttp ever sent something else, the request is refused before the socket write."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from yarl import URL

    from aiorehom import transport as transport_module
    from aiorehom._guard import SingleSendGuard

    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.json_response({})

    app = web.Application()
    app.router.add_get("/api/alive/", handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    monkeypatch.setattr(
        transport_module,
        "SingleSendGuard",
        lambda method, url: SingleSendGuard(method, URL("http://127.0.0.1:1/elsewhere/")),
    )
    assert server.port is not None
    try:
        async with ReadOnlyTransport("127.0.0.1", server.port, min_interval=0) as transport:
            with pytest.raises(ForbiddenRequestError):
                await transport.get_alive()
            log = transport.request_log
    finally:
        await server.close()
    assert seen == []
    assert log[0]["error"] == "forbidden"
    assert log[0]["ok"] is False
