"""In-test fakes for the sync engine and the client (builder B).

* :class:`FakeTransport` implements exactly the ``ReadTransport`` protocol (no
  other attribute exists, so any other call fails the test), with per-method
  virtual latency, failure injection and a call log.
* :class:`FakeWsConnector` / :class:`FakeWsConnection` hand frames pushed by the
  test to the client.  A connection has no send method at all.
* :class:`FakeBuilder` builds a small but complete :class:`RehomState` from the
  stores (numeric values parsed, so semantically equal stores give equal
  states) with a controllable ``next_change_at``.

All data here is synthetic.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo

from aiorehom.client import ClientOptions, RehomClient
from aiorehom.clock import DeviceClock, VirtualClock
from aiorehom.enums import LockState, ScheduleSource
from aiorehom.models import (
    Forecast,
    ForecastEntry,
    Health,
    Hub,
    Plant,
    RehomState,
    SeasonSchedule,
    StateUpdate,
    UnitIdentity,
    Zone,
    ZoneSchedule,
)
from aiorehom.state import StoreView
from aiorehom.values import parse_bool01, parse_float

from .conftest import rec

T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
ROME = ZoneInfo("Europe/Rome")
AUTO_LOCAL_TIME = "<auto>"


def interface_rows() -> list[dict[str, Any]]:
    """A small synthetic interface snapshot."""
    return [
        rec("REHOM", "", "", "MODO", "1"),
        rec("REHOM", "", "", "SET_POINT", "3"),
        rec("REHOM", "", "", "TEMP_COM", "24"),
        rec("REHOM", "", "", "SET_POINT_TEMP", "26"),
        rec("REHOM", "", "", "VER_SOFT", "3.1.0.12"),
        rec("REHOM", "", "", "WEBSERVER", "1"),
        rec("ZONA", "001", "", "NOME", "Zona 001"),
        rec("ZONA", "001", "", "TEMP_AMBIENTE", "21.5"),
        rec("ZONA", "001", "", "ATTIVA", "0"),
        rec("ZONA", "002", "", "NOME", "Zona 002"),
        rec("ZONA", "002", "", "TEMP_AMBIENTE", "22"),
        rec("ZONA", "002", "", "ATTIVA", "1"),
        rec("PROC", "", "", "WATCHDOG_MASTER", "1"),
        rec("METEO", "", "", "DATA", "2026-09-25 12:00:00"),
        rec("METEO", "0", "", "VENTO", "3.5,180"),
        rec("DEUM", "001", "", "ST_MODE", "1"),
        rec("DEUM", "1", "", "ST_MODE", "0"),
        rec("UTEN", "", "", "someone", "not-a-real-password"),
    ]


def config_payload() -> dict[str, Any]:
    return {
        "TIMEZONE": "Europe/Rome",
        "LOCAL_TIME": AUTO_LOCAL_TIME,
        "VERSION": "3.16.3",
        "BOARD": "pi",
        "METEO_KEY": "0123456789abcdef",
    }


def termo(path: str, value: Any = None, *, kind: str = "update", **extra: Any) -> dict[str, Any]:
    """A ``termo`` frame; ``gruppo`` defaults to the path prefix."""
    frame: dict[str, Any] = {"domain": "termo", "type": kind, "path": path}
    frame["gruppo"] = extra.pop("gruppo", path.split(".", 1)[0])
    if kind == "update":
        frame["value"] = value
    frame.update(extra)
    return frame


def bus(key: str, value: Any = None, *, kind: str = "update") -> dict[str, Any]:
    frame: dict[str, Any] = {"domain": "bus", "type": kind, "key": key}
    if kind == "update":
        frame["value"] = value
    return frame


class FakeTransport:
    """``ReadTransport`` over in-memory payloads, timed on a :class:`VirtualClock`."""

    def __init__(
        self,
        clock: VirtualClock,
        *,
        latency: dict[str, float] | None = None,
        has_token: bool = False,
        log: list[str] | None = None,
    ) -> None:
        self.clock = clock
        self.interface: Any = interface_rows()
        self.overrides: Any = []
        self.plant_conf: Any = {"stagione": "1", "CONFIGURA_ON": "0"}
        self.config: Any = config_payload()
        self.alive: Any = {"version": "3.16.3"}
        self.skew = timedelta(0)
        self.latency: dict[str, float] = {
            "get_alive": 0.1,
            "get_interface": 2.0,
            "get_overrides": 0.2,
            "get_plant_conf": 0.2,
            "get_config": 0.2,
            "get_history": 0.5,
        }
        if latency is not None:
            self.latency.update(latency)
        self.failures: dict[str, list[BaseException]] = {}
        self.calls: list[tuple[str, float | None]] = []
        self.call_times: list[tuple[str, datetime]] = []
        self.logins: list[tuple[str, str]] = []
        self.history: Callable[[str, str, str, str], Any] = lambda *_a: []
        self.history_calls: list[tuple[str, str, str, str, float]] = []
        self.closed = 0
        self.log = log if log is not None else []
        self._token = has_token

    def fail(self, method: str, *errors: BaseException) -> None:
        self.failures.setdefault(method, []).extend(errors)

    def names(self) -> list[str]:
        return [name for name, _timeout in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for called, _timeout in self.calls if called == name)

    @property
    def has_token(self) -> bool:
        return self._token

    async def login(self, username: str, password: str) -> None:
        self.calls.append(("login", None))
        self.log.append("login")
        self.logins.append((username, password))
        await self._fail_or_wait("login")
        self._token = True

    async def _fail_or_wait(self, name: str) -> None:
        delay = self.latency.get(name, 0.0)
        if delay:
            await self.clock.sleep(delay)
        pending = self.failures.get(name)
        if pending:
            raise pending.pop(0)

    async def _get(
        self,
        name: str,
        timeout: float | None,  # noqa: ASYNC109 - recorded, as the real transport's
        value: Callable[[], Any],
    ) -> Any:
        self.calls.append((name, timeout))
        self.call_times.append((name, self.clock.utcnow()))
        self.log.append(name)
        await self._fail_or_wait(name)
        return copy.deepcopy(value())

    def _config(self) -> Any:
        config = copy.deepcopy(self.config)
        if isinstance(config, dict) and config.get("LOCAL_TIME") == AUTO_LOCAL_TIME:
            local = (self.clock.utcnow() + self.skew).astimezone(ROME)
            config["LOCAL_TIME"] = local.strftime("%Y-%m-%d %H:%M:%S")
        return config

    async def get_alive(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return await self._get("get_alive", timeout, lambda: self.alive)

    async def get_config(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return await self._get("get_config", timeout, self._config)

    async def get_interface(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return await self._get("get_interface", timeout, lambda: self.interface)

    async def get_overrides(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return await self._get("get_overrides", timeout, lambda: self.overrides)

    async def get_plant_conf(self, *, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        return await self._get("get_plant_conf", timeout, lambda: self.plant_conf)

    async def get_history(
        self,
        key: str,
        unita: str,
        gte: str,
        lte: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        self.history_calls.append((key, unita, gte, lte, self.clock.monotonic()))
        return await self._get("get_history", timeout, lambda: self.history(key, unita, gte, lte))

    async def close(self) -> None:
        self.closed += 1

    def set_value(self, gruppo: str, unita: str, subuni: str, key: str, value: str) -> None:
        """Change (or add) one interface row of the next snapshot."""
        for row in self.interface:
            if (row["Gruppo"], row["Unita"], row["SubUni"], row["Key"]) == (
                gruppo,
                unita,
                subuni,
                key,
            ):
                row["Valore"] = value
                return
        self.interface.append(rec(gruppo, unita, subuni, key, value))


_END = object()


class FakeWsConnection:
    """A receive-only connection fed by the test.  It has no send method."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.close_calls = 0

    def push(self, *frames: Any) -> None:
        for frame in frames:
            self._queue.put_nowait(frame)

    def end(self) -> None:
        """The server closes the connection."""
        self._queue.put_nowait(_END)

    async def receive(self) -> Any | None:
        if self.closed:
            return None
        item = await self._queue.get()
        if item is _END or self.closed:
            return None
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.close_calls += 1
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(_END)


