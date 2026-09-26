"""Static no-writes guard over ``src/aiorehom``.

Version 0.2 is the read path: the only non-GET is the existing login in ``transport.py``,
``transport.py`` is the only HTTP call site, ``websocket.py`` the only WS call
site, nothing ever sends a WS frame, and every timer goes through ``clock.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import aiorehom
from aiorehom import transport

SRC = Path(aiorehom.__file__).resolve().parent
HTTP_WRITE_ATTRS = frozenset({"request", "post", "put", "patch", "delete"})
WS_SEND_ATTRS = frozenset(
    {"send_str", "send_json", "send_bytes", "send_json_bytes", "send_frame", "ping", "pong"}
)
#: (module, attribute) references that read the system clock or sleep for real.
CLOCK_USES = frozenset(
    {
        ("asyncio", "sleep"),
        ("time", "monotonic"),
        ("time", "time"),
        ("datetime", "now"),
        ("datetime", "utcnow"),
        ("datetime", "today"),
    }
)
CLOCK_MODULES = frozenset(
    {"clock.py", "transport.py", "websocket.py", "cli.py", "probe.py", "_files.py", "replay.py"}
)


def modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=rel)


def calls(tree: ast.Module, *, methods_only: bool = False) -> Iterator[tuple[str, int]]:
    """Name of every called function/method (``x.y(...)`` -> ``y``) with its line."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                yield func.attr, node.lineno
            elif isinstance(func, ast.Name) and not methods_only:
                yield func.id, node.lineno


def dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def clock_uses(tree: ast.Module) -> Iterator[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            name = dotted(node)
            if name is None:
                continue
            parts = name.split(".")
            if len(parts) >= 2 and (parts[-2], parts[-1]) in CLOCK_USES:
                yield name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.module in ("asyncio", "time"):
            for alias in node.names:
                if (node.module, alias.name) in CLOCK_USES:
                    yield f"from {node.module} import {alias.name}", node.lineno


def test_sources_are_found() -> None:
    names = {rel for rel, _tree in modules()}
    assert {"client.py", "builder.py", "replay.py", "transport.py", "logic/zone.py"} <= names


def test_http_writes_only_in_transport() -> None:
    offenders = [
        f"{rel}:{line} .{name}("
        for rel, tree in modules()
        for name, line in calls(tree, methods_only=True)  # ".post(" etc., not a local patch()
        if name in HTTP_WRITE_ATTRS and rel != "transport.py"
    ]
    assert offenders == []


def test_ws_connect_and_sessions_only_in_their_modules() -> None:
    offenders: list[str] = []
    for rel, tree in modules():
        for name, line in calls(tree):
            if name == "ws_connect" and rel != "websocket.py":
                offenders.append(f"{rel}:{line} ws_connect(")
            if name == "ClientSession" and rel not in ("transport.py", "websocket.py"):
                offenders.append(f"{rel}:{line} ClientSession(")
    assert offenders == []


def test_nothing_ever_sends_a_ws_frame() -> None:
    offenders = [
        f"{rel}:{line} .{name}("
        for rel, tree in modules()
        for name, line in calls(tree)
        if name in WS_SEND_ATTRS
    ]
    assert offenders == []


def test_allowlist_has_exactly_one_non_get() -> None:
    non_get = [key for key in transport.ALLOWLIST if key[0] != "GET"]
    assert non_get == [("POST", "/api/get-token/")]


def test_system_clock_only_in_allowed_modules() -> None:
    offenders = [
        f"{rel}:{line} {name}"
        for rel, tree in modules()
        for name, line in clock_uses(tree)
        if rel not in CLOCK_MODULES
    ]
    assert offenders == []
    # the guard itself sees the real uses
    seen = {rel for rel, tree in modules() if any(True for _ in clock_uses(tree))}
    assert {"clock.py", "replay.py"} <= seen
    assert "client.py" not in seen
    assert not any(rel.startswith("logic/") for rel in seen)


def test_guard_detects_offences() -> None:
    tree = ast.parse(
        "import asyncio\nfrom time import monotonic\n"
        "async def f(s, ws):\n"
        "    await s.post('x')\n    await ws.send_str('x')\n    await asyncio.sleep(1)\n"
    )
    assert {name for name, _line in calls(tree)} >= {"post", "send_str", "sleep"}
    assert "patch" not in {name for name, _line in calls(ast.parse("patch(x)"), methods_only=True)}
    assert {name for name, _line in clock_uses(tree)} == {
        "asyncio.sleep",
        "from time import monotonic",
    }
