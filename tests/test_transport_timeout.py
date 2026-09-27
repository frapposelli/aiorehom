"""Per-call ``timeout`` on the typed GET methods (additive; allowlist untouched)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from aiorehom.exceptions import ForbiddenRequestError, RehomTimeoutError
from aiorehom.transport import ALLOWLIST, ReadOnlyTransport

from .conftest import BASE, TEST_HOST

TOKEN = "tok-abcdef0123456789"

Call = Callable[[ReadOnlyTransport, float | None], Awaitable[Any]]

CALLS: dict[str, tuple[str, Call]] = {
    "alive": ("/api/alive/", lambda t, x: t.get_alive(timeout=x)),
    "me": ("/api/me/", lambda t, x: t.get_me(timeout=x)),
    "config": ("/api/config/", lambda t, x: t.get_config(timeout=x)),
    "interface": ("/api/interface/", lambda t, x: t.get_interface(timeout=x)),
    "overrides": ("/api/overrides/", lambda t, x: t.get_overrides(timeout=x)),
    "plant_conf": ("/api/plant/conf/", lambda t, x: t.get_plant_conf(timeout=x)),
    "domotica": ("/api/domotica/rooms/", lambda t, x: t.get_domotica("rooms", timeout=x)),
}


@pytest.fixture
def mocked() -> Iterator[aioresponses]:
    with aioresponses() as m:
        yield m


async def _transport(mocked: aioresponses) -> ReadOnlyTransport:
    mocked.post(f"{BASE}/api/get-token/", payload={"token": TOKEN})
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0, timeout=10, allow_login=True)
    await transport.login("user", "pw")
    return transport


def _timeouts(mocked: aioresponses, path: str) -> list[aiohttp.ClientTimeout]:
    return [
        call.kwargs["timeout"]
        for (_method, url), calls in mocked.requests.items()
        if url.path == path
        for call in calls
    ]


@pytest.mark.parametrize("name", sorted(CALLS))
async def test_per_call_timeout_is_used_and_default_is_unchanged(
    mocked: aioresponses, name: str
) -> None:
    path, call = CALLS[name]
    transport = await _transport(mocked)
    mocked.get(f"{BASE}{path}", payload={"ok": 1}, repeat=True)
    await call(transport, 3.5)
    await call(transport, None)
    await transport.close()
    first, second = _timeouts(mocked, path)
    assert first.total == 3.5
    assert second.total == 10.0


async def test_history_takes_a_timeout(mocked: aioresponses) -> None:
    transport = await _transport(mocked)
    mocked.get(
        f"{BASE}/api/history/?Key=TEMP_AMBIENTE&Unita=001"
        "&Tempo__gte=2026-09-25%2000:00:00&Tempo__lte=2026-09-25%2012:00:00",
        payload=[],
    )
    await transport.get_history(
        "TEMP_AMBIENTE", "001", "2026-09-25 00:00:00", "2026-09-25 12:00:00", timeout=7
    )
    await transport.close()
    assert [t.total for t in _timeouts(mocked, "/api/history/")] == [7.0]


async def test_timeout_error_reports_the_per_call_value(mocked: aioresponses) -> None:
    transport = await _transport(mocked)
    mocked.get(f"{BASE}/api/alive/", exception=TimeoutError())
    with pytest.raises(RehomTimeoutError, match=r"GET /api/alive/: timed out after 5 s"):
        await transport.get_alive(timeout=5)
    await transport.close()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "5"])
async def test_invalid_timeout_is_refused_before_any_io(mocked: aioresponses, bad: Any) -> None:
    transport = ReadOnlyTransport(TEST_HOST, min_interval=0)
    with pytest.raises(ValueError, match="timeout"):
        await transport.get_alive(timeout=bad)
    assert mocked.requests == {}
    assert transport.request_log == []
    await transport.close()


async def test_interface_filters_still_work_and_allowlist_is_unchanged(
    mocked: aioresponses,
) -> None:
    transport = await _transport(mocked)
    mocked.get(f"{BASE}/api/interface/?Gruppo=ZONA", payload=[])
    assert await transport.get_interface(Gruppo="ZONA", timeout=20) == []
    with pytest.raises(ForbiddenRequestError):
        await transport.get_interface(Stato="1", timeout=20)
    await transport.close()
    assert [t.total for t in _timeouts(mocked, "/api/interface/")] == [20.0]
    non_get = [key for key, rule in ALLOWLIST.items() if key[0] != "GET" and not rule.write]
    assert non_get == [("POST", "/api/get-token/")]