class FakeWsConnector:
    """``WsConnector``: each call records its virtual time and returns a new connection."""

    def __init__(self, clock: VirtualClock, *, log: list[str] | None = None) -> None:
        self.clock = clock
        self.attempts: list[float] = []
        self.connections: list[FakeWsConnection] = []
        self.failures: list[BaseException] = []
        self.fail_always: BaseException | None = None
        self.latency = 0.0
        self.log = log if log is not None else []

    @property
    def current(self) -> FakeWsConnection:
        return self.connections[-1]

    async def __call__(self) -> FakeWsConnection:
        self.attempts.append(self.clock.monotonic())
        self.log.append("ws_connect")
        if self.latency:
            await self.clock.sleep(self.latency)
        if self.failures:
            raise self.failures.pop(0)
        if self.fail_always is not None:
            raise self.fail_always
        conn = FakeWsConnection()
        self.connections.append(conn)
        return conn


def _identity() -> UnitIdentity:
    return UnitIdentity()


def _schedule() -> ZoneSchedule:
    empty = SeasonSchedule(days=(), crono=None, source=ScheduleSource.NONE)
    return ZoneSchedule(winter=empty, summer=empty)


class FakeBuilder:
    """Small complete :class:`RehomState` from the stores; see the module docstring."""

    def __init__(self) -> None:
        self.builds: list[datetime] = []
        self.next_change: Callable[[datetime], datetime | None] = lambda _now: None
        self.fail_next = 0
        self.live_seen: list[datetime | None] = []

    def build(self, view: StoreView, *, now: datetime, clock: DeviceClock) -> RehomState:
        self.builds.append(now)
        self.live_seen.append(view.live_since)
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("builder failure (test)")

        def r(key: str) -> str | None:
            return view.value("REHOM", "", "", key)

        zones: dict[str, Zone] = {}
        for row in view.rows_with_key("TEMP_AMBIENTE"):
            if row.gruppo != "ZONA":
                continue
            z = row.unita
            zones[z] = Zone(
                id=z,
                name=view.value("ZONA", z, "", "NOME") or f"Zona {z}",
                online=True,
                temperature=parse_float(row.value),
                humidity=None,
                has_humidity_sensor=None,
                calling=parse_bool01(view.value("ZONA", z, "", "ATTIVA")),
                setp=None,
                offset=None,
                forced=None,
                forced_setpoint=None,
                controller_setpoint=parse_float(view.value("ZONA", z, "", "SET_POINT_TEMP")),
                level=None,
                base=None,
                target=None,
                mode=None,
                preset=None,
                control_source=None,
                hvac_action=None,
                override=None,
                next_change_at=None,
                schedule=_schedule(),
                has_vmc=None,
                identity=_identity(),
            )
        forecast_view = view.forecast
        forecast = (
            None
            if forecast_view is None
            else Forecast(
                received_at=forecast_view.received_at,
                entries=tuple(
                    ForecastEntry(
                        time=datetime.fromtimestamp(epoch, UTC),
                        temperature=None,
                        feels_like=None,
                        humidity=None,
                        dew_point=None,
                        pressure=None,
                        wind_speed=None,
                        wind_bearing=None,
                        wind_gust=None,
                        precipitation_probability=None,
                        clouds=None,
                        condition_id=None,
                        icon=raw[:8],
                    )
                    for epoch, raw in sorted(forecast_view.items.items())
                ),
            )
        )
        heartbeat = view.row("PROC", "", "", "WATCHDOG_MASTER")
        version = view.alive.get("version")
        return RehomState(
            hub=Hub(
                mac=None,
                web_version=version if isinstance(version, str) else None,
                controller_version=r("VER_SOFT"),
                board=view.config.get("BOARD"),
                platform=None,
                timezone=view.config.get("TIMEZONE"),
            ),
            plant=Plant(
                season=None,
                season_forced=None,
                conf_season=None,
                mode=None,
                set_point=None,
                preset=None,
                temperature_off=None,
                temperature_economy=None,
                temperature_pre_comfort=None,
                temperature_comfort=parse_float(r("TEMP_COM")),
                temperature_eco_aux=None,
                display_temperature=None,
                controller_setpoint=parse_float(r("SET_POINT_TEMP")),
                effective_setpoint=None,
                setpoint_mismatch=None,
                setpoint_mismatch_since=None,
                predictive=None,
                read_only=None,
                lock=LockState.NORMAL,
                is_crono=False,
                serial_down=False,
                server_on_requested=None,
                demand=None,
                current_temperature=None,
                domotica_enabled=False,
                installer_session=None,
                installer_activity_at=view.last_commissioning_at,
                vmc_type_code=None,
            ),
            zones=MappingProxyType(zones),
            vmcs=MappingProxyType({}),
            actuators=MappingProxyType({}),
            fancoils=MappingProxyType({}),
            alarms=(),
            alarms_debounced=(),
            health=Health(
                bus_ok=None,
                watchdog_ok=None,
                internet_ok=None,
                remote_access_configured=False,
                heartbeat_at=None if heartbeat is None else heartbeat.changed_at,
                heartbeat_ok=None if view.live_since is None else True,
                cpu_temperature=None,
                wifi_available=None,
                weather_service_ok=None,
            ),
            weather=None,
            forecast=forecast,
            device_clock=clock,
            plant_conf=MappingProxyType(dict(view.plant_conf)),
            next_change_at=self.next_change(now),
            synced_at=view.synced_at,
            built_at=now,
        )


