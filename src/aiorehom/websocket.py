"""Listen-only WebSocket capture.

The client opens ``ws://<host>:<port>/ws/`` with no subprotocol and no
client heartbeat.  Server pings are answered by aiohttp
(``autoping=True``) and a CLOSE frame is sent at shutdown; the client never
sends a data frame: the connection is wrapped in :class:`ReceiveOnlyWebSocket`,
whose send methods raise.  Every decoded frame is redacted before it reaches
the caller.

The handshake is exactly one ``GET /ws/`` per attempt: a middleware refuses
any other request (redirects are never followed, a 3xx is a handshake error)
and aiohttp's transparent resend is disabled.  Each handshake is raced
against ``stop_event`` and the deadline, and bounded by ``connect_timeout``.
An optional ``gate`` (an async context manager factory, e.g.
:meth:`ReadOnlyTransport.pacing_slot`) is held around each handshake so that it
is paced together with the HTTP requests of the same process.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import random
import time
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Final, NoReturn

import aiohttp
from yarl import URL

from ._guard import SingleSendGuard, disable_transparent_retry
from .exceptions import ForbiddenRequestError, RehomConnectionError, RehomTimeoutError
from .redact import redact_ws_frame
from .transport import validate_host

__all__ = [
    "ListenStats",
    "ReceiveOnlyConnection",
    "ReceiveOnlyWebSocket",
    "backoff_delay",
    "decode_frame",
    "listen_only",
    "open_receive_only",
]

FrameCallback = Callable[[Any], Awaitable[None] | None]
EventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
Gate = Callable[[], AbstractAsyncContextManager[Any]]

BACKOFF_BASE: Final = 1.0
BACKOFF_FACTOR: Final = 1.5
BACKOFF_CAP: Final = 30.0
#: A connection must stay up this long before the backoff counter resets.
STABLE_CONNECTION_S: Final = 10.0


def _refuse(name: str) -> NoReturn:
    raise ForbiddenRequestError(f"websocket {name}() refused: the capture is listen-only")


class ReceiveOnlyWebSocket:
    """Wrapper around an aiohttp client WebSocket that can only receive and close."""

    __slots__ = ("_ws",)

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self._ws = ws

    async def receive(self, timeout: float | None = None) -> aiohttp.WSMessage:  # noqa: ASYNC109
        return await self._ws.receive(timeout)

    async def close(self) -> bool:
        return await self._ws.close()

    @property
    def closed(self) -> bool:
        return self._ws.closed

    @property
    def close_code(self) -> int | None:
        return self._ws.close_code

    def send_str(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("send_str")

    def send_bytes(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("send_bytes")

    def send_json(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("send_json")

    def send_json_bytes(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("send_json_bytes")

    def send_frame(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("send_frame")

    def ping(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("ping")

    def pong(self, *args: object, **kwargs: object) -> NoReturn:
        _refuse("pong")

    def __getattr__(self, name: str) -> NoReturn:
        if name.startswith("send"):
            _refuse(name)
        raise AttributeError(name)


@dataclass
class ListenStats:
    """Counters for one listen-only session."""

    connects: int = 0
    disconnects: int = 0
    connect_errors: int = 0
    frames: int = 0
    non_json: int = 0
    binary: int = 0
    redacted_values: int = 0
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connects": self.connects,
            "disconnects": self.disconnects,
            "connect_errors": self.connect_errors,
            "frames": self.frames,
            "non_json": self.non_json,
            "binary": self.binary,
            "redacted_values": self.redacted_values,
            "stop_reason": self.stop_reason,
            "errors": list(self.errors),
        }


def backoff_delay(attempt: int, rng: Callable[[], float] = random.random) -> float:
    """``1 s * 1.5**attempt`` capped at 30 s, plus up to 25 % jitter."""
    base = min(BACKOFF_CAP, BACKOFF_BASE * BACKOFF_FACTOR ** max(0, attempt))
    return base + base * 0.25 * rng()


def decode_frame(data: str) -> tuple[Any, int, bool]:
    """Decode and redact one text frame: ``(frame, redacted_count, is_json)``.

    Non-JSON text is recorded only as ``{"_raw_len": n, "_non_json": true}``.
    """
    try:
        frame = json.loads(data)
    except ValueError:
        return {"_raw_len": len(data), "_non_json": True}, 0, False
    redacted, count = redact_ws_frame(frame)
    return redacted, count, True


async def _call(callback: Callable[..., Awaitable[None] | None] | None, arg: Any) -> None:
    if callback is None:
        return
    result = callback(arg)
    if inspect.isawaitable(result):
        await result


async def _wait_stop(stop_event: asyncio.Event, delay: float) -> None:
    if delay <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), delay)


async def _cancel(task: asyncio.Task[Any]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _pump(
    ws: ReceiveOnlyWebSocket,
    *,
    on_frame: FrameCallback,
    stop_event: asyncio.Event,
    deadline: float,
    clock: Callable[[], float],
    stats: ListenStats,
) -> str:
    stop_task = asyncio.ensure_future(stop_event.wait())
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                return "max_duration"
            recv_task = asyncio.ensure_future(ws.receive())
            done, _pending = await asyncio.wait(
                {recv_task, stop_task}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if recv_task not in done:
                await _cancel(recv_task)
                return "stopped" if stop_event.is_set() else "max_duration"
            msg = recv_task.result()
            if msg.type == aiohttp.WSMsgType.TEXT:
                frame, count, is_json = decode_frame(msg.data)
                stats.frames += 1
                stats.redacted_values += count
                if not is_json:
                    stats.non_json += 1
                await _call(on_frame, frame)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                stats.frames += 1
                stats.binary += 1
                await _call(on_frame, {"_binary_len": len(msg.data), "_non_json": True})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                return "error"
            elif msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                return "closed"
    finally:
        await _cancel(stop_task)


async def _race_handshake(
    handshake: Coroutine[Any, Any, aiohttp.ClientWebSocketResponse],
    stop_event: asyncio.Event,
    timeout: float,  # noqa: ASYNC109
) -> aiohttp.ClientWebSocketResponse | None:
    """Run ``handshake`` until it finishes, ``stop_event`` is set or ``timeout`` elapses.

    Returns ``None`` (after cancelling the handshake) on stop or timeout;
    exceptions of the handshake propagate.
    """
    task = asyncio.ensure_future(handshake)
    stop_task = asyncio.ensure_future(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {task, stop_task}, timeout=max(0.0, timeout), return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            return task.result()
        task.cancel()
        try:
            late = await task
        except (asyncio.CancelledError, Exception):
            return None
        await late.close()  # finished while being cancelled: do not leak it
        return None
    finally:
        await _cancel(stop_task)


def _error_event(err: BaseException) -> tuple[dict[str, Any], str]:
    """Error event and stats entry; a handshake refusal keeps its HTTP status (int only)."""
    name = type(err).__name__
    event: dict[str, Any] = {"event": "error", "error": name}
    status = err.status if isinstance(err, aiohttp.ClientResponseError) else None
    if isinstance(status, int) and not isinstance(status, bool):
        event["status"] = status
        return event, f"{name} (HTTP {status})"
    return event, name


async def listen_only(
    host: str,
    port: int,
    on_frame: FrameCallback,
    stop_event: asyncio.Event,
    max_minutes: float,
    *,
    on_event: EventCallback | None = None,
    gate: Gate | None = None,
    connect_timeout: float = 10.0,
    clock: Callable[[], float] = time.monotonic,
    rng: Callable[[], float] = random.random,
) -> ListenStats:
    """Capture ``ws://<host>:<port>/ws/`` until ``stop_event`` is set or ``max_minutes`` elapse.

    One socket at a time; reconnects with ``1 s * 1.5**n`` backoff capped at 30 s
    plus jitter.  ``on_frame`` receives each redacted frame; ``on_event``
    receives ``connect``/``disconnect``/``error``/``reconnect_wait``/
    ``connect_abandoned`` events (``error`` carries ``status`` when the
    handshake was answered with an HTTP status).  ``gate``, if given, is
    entered around every handshake (shared pacing with an HTTP transport).
    """
    host = validate_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid port")
    if max_minutes <= 0:
        raise ValueError("max_minutes must be positive")
    if connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    url = URL.build(scheme="ws", host=host, port=port, path="/ws/")
    deadline = clock() + max_minutes * 60.0
    stats = ListenStats()
    guard = SingleSendGuard("GET", url, refuse_redirects=True)
    session = aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(
            total=None, connect=connect_timeout, sock_connect=connect_timeout
        ),
        middlewares=(guard,),
    )
    disable_transparent_retry(session)

    async def handshake() -> aiohttp.ClientWebSocketResponse:
        async with contextlib.AsyncExitStack() as stack:
            if gate is not None:
                await stack.enter_async_context(gate())
            guard.arm()
            async with asyncio.timeout(connect_timeout):
                return await session.ws_connect(
                    url,
                    heartbeat=None,
                    autoping=True,
                    autoclose=True,
                    protocols=(),
                    timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5.0),
                )

    attempt = 0
    try:
        while not stop_event.is_set() and clock() < deadline:
            try:
                raw = await _race_handshake(handshake(), stop_event, deadline - clock())
                if raw is None:
                    reason = "stopped" if stop_event.is_set() else "max_duration"
                    stats.stop_reason = reason
                    await _call(on_event, {"event": "connect_abandoned", "reason": reason})
                    break
                try:
                    connected_at = clock()
                    stats.connects += 1
                    await _call(on_event, {"event": "connect", "url_path": "/ws/"})
                    reason = await _pump(
                        ReceiveOnlyWebSocket(raw),
                        on_frame=on_frame,
                        stop_event=stop_event,
                        deadline=deadline,
                        clock=clock,
                        stats=stats,
                    )
                    if clock() - connected_at >= STABLE_CONNECTION_S:
                        attempt = 0
                finally:
                    await raw.close()
                stats.disconnects += 1
                await _call(
                    on_event,
                    {"event": "disconnect", "reason": reason, "close_code": raw.close_code},
                )
                if reason in ("stopped", "max_duration"):
                    stats.stop_reason = reason
                    break
            except (aiohttp.ClientError, OSError, TimeoutError) as err:
                stats.connect_errors += 1
                event, entry = _error_event(err)
                stats.errors.append(entry)
                await _call(on_event, event)
            if stop_event.is_set() or clock() >= deadline:
                break
            delay = backoff_delay(attempt, rng)
            attempt += 1
            await _call(on_event, {"event": "reconnect_wait", "delay_s": round(delay, 3)})
            await _wait_stop(stop_event, min(delay, deadline - clock()))
        if not stats.stop_reason:
            stats.stop_reason = "stopped" if stop_event.is_set() else "max_duration"
    finally:
        await session.close()
    return stats


# ---------------------------------------------------------------------------
# One receive-only connection (the client's WS loop owns reconnects)
# ---------------------------------------------------------------------------


class ReceiveOnlyConnection:
    """One open, receive-only ``/ws/`` connection with its own private session.

    Closing it closes that session; a connector the caller lent to the session
    (``open_receive_only(connector=...)``) stays open.

    :meth:`receive` returns the next decoded, redacted JSON frame; binary and
    non-JSON text frames are skipped (and counted in :attr:`stats`); a close,
    closing, closed or error message, or a receive error, returns ``None``.
    Nothing is ever sent except aiohttp's automatic pong and the final CLOSE.
    """

    def __init__(self, session: aiohttp.ClientSession, ws: ReceiveOnlyWebSocket) -> None:
        self._session = session
        self._ws = ws
        self._closed = False
        self.stats = ListenStats(connects=1)

    @property
    def closed(self) -> bool:
        return self._closed or self._ws.closed

    @property
    def close_code(self) -> int | None:
        return self._ws.close_code

    async def receive(self) -> Any | None:
        """Next redacted JSON frame, or ``None`` once the connection has ended."""
        while not self._closed:
            try:
                msg = await self._ws.receive()
            except (aiohttp.ClientError, OSError, RuntimeError, TimeoutError) as err:
                self.stats.errors.append(type(err).__name__)
                self.stats.stop_reason = "error"
                return None
            if msg.type == aiohttp.WSMsgType.TEXT:
                frame, count, is_json = decode_frame(msg.data)
                self.stats.frames += 1
                self.stats.redacted_values += count
                if not is_json:
                    self.stats.non_json += 1
                    continue
                return frame
            if msg.type == aiohttp.WSMsgType.BINARY:
                self.stats.frames += 1
                self.stats.binary += 1
                continue
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                self.stats.stop_reason = (
                    "error" if msg.type == aiohttp.WSMsgType.ERROR else "closed"
                )
                return None
        return None

    async def close(self) -> None:
        """Close the socket and the private session.  Idempotent."""
        if self._closed:
            return
        self._closed = True
        self.stats.disconnects += 1
        if not self.stats.stop_reason:
            self.stats.stop_reason = "stopped"
        try:
            with contextlib.suppress(aiohttp.ClientError, OSError, RuntimeError, TimeoutError):
                await self._ws.close()
        finally:
            await self._session.close()


def _handshake_error(err: BaseException) -> RehomConnectionError:
    _event, entry = _error_event(err)
    return RehomConnectionError(f"GET /ws/: handshake failed ({entry})")


async def open_receive_only(
    host: str,
    port: int,
    *,
    connect_timeout: float = 10.0,
    gate: Gate | None = None,
    connector: aiohttp.BaseConnector | None = None,
) -> ReceiveOnlyConnection:
    """Open one receive-only ``ws://<host>:<port>/ws/`` connection.

    Exactly one ``GET /ws/`` is sent (no redirects, no transparent resend),
    with no subprotocol and no client heartbeat, bounded by
    ``connect_timeout`` and entered inside ``gate()`` when one is given.
    Handshake failures raise :class:`RehomConnectionError`, timeouts
    :class:`RehomTimeoutError`.

    The connection always has its own private session (cookie jar, timeouts,
    request guard).  ``connector``, if given, is the caller's connector that
    session runs on (its DNS resolver and connection limits, e.g. those of an
    injected REST session); it is borrowed, never closed.  Without it the
    session creates and owns a default connector.
    """
    host = validate_host(host)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid port")
    if connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    url = URL.build(scheme="ws", host=host, port=port, path="/ws/")
    guard = SingleSendGuard("GET", url, refuse_redirects=True)
    session = aiohttp.ClientSession(
        connector=connector,
        connector_owner=connector is None,
        cookie_jar=aiohttp.DummyCookieJar(),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(
            total=None, connect=connect_timeout, sock_connect=connect_timeout
        ),
        middlewares=(guard,),
    )
    disable_transparent_retry(session)
    try:
        async with contextlib.AsyncExitStack() as stack:
            if gate is not None:
                await stack.enter_async_context(gate())
            guard.arm()
            async with asyncio.timeout(connect_timeout):
                raw = await session.ws_connect(
                    url,
                    heartbeat=None,
                    autoping=True,
                    autoclose=True,
                    protocols=(),
                    timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=5.0),
                )
    except TimeoutError:
        await session.close()
        raise RehomTimeoutError(
            f"GET /ws/: handshake timed out after {connect_timeout:g} s"
        ) from None
    except (aiohttp.ClientError, OSError) as err:
        await session.close()
        raise _handshake_error(err) from None
    except BaseException:
        await session.close()
        raise
    return ReceiveOnlyConnection(session, ReceiveOnlyWebSocket(raw))
