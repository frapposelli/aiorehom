"""Static no-writes guard over ``src/aiorehom``.

The non-GETs on the allowlist are the login and the two gated bulk writes
(refused unless the transport was created with ``allow_writes=True``), and
``.post_bulk_update(`` is called only from ``RehomClient._execute`` in
``client.py``.  ``transport.py`` is the only HTTP call site, ``websocket.py``
the only WS call site, nothing ever sends a WS frame, and every timer goes
through ``clock.py``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import aiorehom
from aiorehom import transport

SRC = Path(aiorehom.__file__).resolve().parent
HTTP_WRITE_ATTRS = frozenset({"request", "post", "put", "patch", "delete"})
#: The one place a bulk write may be sent from: (module, enclosing function).
BULK_WRITE_CALLER = ("client.py", "_execute")
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


def attribute_uses(tree: ast.Module, attr: str) -> Iterator[tuple[str, bool, int]]:
    """Every ``x.<attr>`` reference: (innermost enclosing function, is it called, line)."""

    def visit(node: ast.AST, scope: str, called: set[int]) -> Iterator[tuple[str, bool, int]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from visit(child, child.name, called)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                called.add(id(child.func))
            if isinstance(child, ast.Attribute) and child.attr == attr:
                yield scope, id(child) in called, child.lineno
            yield from visit(child, scope, called)

    yield from visit(tree, "<module>", set())


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


def test_bulk_writes_only_from_client_execute() -> None:
    uses = [
        (rel, scope, called, line)
        for rel, tree in modules()
        for scope, called, line in attribute_uses(tree, "post_bulk_update")
    ]
    offenders = [
        f"{rel}:{line} {scope}: .post_bulk_update{'(' if called else ''}"
        for rel, scope, called, line in uses
        if (rel, scope) != BULK_WRITE_CALLER or not called
    ]
    assert offenders == []
    # the guard itself sees the real call site
    assert [(rel, scope) for rel, scope, _called, _line in uses] == [BULK_WRITE_CALLER]


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


def test_allowlist_non_get_is_login_plus_two_gated_writes() -> None:
    non_get = {key: rule for key, rule in transport.ALLOWLIST.items() if key[0] != "GET"}
    assert set(non_get) == {
        ("POST", "/api/get-token/"),
        ("POST", "/api/interface/bulk_update/"),
        ("POST", "/api/overrides/bulk_update/"),
    }
    assert not non_get[("POST", "/api/get-token/")].write
    assert non_get[("POST", "/api/interface/bulk_update/")].write
    assert non_get[("POST", "/api/overrides/bulk_update/")].write


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
    writes = ast.parse(
        "async def _execute(t):\n    await t.post_bulk_update('p', [])\n"
        "async def other(t):\n    send = t.post_bulk_update\n"
        "    async def inner():\n        await t.post_bulk_update('p', [])\n"
        "t.post_bulk_update('p', [])\n"
    )
    assert sorted(attribute_uses(writes, "post_bulk_update")) == [
        ("<module>", True, 7),
        ("_execute", True, 2),
        ("inner", True, 6),
        ("other", False, 4),
    ]
