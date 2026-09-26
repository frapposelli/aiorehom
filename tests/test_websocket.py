"""Listen-only WebSocket capture against a local aiohttp server on 127.0.0.1."""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from aiorehom import websocket as ws_module
from aiorehom.exceptions import ForbiddenRequestError
from aiorehom.websocket import ReceiveOnlyWebSocket, backoff_delay, decode_frame, listen_only

FRAMES: list[Any] = [
    {
        "domain": "termo",
        "type": "update",
        "gruppo": "ZONA",
        "path": "ZONA.001..TEMP_AMBIENTE",
        "value": "21.4",
    },
    {
        "domain": "termo",
        "type": "update",
        "gruppo": "X",
        "path": "X...API_TOKEN",
        "value": "secret-value-1",
    },
    {"domain": "bus", "type": "update", "key": "user_message", "value": ""},
]


@dataclass
class ServerLog:
    connections: int = 0
    client_messages: list[aiohttp.WSMsgType] = field(default_factory=list)
    client_data: list[Any] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)
    protocols: list[str | None] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


async def _start(handler: Handler, alive: bool = False) -> TestServer:
    app = web.Application()
    app.router.add_get("/ws/", handler)
    if alive:

        async def alive_handler(request: web.Request) -> web.Response:
            return web.json_response({"version": "t"})

        app.router.add_get("/api/alive/", alive_handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server


@pytest.fixture
async def frame_server() -> AsyncIterator[tuple[TestServer, ServerLog]]:
    log = ServerLog()

    async def handler(request: web.Request) -> web.StreamResponse:
        log.connections += 1
        log.headers.append(dict(request.headers))
        ws = web.WebSocketResponse(autoping=False)
        await ws.prepare(request)
        log.protocols.append(ws.ws_protocol)
        for frame in FRAMES:
            await ws.send_str(json.dumps(frame))
        await ws.send_str("not json at all")
        await ws.send_bytes(b"\x00\x01")
        await ws.ping(b"server-ping")
        async for msg in ws:
            log.client_messages.append(msg.type)
            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                log.client_data.append(msg.data)
        log.done.set()
        return ws

    server = await _start(handler, alive=True)
    yield server, log
    await server.close()


async def test_listen_only_receives_redacts_and_never_sends(
    frame_server: tuple[TestServer, ServerLog],
) -> None:
    server, log = frame_server
    received: list[Any] = []
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()

    async def on_frame(frame: Any) -> None:
        received.append(frame)
        if len(received) == 5:
            await asyncio.sleep(0.2)  # leave time for the ping/pong round trip
            stop.set()

    assert server.port is not None
    stats = await listen_only("127.0.0.1", server.port, on_frame, stop, 0.5, on_event=events.append)
    await asyncio.wait_for(log.done.wait(), 2)
    assert stats.stop_reason == "stopped"
    assert stats.frames == 5
    assert stats.non_json == 1
    assert stats.binary == 1
    assert stats.redacted_values == 1
    assert received[0] == FRAMES[0]
    assert received[1]["value"] == "<redacted len=14>"
    assert received[2] == FRAMES[2]
    assert received[3] == {"_raw_len": len("not json at all"), "_non_json": True}
    assert received[4] == {"_binary_len": 2, "_non_json": True}
    assert "secret-value-1" not in json.dumps(received)
    # the client answered the server ping (autoping) and sent nothing else but CLOSE
    assert log.client_data == []
    assert set(log.client_messages) <= {aiohttp.WSMsgType.PONG, aiohttp.WSMsgType.CLOSE}
    assert aiohttp.WSMsgType.PONG in log.client_messages
    # no cookies, no subprotocol
    headers = {k.lower(): v for k, v in log.headers[0].items()}
    assert "cookie" not in headers
    assert "sec-websocket-protocol" not in headers
    assert log.protocols == [None]
    assert [e["event"] for e in events] == ["connect", "disconnect"]
    assert events[1]["reason"] == "stopped"
    assert stats.to_dict()["connects"] == 1


async def test_reconnects_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = [0]

    async def handler(request: web.Request) -> web.StreamResponse:
        connections[0] += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"n": connections[0]}))
        await ws.close()
        return ws

    server = await _start(handler)
    monkeypatch.setattr(ws_module, "BACKOFF_BASE", 0.01)
    received: list[Any] = []
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()

    def on_frame(frame: Any) -> None:
        received.append(frame)
        if len(received) == 3:
            stop.set()

    assert server.port is not None
    stats = await listen_only(
        "127.0.0.1", server.port, on_frame, stop, 0.5, on_event=events.append, rng=lambda: 0.0
    )
    await server.close()
    assert received[:3] == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert stats.connects >= 3
    waits = [e["delay_s"] for e in events if e["event"] == "reconnect_wait"]
    assert waits[:2] == [0.01, 0.015]
    assert any(e.get("reason") == "closed" for e in events)