class Harness:
    """A client wired to fakes on a :class:`VirtualClock`.

    The fake socket is silent unless a test pushes frames (a real controller
    sends a heartbeat every minute), so the WS idle watchdog is off by default:
    ``ws_idle_timeout`` overrides the one in ``options``.
    """

    def __init__(
        self,
        *,
        options: ClientOptions | None = None,
        username: str | None = None,
        password: str | None = None,
        has_token: bool = False,
        start: datetime = T0,
        ws_idle_timeout: float | None = None,
    ) -> None:
        options = dataclasses.replace(
            options if options is not None else ClientOptions(), ws_idle_timeout=ws_idle_timeout
        )
        self.clock = VirtualClock(start)
        self.log: list[str] = []
        self.transport = FakeTransport(self.clock, has_token=has_token, log=self.log)
        self.connector = FakeWsConnector(self.clock, log=self.log)
        self.builder = FakeBuilder()
        self.client = RehomClient(
            "replay.invalid",
            username=username,
            password=password,
            transport=self.transport,
            ws_connector=self.connector,
            clock=self.clock,
            builder=self.builder,
            options=options,
            rng=lambda: 0.0,
        )
        self.updates: list[StateUpdate] = []
        self.client.subscribe(self.updates.append)
        self.transitions: list[Any] = []
        self.client.on_connection_change(self.transitions.append)

    async def connect(self, advance: float = 5.0) -> None:
        task = asyncio.create_task(self.client.connect())
        await self.clock.settle()
        await self.clock.advance(advance)
        await task

    @property
    def ws(self) -> FakeWsConnection:
        return self.connector.current

    async def push(self, *frames: Any, settle: bool = True) -> None:
        self.ws.push(*frames)
        if settle:
            await self.clock.settle()

    def reasons(self) -> list[str]:
        return [update.reason.value for update in self.updates]

    async def close(self) -> None:
        await self.client.close()
