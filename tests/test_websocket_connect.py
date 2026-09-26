"""``open_receive_only``/``ReceiveOnlyConnection`` against a loopback aiohttp server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer

from aiorehom import websocket as ws_module
from aiorehom.exceptions import ForbiddenRequestError, RehomConnectionError, RehomTimeoutError
from aiorehom.websocket import ReceiveOnlyConnection, open_receive_only

FRAMES: list[Any] = [
    {"domain": "termo", "type": "update", "gruppo": "ZONA", "path": "ZONA.001..TEMP_AMBIENTE",
     "value": "21.4"},
    {"domain": "termo", "type": "update", "gruppo": "X", "path": "X...API_TOKEN",
     "value": "secret-value-000001"},
    {"domain": "bus", "type": "update", "key": "user_message", "value": ""},
]  # fmt: skip


class Log:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.client_messages: list[aiohttp.WSMsgType] = []
        self.client_data: list[Any] = []
        self.headers: list[dict[str, str]] = []
        self.done = asyncio.Event()


async def _server(log: Log, *, close_after: bool = True) -> TestServer:
    async def handler(request: web.Request) -> web.StreamResponse:
        log.requests.append(f"{request.method} {request.path_qs}")
        log.headers.append(dict(request.headers))
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        await ws.send_str(json.dumps(FRAMES[0]))
        await ws.send_bytes(b"\x00\x01")
        await ws.send_str("not json at all")
        await ws.send_str(json.dumps(FRAMES[1]))
        await ws.ping(b"server-ping")
        await ws.send_str(json.dumps(FRAMES[2]))
        if close_after:
            await ws.close()
        async for msg in ws:
            log.client_messages.append(msg.type)
            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                log.client_data.append(msg.data)
        log.done.set()
        return ws

    app = web.Application()
    app.router.add_get("/ws/", handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server


@pytest.fixture
async def served() -> AsyncIterator[tuple[TestServer, Log]]:
    log = Log()
    server = await _server(log)
    yield server, log
    await server.close()


async def test_receives_redacted_frames_skips_binary_and_non_json_and_ends(
    served: tuple[TestServer, Log],
) -> None:
    server, log = served
    assert server.port is not None
    conn = await open_receive_only("127.0.0.1", server.port, connect_timeout=5)
    assert isinstance(conn, ReceiveOnlyConnection)
    received = [await conn.receive() for _ in range(3)]
    assert received[0] == FRAMES[0]
    assert received[1]["value"] == "<redacted len=19>"
    assert received[2] == FRAMES[2]
    assert await conn.receive() is None  # server closed
    assert await conn.receive() is None
    assert conn.stats.frames == 5
    assert conn.stats.binary == 1
    assert conn.stats.non_json == 1
    assert conn.stats.redacted_values == 1
    assert conn.stats.stop_reason == "closed"
    assert conn.closed
    await conn.close()
    await conn.close()  # idempotent
    await asyncio.wait_for(log.done.wait(), 2)
    assert log.requests == ["GET /ws/"]
    headers = {k.lower() for k in log.headers[0]}
    assert "cookie" not in headers
    assert "sec-websocket-protocol" not in headers
    # the client sent no data frame (only the automatic pong and the close)
    assert log.client_data == []
    assert set(log.client_messages) <= {aiohttp.WSMsgType.PONG, aiohttp.WSMsgType.CLOSE}
    assert "not-a-real-password" not in json.dumps(received)


async def test_close_while_open_and_send_methods_refuse() -> None:
    log = Log()
    server = await _server(log, close_after=False)
    assert server.port is not None
    try:
        conn = await open_receive_only("127.0.0.1", server.port)
        assert await conn.receive() == FRAMES[0]
        for name in ("send_str", "send_bytes", "send_json", "send_frame", "ping", "pong"):
            assert not hasattr(conn, name)
            with pytest.raises(ForbiddenRequestError, match="listen-only"):
                getattr(conn._ws, name)("x")
        await conn.close()
        assert conn.closed
        assert conn.stats.stop_reason == "stopped"
        assert conn.stats.disconnects == 1
        assert await conn.receive() is None
        await asyncio.wait_for(log.done.wait(), 2)
    finally:
        await server.close()
    assert log.client_data == []
    # the server's iterator ended on our CLOSE (log.done); only the auto-pong came before it
    assert set(log.client_messages) <= {aiohttp.WSMsgType.PONG}


async def test_receive_errors_end_the_connection() -> None:
    class Broken:
        closed = False
        close_code = None

        async def receive(self) -> aiohttp.WSMessage:
            raise aiohttp.ClientConnectionError("boom")

        async def close(self) -> bool:
            raise aiohttp.ClientConnectionError("close failed")

    class Session:
        closed = 0

        async def close(self) -> None:
            Session.closed += 1

    conn = ReceiveOnlyConnection(Session(), Broken())  # type: ignore[arg-type]
    assert await conn.receive() is None
    assert conn.stats.errors == ["ClientConnectionError"]
    assert conn.stats.stop_reason == "error"
    await conn.close()  # the close error is swallowed, the session still closed
    assert Session.closed == 1
    assert conn.close_code is None


async def test_error_message_ends_the_connection() -> None:
    class Erroring:
        closed = False
        close_code = 1006

        async def receive(self) -> aiohttp.WSMessage:
            return aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, ValueError("x"), None)

        async def close(self) -> bool:
            return True

    class Session:
        async def close(self) -> None:
            return None

    conn = ReceiveOnlyConnection(Session(), Erroring())  # type: ignore[arg-type]
    assert await conn.receive() is None
    assert conn.stats.stop_reason == "error"
    assert conn.close_code == 1006


async def _recording_server(
    status: int | None = None, location: str | None = None
) -> tuple[TestServer, list[str]]:
    seen: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(f"{request.method} {request.path_qs}")
        if request.path == "/ws/" and status is not None:
            headers = {"Location": location} if location is not None else None
            return web.json_response({"detail": "no"}, status=status, headers=headers)
        return web.json_response({"oops": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server, seen


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_redirect_is_refused_and_never_followed(status: int) -> None:
    target, target_seen = await _recording_server()
    assert target.port is not None
    source, source_seen = await _recording_server(
        status, f"http://127.0.0.1:{target.port}/api/config/?reload=1"
    )
    assert source.port is not None
    try:
        with pytest.raises(RehomConnectionError, match=f"HTTP {status}"):
            await open_receive_only("127.0.0.1", source.port)
    finally:
        await source.close()
        await target.close()
    assert source_seen == ["GET /ws/"]
    assert target_seen == []


async def test_handshake_refusal_is_a_connection_error_without_body() -> None:
    server, seen = await _recording_server(403)
    assert server.port is not None
    try:
        with pytest.raises(RehomConnectionError) as info:
            await open_receive_only("127.0.0.1", server.port)
    finally:
        await server.close()
    assert seen == ["GET /ws/"]
    assert "HTTP 403" in str(info.value)
    assert "detail" not in str(info.value)
    assert not isinstance(info.value, RehomTimeoutError)


@pytest.mark.parametrize("disable_retry", [True, False])
async def test_exactly_one_get_per_attempt(
    monkeypatch: pytest.MonkeyPatch, disable_retry: bool
) -> None:
    if not disable_retry:
        monkeypatch.setattr(ws_module, "disable_transparent_retry", lambda _s: None)
    heads: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head.split(b"\r\n", 1)[0])
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        with pytest.raises(RehomConnectionError):
            await open_receive_only("127.0.0.1", port)
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()
    assert heads == [b"GET /ws/ HTTP/1.1"]


async def test_handshake_timeout_and_refused_connection() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)  # never accept()ed
        port = listener.getsockname()[1]
        with pytest.raises(RehomTimeoutError, match=r"timed out after 0\.2 s"):
            await asyncio.wait_for(open_receive_only("127.0.0.1", port, connect_timeout=0.2), 3)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    with pytest.raises(RehomConnectionError, match="handshake failed"):
        await open_receive_only("127.0.0.1", closed_port)


async def test_gate_is_held_around_the_handshake(served: tuple[TestServer, Log]) -> None:
    server, _log = served
    entered: list[str] = []

    @contextlib.asynccontextmanager
    async def gate() -> AsyncIterator[None]:
        entered.append("in")
        yield
        entered.append("out")

    assert server.port is not None
    conn = await open_receive_only("127.0.0.1", server.port, gate=gate)
    assert entered == ["in", "out"]
    await conn.close()


async def test_cancellation_and_bugs_close_the_private_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[aiohttp.ClientSession] = []
    real_session = aiohttp.ClientSession

    def tracking_session(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = real_session(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(ws_module.aiohttp, "ClientSession", tracking_session)

    @contextlib.asynccontextmanager
    async def failing_gate() -> AsyncIterator[None]:
        raise ForbiddenRequestError("gate refused (test)")
        yield  # pragma: no cover

    with pytest.raises(ForbiddenRequestError):
        await open_receive_only("127.0.0.1", 1, gate=failing_gate)
    assert sessions[-1].closed


async def test_argument_validation() -> None:
    with pytest.raises(ValueError, match="host"):
        await open_receive_only("ws://x", 1)
    with pytest.raises(ValueError, match="port"):
        await open_receive_only("127.0.0.1", 0)
    with pytest.raises(ValueError, match="port"):
        await open_receive_only("127.0.0.1", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="connect_timeout"):
        await open_receive_only("127.0.0.1", 1, connect_timeout=0)


class _NameResolver(AbstractResolver):
    """Resolves only the names it was given (like a caller's mDNS-capable resolver)."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.lookups: list[str] = []

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        self.lookups.append(host)
        return [
            ResolveResult(
                hostname=host,
                host=self.names[host],
                port=port,
                family=socket.AF_INET,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
        ]

    async def close(self) -> None:
        return None


async def test_borrowed_connector_resolves_the_host_and_stays_open(
    served: tuple[TestServer, Log],
) -> None:
    """The private session runs on the caller's connector: its resolver, never closed."""
    server, log = served
    assert server.port is not None
    resolver = _NameResolver({"rehomserver.local": "127.0.0.1"})
    connector = aiohttp.TCPConnector(resolver=resolver)
    try:
        conn = await open_receive_only("rehomserver.local", server.port, connector=connector)
        assert await conn.receive() == FRAMES[0]
        await conn.close()
        assert resolver.lookups == ["rehomserver.local"]
        assert log.requests == ["GET /ws/"]
        assert not connector.closed

        # A failed handshake closes the private session, not the borrowed connector.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            closed_port = sock.getsockname()[1]
        with pytest.raises(RehomConnectionError, match="handshake failed"):
            await open_receive_only("rehomserver.local", closed_port, connector=connector)
        assert not connector.closed
    finally:
        await connector.close()


async def test_default_connector_is_owned_by_the_private_session(
    served: tuple[TestServer, Log], monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions: list[aiohttp.ClientSession] = []
    real_session = aiohttp.ClientSession

    def tracking_session(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = real_session(*args, **kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(ws_module.aiohttp, "ClientSession", tracking_session)
    server, _log = served
    assert server.port is not None
    conn = await open_receive_only("127.0.0.1", server.port)
    connector = sessions[-1].connector
    assert connector is not None
    assert sessions[-1].connector_owner
    await conn.close()
    assert connector.closed