async def test_connection_refused_is_retried_until_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(ws_module, "BACKOFF_BASE", 0.01)
    events: list[dict[str, Any]] = []
    stats = await listen_only(
        "127.0.0.1", port, lambda _f: None, asyncio.Event(), 0.005, on_event=events.append
    )
    assert stats.connects == 0
    assert stats.connect_errors >= 1
    assert stats.stop_reason == "max_duration"
    assert any(e["event"] == "error" for e in events)


async def test_max_duration_while_connected(frame_server: tuple[TestServer, ServerLog]) -> None:
    server, _log = frame_server
    assert server.port is not None
    stats = await listen_only("127.0.0.1", server.port, lambda _f: None, asyncio.Event(), 0.01)
    assert stats.stop_reason == "max_duration"
    assert stats.connects == 1


async def test_stop_before_start_and_argument_validation() -> None:
    stop = asyncio.Event()
    stop.set()
    stats = await listen_only("127.0.0.1", 9, lambda _f: None, stop, 1)
    assert stats.connects == 0
    assert stats.stop_reason == "stopped"
    with pytest.raises(ValueError, match="host"):
        await listen_only("ws://x", 1, lambda _f: None, stop, 1)
    with pytest.raises(ValueError, match="port"):
        await listen_only("127.0.0.1", 0, lambda _f: None, stop, 1)
    with pytest.raises(ValueError, match="max_minutes"):
        await listen_only("127.0.0.1", 1, lambda _f: None, stop, 0)


async def test_wrapper_refuses_every_send() -> None:
    class Dummy:
        closed = False
        close_code = None

        async def receive(self, timeout: float | None = None) -> str:  # noqa: ASYNC109
            return "msg"

        async def close(self) -> bool:
            return True

    wrapper = ReceiveOnlyWebSocket(Dummy())  # type: ignore[arg-type]
    for name in (
        "send_str",
        "send_bytes",
        "send_json",
        "send_json_bytes",
        "send_frame",
        "ping",
        "pong",
    ):
        with pytest.raises(ForbiddenRequestError, match="listen-only"):
            getattr(wrapper, name)("x")
    with pytest.raises(ForbiddenRequestError):
        wrapper.send_anything_else  # noqa: B018
    with pytest.raises(AttributeError):
        wrapper.unrelated  # noqa: B018
    assert await wrapper.receive() == "msg"  # type: ignore[comparison-overlap]
    assert await wrapper.close() is True
    assert wrapper.closed is False
    assert wrapper.close_code is None


def test_backoff_delay() -> None:
    assert backoff_delay(0, lambda: 0.0) == 1.0
    assert backoff_delay(1, lambda: 0.0) == 1.5
    assert backoff_delay(2, lambda: 0.0) == 2.25
    assert backoff_delay(50, lambda: 0.0) == 30.0
    assert backoff_delay(50, lambda: 1.0) == 37.5
    assert 1.0 <= backoff_delay(0) <= 1.25


def test_decode_frame() -> None:
    frame, count, is_json = decode_frame('{"domain":"bus","key":"api_key","value":"v"}')
    assert frame["value"] == "<redacted len=1>"
    assert (count, is_json) == (1, True)
    assert decode_frame("x") == ({"_raw_len": 1, "_non_json": True}, 0, False)


# ---------------------------------------------------------------------------
# Regressions: one GET /ws/ per attempt, bounded handshake, status kept
# ---------------------------------------------------------------------------


async def _recording_server(
    ws_status: int | None = None, location: str | None = None
) -> tuple[TestServer, list[str]]:
    """Server recording every request line; /ws/ answers ``ws_status`` (+ Location)."""
    seen: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen.append(f"{request.method} {request.path_qs}")
        if request.path == "/ws/" and ws_status is not None:
            headers = {"Location": location} if location is not None else None
            return web.json_response({"detail": "no"}, status=ws_status, headers=headers)
        return web.json_response({"oops": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app, host="127.0.0.1", port=0)
    await server.start_server()
    return server, seen


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_ws_redirect_to_other_port_is_never_followed(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Regression: ws_connect followed redirects (to other hosts/ports, forbidden paths)."""
    target, target_seen = await _recording_server()
    assert target.port is not None
    source, source_seen = await _recording_server(
        status, f"http://127.0.0.1:{target.port}/api/config/?reload=1"
    )
    monkeypatch.setattr(ws_module, "BACKOFF_BASE", 0.05)
    events: list[dict[str, Any]] = []
    assert source.port is not None
    try:
        stats = await listen_only(
            "127.0.0.1",
            source.port,
            lambda _f: None,
            asyncio.Event(),
            0.005,
            on_event=events.append,
        )
    finally:
        await source.close()
        await target.close()
    assert target_seen == []
    assert source_seen  # at least one attempt
    assert set(source_seen) == {"GET /ws/"}  # never a second hop on the same server
    attempts = [e for e in events if e["event"] == "error"]
    assert len(attempts) == len(source_seen)  # exactly one request per attempt
    assert all(e["status"] == status for e in attempts)
    assert stats.errors[0] == f"WSServerHandshakeError (HTTP {status})"
    assert stats.connects == 0


async def test_ws_same_host_redirect_to_forbidden_path_not_followed() -> None:
    server, seen = await _recording_server(302, "/api/plant/conf/export/")
    assert server.port is not None
    try:
        await listen_only("127.0.0.1", server.port, lambda _f: None, asyncio.Event(), 0.001)
    finally:
        await server.close()
    assert seen == ["GET /ws/"]


async def test_ws_handshake_refusal_keeps_http_status() -> None:
    """Regression: a 401/403 on the upgrade was recorded as the class name only."""
    server, seen = await _recording_server(403)
    events: list[dict[str, Any]] = []
    assert server.port is not None
    try:
        stats = await listen_only(
            "127.0.0.1",
            server.port,
            lambda _f: None,
            asyncio.Event(),
            0.001,
            on_event=events.append,
        )
    finally:
        await server.close()
    assert seen == ["GET /ws/"]
    assert events[0] == {"event": "error", "error": "WSServerHandshakeError", "status": 403}
    assert stats.errors == ["WSServerHandshakeError (HTTP 403)"]
    assert "no" not in json.dumps(events)  # no body, no headers


async def _drop_first_connection_server() -> tuple[asyncio.Server, list[bytes]]:
    """Raw server: reads a request head, then closes without answering."""
    heads: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        heads.append(head.split(b"\r\n", 1)[0])
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, heads


@pytest.mark.parametrize("disable_retry", [True, False])
async def test_ws_handshake_is_not_transparently_resent(
    monkeypatch: pytest.MonkeyPatch, disable_retry: bool
) -> None:
    """Regression: aiohttp resent the handshake at once after a dropped connection.

    ``disable_retry=False`` simulates an aiohttp that ignores the private flag:
    the send guard must still refuse the second send.
    """
    if not disable_retry:
        monkeypatch.setattr(ws_module, "disable_transparent_retry", lambda _s: None)
    monkeypatch.setattr(ws_module, "BACKOFF_BASE", 60.0)  # one attempt only
    server, heads = await _drop_first_connection_server()
    port = server.sockets[0].getsockname()[1]
    events: list[dict[str, Any]] = []
    try:
        stats = await listen_only(
            "127.0.0.1", port, lambda _f: None, asyncio.Event(), 0.005, on_event=events.append
        )
        await asyncio.sleep(0.05)
    finally:
        server.close()
        await server.wait_closed()
    assert heads == [b"GET /ws/ HTTP/1.1"]
    assert stats.connect_errors == 1
    expected = "ServerDisconnectedError" if disable_retry else "ResendRefusedError"
    assert stats.errors == [expected]


async def test_ws_handshake_that_never_answers_honours_stop_and_deadline() -> None:
    """Regression: a peer that accepts TCP but never answers blocked stop and max_minutes."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)  # never accept()ed: the kernel completes the TCP handshake
        port = listener.getsockname()[1]
        stop = asyncio.Event()
        task = asyncio.create_task(
            listen_only("127.0.0.1", port, lambda _f: None, stop, 10, connect_timeout=30)
        )
        await asyncio.sleep(0.3)
        assert not task.done()
        stop.set()  # what SIGINT does in `watch`
        stats = await asyncio.wait_for(task, 2)
        assert stats.stop_reason == "stopped"
        assert stats.connects == 0

        loop = asyncio.get_running_loop()
        start = loop.time()
        stats = await asyncio.wait_for(
            listen_only(
                "127.0.0.1", port, lambda _f: None, asyncio.Event(), 0.005, connect_timeout=30
            ),
            2,
        )
        assert stats.stop_reason == "max_duration"
        assert loop.time() - start < 1.5


async def test_ws_handshake_timeout_is_an_error_then_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ws_module, "BACKOFF_BASE", 60.0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        events: list[dict[str, Any]] = []
        stats = await asyncio.wait_for(
            listen_only(
                "127.0.0.1",
                port,
                lambda _f: None,
                asyncio.Event(),
                0.01,
                on_event=events.append,
                connect_timeout=0.2,
            ),
            3,
        )
    assert stats.errors == ["TimeoutError"]
    assert [e["event"] for e in events] == ["error", "reconnect_wait"]
    assert stats.stop_reason == "max_duration"
    with pytest.raises(ValueError, match="connect_timeout"):
        await listen_only("127.0.0.1", port, lambda _f: None, asyncio.Event(), 1, connect_timeout=0)


async def test_gate_is_held_around_each_handshake(
    frame_server: tuple[TestServer, ServerLog],
) -> None:
    server, _log = frame_server
    entered: list[str] = []

    @contextlib.asynccontextmanager
    async def gate() -> AsyncIterator[None]:
        entered.append("in")
        yield
        entered.append("out")

    assert server.port is not None
    stats = await listen_only(
        "127.0.0.1", server.port, lambda _f: None, asyncio.Event(), 0.005, gate=gate
    )
    assert stats.connects == 1
    assert entered == ["in", "out"]
