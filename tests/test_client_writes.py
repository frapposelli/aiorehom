"""Client write execution: opt-in, FIFO order, no-op skip, echo confirmation, fallback."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from aiorehom.client import ClientOptions, RehomClient
from aiorehom.clock import VirtualClock
from aiorehom.enums import ConnectionState, MasterPreset, VmcMode, ZoneSetp
from aiorehom.exceptions import (
    RehomConnectionError,
    RehomNotReadyError,
    RehomTimeoutError,
    RehomWriteNotConfirmedError,
    RehomWriteRefusedError,
)
from aiorehom.transport import INTERFACE_WRITE_PATH, OVERRIDES_WRITE_PATH, check_write_body
from aiorehom.values import values_equal

from .sync_fakes import AUTO_LOCAL_TIME, T0, FakeTransport, FakeWsConnector, termo

FIXTURE = Path(__file__).parent / "fixtures" / "20260925T102117Z"


def controller_spelling(value: str) -> str:
    """How the controller re-emits a number some seconds later (``"1.0"`` -> ``"1"``)."""
    try:
        number = Decimal(value)
    except InvalidOperation:
        return value
    return format(number.normalize(), "f") if number.is_finite() else value


class WritingController(FakeTransport):
    """FakeTransport that accepts bulk writes and echoes them in the live order.

    On a live controller the WS echo of every write arrives before the
    POST's 204: a write waits ``post_latency``, applies its records to the next
    REST snapshot (``apply``) and pushes their echo (``echo``), then answers
    ``response_delay`` later.  Override records go to the overrides snapshot and
    their echo carries ``issuedAt``/``expiresAt``.  With ``normalise_after`` set,
    a number sent in another spelling (``"1.0"``) is re-emitted in the
    controller's spelling (``"1"``) that many seconds later, in REST and on the WS.
    Every body first goes through the real write gate (:func:`check_write_body`),
    as in ``ReadOnlyTransport``: a body the gate refuses is never recorded.
    """

    def __init__(self, clock: VirtualClock, connector: FakeWsConnector) -> None:
        super().__init__(clock)
        self.connector = connector
        self.interface = json.loads((FIXTURE / "interface.json").read_text())
        self.plant_conf = json.loads((FIXTURE / "plant_conf.json").read_text())
        config = json.loads((FIXTURE / "config.json").read_text())
        config["LOCAL_TIME"] = AUTO_LOCAL_TIME
        self.config = config
        self.writes: list[tuple[str, list[dict[str, Any]], float]] = []
        self.echo = True
        self.apply = True
        self.post_latency = 0.1
        self.response_delay = 0.01
        self.normalise_after: float | None = None
        self.renormalising: list[asyncio.Task[None]] = []

    async def post_bulk_update(
        self,
        path: str,
        records: Any,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> int:
        records = check_write_body(path, records)  # refused before any "I/O", as live
        self.writes.append((path, list(records), self.clock.monotonic()))
        await self.clock.sleep(self.post_latency)
        for record in records:
            sent = str(record["Valore"])
            self._report(record, sent)
            normalised = controller_spelling(sent)
            if self.normalise_after is not None and normalised != sent:
                self.renormalising.append(
                    asyncio.ensure_future(self._renormalise(record, normalised))
                )
        await self.clock.sleep(self.response_delay)
        return 204

    async def _renormalise(self, record: dict[str, Any], value: str) -> None:
        assert self.normalise_after is not None
        await self.clock.sleep(self.normalise_after)
        self._report(record, value)

    def _report(self, record: dict[str, Any], value: str) -> None:
        if self.apply:
            if record["Gruppo"] == "PROG_OVERRIDE":
                self.set_override(record, value)
            else:
                self.set_value(
                    record["Gruppo"], record["Unita"], record["SubUni"], record["Key"], value
                )
        if self.echo:
            extra: dict[str, Any] = {}
            if "Impostazione" in record:
                extra = {"issuedAt": record["Impostazione"], "expiresAt": record["Scadenza"]}
            path = f"{record['Gruppo']}.{record['Unita']}.{record['SubUni']}.{record['Key']}"
            self.connector.current.push(termo(path, value, **extra))

    def set_override(self, record: dict[str, Any], value: str) -> None:
        """Store an override row in the next ``/overrides/`` snapshot (one per path)."""
        row = {
            "Gruppo": "PROG_OVERRIDE",
            "Unita": record["Unita"],
            "SubUni": record["SubUni"],
            "Key": record["Key"],
            "Valore": value,
            "Impostazione": record["Impostazione"],
            "Scadenza": record["Scadenza"],
        }
        key = (row["Unita"], row["SubUni"], row["Key"])
        for index, old in enumerate(self.overrides):
            if (old["Unita"], old["SubUni"], old["Key"]) == key:
                self.overrides[index] = row
                return
        self.overrides.append(row)

    def program(self, zone: str, preset: str) -> list[str]:
        """The summer day program ``preset`` of ``zone``, as 48 level strings."""
        return next(
            str(r["Valore"])
            for r in self.interface
            if (r["Gruppo"], r["Unita"], r["SubUni"], r["Key"])
            == ("PROG", zone, preset, "PROG_GIORNO_ESTATE")
        ).split(",")


class Rig:
    def __init__(self, *, allow_writes: bool = True, start: datetime = T0) -> None:
        self.clock = VirtualClock(start)
        self.connector = FakeWsConnector(self.clock)
        self.ctrl = WritingController(self.clock, self.connector)
        self.client = RehomClient(
            "replay.invalid",
            transport=self.ctrl,
            ws_connector=self.connector,
            clock=self.clock,
            options=ClientOptions(ws_idle_timeout=None),
            rng=lambda: 0.0,
            allow_writes=allow_writes,
        )

    async def connect(self) -> None:
        task = asyncio.create_task(self.client.connect())
        await self.clock.settle()
        await self.clock.advance(5)
        await task

    async def run(self, coro: Any, advance: float = 30.0) -> Any:
        task = asyncio.ensure_future(coro)
        await self.clock.settle()
        step = 0.5
        waited = 0.0
        while not task.done() and waited < advance:
            await self.clock.advance(step)
            waited += step
        await self.clock.settle()
        if not task.done():  # nothing else advances the virtual clock: fail, never hang
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            pytest.fail(f"the call needed more than {advance:g} virtual seconds")
        return await task


def _keys(rig: Rig) -> list[tuple[str, str]]:
    return [(str(r["path"]), str(r["Valore"])) for w in rig.ctrl.writes for r in w[1]]


# -- opt-in ----------------------------------------------------------------------


async def test_writes_disabled_by_default() -> None:
    rig = Rig(allow_writes=False)
    await rig.connect()
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 2))  # a regression fails, never hangs
    assert info.value.reason == "writes_disabled"
    assert rig.ctrl.writes == []
    await rig.client.close()


@pytest.mark.parametrize("flag", ["false", "no", "true", 1, 0, None])
def test_allow_writes_must_be_a_real_bool(flag: Any) -> None:
    clock = VirtualClock(T0)
    connector = FakeWsConnector(clock)
    with pytest.raises(TypeError, match="allow_writes"):
        RehomClient(
            "replay.invalid",
            transport=WritingController(clock, connector),
            ws_connector=connector,
            clock=clock,
            allow_writes=flag,
        )
    with pytest.raises(TypeError, match="allow_writes"):  # before any transport is built
        RehomClient("replay.invalid", username="u", password="p", allow_writes=flag)


async def test_injected_transport_must_be_able_to_write() -> None:
    clock = VirtualClock(T0)
    connector = FakeWsConnector(clock)
    with pytest.raises(ValueError, match="post_bulk_update"):
        RehomClient(
            "replay.invalid",
            transport=FakeTransport(clock),
            ws_connector=connector,
            clock=clock,
            allow_writes=True,
        )


@pytest.mark.parametrize("mode", [VmcMode.RAPID_RENEWAL, VmcMode.RAPID_HEAT])
async def test_rapid_vmc_modes_are_refused_before_the_gate(mode: VmcMode) -> None:
    """Selectable in the fixture, yet refused with a reason: nothing reaches the gate."""
    rig = Rig()
    await rig.connect()
    assert VmcMode.RAPID_RENEWAL in rig.client.state.vmcs["001"].selectable_modes
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_vmc_mode("001", mode))
    assert info.value.reason == "unsupported_vmc_mode"
    assert rig.ctrl.writes == []
    await rig.client.close()


async def test_not_before_connect() -> None:
    rig = Rig()
    with pytest.raises(RehomNotReadyError):
        await rig.client.set_vmc_fan("001", 2)


async def test_refused_while_unavailable() -> None:
    rig = Rig()
    await rig.connect()
    rig.ctrl.fail("get_alive", *(RehomConnectionError("down") for _ in range(3)))
    await rig.clock.advance(3 * rig.client.options.alive_interval + 1)
    assert rig.client.connection_state is ConnectionState.UNAVAILABLE
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 2))
    assert info.value.reason == "unavailable"
    assert rig.ctrl.writes == []
    await rig.client.close()


# -- send and confirm ------------------------------------------------------------


async def test_write_confirmed_by_echo() -> None:
    rig = Rig()
    await rig.connect()
    assert await rig.run(rig.client.set_vmc_fan("001", 2)) is True
    ((path, records, _t),) = rig.ctrl.writes
    assert path == INTERFACE_WRITE_PATH
    assert records[0]["path"] == "DEUM.001..COM_VENTILA" and records[0]["Valore"] == "2"
    # Published by the time the call returns (no wait for the notify batch window).
    assert rig.client.state.vmcs["001"].fan.value == 2
    await rig.client.close()


async def test_noop_write_is_skipped() -> None:
    rig = Rig()
    await rig.connect()
    assert await rig.run(rig.client.set_vmc_fan("001", 1)) is False  # already MIN
    assert rig.ctrl.writes == []
    await rig.client.close()


async def test_confirmed_by_resync_when_no_echo() -> None:
    rig = Rig()
    rig.ctrl.echo = False
    await rig.connect()
    assert await rig.run(rig.client.set_vmc_fan("001", 3), advance=200) is True
    assert rig.ctrl.count("get_interface") >= 2  # initial sync + the confirming resync
    assert rig.client.state.vmcs["001"].fan.value == 3
    await rig.client.close()


async def test_not_confirmed_when_the_controller_ignores_it() -> None:
    rig = Rig()
    rig.ctrl.echo = False
    rig.ctrl.apply = False
    await rig.connect()
    with pytest.raises(RehomWriteNotConfirmedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 3), advance=200)
    assert info.value.__cause__ is None
    assert len(rig.ctrl.writes) == 1  # never retried
    await rig.client.close()


async def test_zone_offset_confirmed_in_either_spelling() -> None:
    """Sent as "1.0", re-emitted as "1" seconds later: one write, then a no-op."""
    rig = Rig()
    rig.ctrl.normalise_after = 6.0
    await rig.connect()
    assert await rig.run(rig.client.set_zone_offset("001", 1)) is True
    assert _keys(rig) == [("ZONA.001..DELTA_SETP_CORRENTE", "1.0")]
    assert rig.client.state.zones["001"].offset == 1.0
    await rig.clock.advance(10)
    assert rig.client.state.zones["001"].offset == 1.0
    assert await rig.run(rig.client.set_zone_offset("001", 1)) is False
    assert await rig.run(rig.client.set_zone_offset("001", 0)) is True
    assert _keys(rig)[-1] == ("ZONA.001..DELTA_SETP_CORRENTE", "0.0")
    await rig.clock.advance(10)
    assert rig.client.state.zones["001"].offset == 0.0
    await rig.client.close()


async def test_predictive_switch() -> None:
    rig = Rig()
    rig.ctrl.set_value("REHOM", "", "", "MODO", "2")
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT", "0")
    await rig.connect()
    assert rig.client.state.plant.predictive is True
    assert await rig.run(rig.client.set_predictive(False)) is True
    assert _keys(rig) == [("REHOM...ALG_ATTIVO", "0")]
    assert rig.client.state.plant.predictive is False
    await rig.client.close()


# -- ordering and fresh planning ---------------------------------------------------


async def test_writes_run_one_at_a_time_in_call_order() -> None:
    """Each POST starts only after the previous write is confirmed in the stores."""
    rig = Rig()
    await rig.connect()
    seen: list[dict[str, str]] = []
    post = rig.ctrl.post_bulk_update

    async def spy(path: str, records: Any, *, timeout: float | None = None) -> int:  # noqa: ASYNC109
        seen.append(rig.client.record_values())
        return await post(path, records, timeout=timeout)

    rig.ctrl.post_bulk_update = spy  # type: ignore[method-assign]
    first = asyncio.ensure_future(rig.client.set_vmc_fan("001", 2))
    second = asyncio.ensure_future(rig.client.set_vmc_mode("001", VmcMode.VENTILATE))
    third = asyncio.ensure_future(rig.client.set_vmc_fan("002", 3))
    assert await rig.run(asyncio.gather(first, second, third)) == [True, True, True]
    keys = _keys(rig)
    assert keys == [
        ("DEUM.001..COM_VENTILA", "2"),
        ("DEUM.001..ST_MODE", "8"),
        ("DEUM.002..COM_VENTILA", "3"),
    ]
    for index in range(1, len(keys)):
        previous_path, previous_value = keys[index - 1]
        assert values_equal(seen[index][previous_path], previous_value)
    times = [w[2] for w in rig.ctrl.writes]
    assert all(b - a >= rig.ctrl.post_latency for a, b in itertools.pairwise(times))
    await rig.client.close()


async def test_plan_sees_the_previous_write() -> None:
    """The second call is planned after the first is confirmed: STOP locks the fan."""
    rig = Rig()
    await rig.connect()
    await rig.run(rig.client.set_vmc_mode("001", VmcMode.STOP))
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 2))
    assert info.value.reason == "fan_locked_by_mode"
    await rig.client.close()


async def test_back_to_back_awaits_plan_on_the_confirmed_state() -> None:
    """No clock advance between the calls: the echo is still in the notify batch window."""
    rig = Rig()
    await rig.connect()

    async def scenario() -> None:
        assert await rig.client.set_vmc_mode("001", VmcMode.STOP) is True
        assert rig.client.state.vmcs["001"].mode is VmcMode.STOP
        await rig.client.set_vmc_fan("001", 2)

    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(scenario())
    assert info.value.reason == "fan_locked_by_mode"
    assert _keys(rig) == [("DEUM.001..ST_MODE", "0")]
    await rig.client.close()


async def test_comfort_temperature_then_comfort_preset_is_a_noop() -> None:
    """The preset must carry the new TEMP_COM, not the one from before the first write."""
    rig = Rig()
    await rig.connect()

    async def scenario() -> tuple[bool, bool]:
        first = await rig.client.set_comfort_temperature(25)
        return first, await rig.client.set_house_preset(MasterPreset.COMFORT)

    assert await rig.run(scenario()) == (True, False)
    assert _keys(rig) == [("REHOM...TEMP_COM", "25"), ("REHOM...SET_POINT_TEMP", "25")]
    plant = rig.client.state.plant
    assert plant.temperature_comfort == 25.0
    assert plant.setpoint_mismatch is False
    await rig.client.close()


async def test_comfort_preset_then_comfort_temperature() -> None:
    """A valid follow-up is not refused on the state from before the first write."""
    rig = Rig()
    rig.ctrl.set_value("REHOM", "", "", "MODO", "2")
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT", "0")
    await rig.connect()

    async def scenario() -> tuple[bool, bool]:
        first = await rig.client.set_house_preset(MasterPreset.COMFORT)
        return first, await rig.client.set_comfort_temperature(25)

    assert await rig.run(scenario()) == (True, True)
    assert len(rig.ctrl.writes) == 2
    await rig.client.close()


async def test_house_comfort_always_carries_the_setpoint() -> None:
    rig = Rig()
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT_TEMP", "26")  # stale, as seen live
    await rig.connect()
    assert rig.client.state.plant.setpoint_mismatch is True
    assert await rig.run(rig.client.set_house_preset(MasterPreset.COMFORT)) is True
    (write,) = rig.ctrl.writes
    assert [(r["Key"], r["Valore"]) for r in write[1]] == [
        ("MODO", "1"),
        ("SET_POINT", "3"),
        ("SET_POINT_TEMP", "24"),
    ]
    assert rig.client.state.plant.setpoint_mismatch is False
    await rig.client.close()


async def test_zone_mode_refused_in_manual_house() -> None:
    rig = Rig()
    await rig.connect()
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_zone_mode("001", ZoneSetp.ECONOMY))
    assert info.value.reason == "house_not_auto"
    assert rig.ctrl.writes == []
    await rig.client.close()


# -- after the POST: every failure is "not confirmed" ------------------------------


async def test_failed_confirming_resync_is_not_confirmed() -> None:
    rig = Rig()
    rig.ctrl.echo = False
    await rig.connect()
    rig.ctrl.fail("get_interface", RehomConnectionError("down"))
    with pytest.raises(RehomWriteNotConfirmedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 3), advance=200)
    assert isinstance(info.value.__cause__, RehomConnectionError)
    assert len(rig.ctrl.writes) == 1
    await rig.client.close()


async def test_confirming_resync_is_bounded() -> None:
    """A resync that never completes does not hold the write lock until close()."""
    rig = Rig()
    rig.ctrl.echo = False
    await rig.connect()
    rig.ctrl.latency["get_interface"] = 1e6  # the sync hangs
    task = asyncio.ensure_future(rig.client.set_vmc_fan("001", 3))
    await rig.clock.advance(400)
    assert task.done()
    with pytest.raises(RehomWriteNotConfirmedError) as info:
        await task
    assert isinstance(info.value.__cause__, RehomTimeoutError)
    assert len(rig.ctrl.writes) == 1
    await rig.client.close()


async def test_close_while_confirming() -> None:
    """close() ends the wait at once; a queued write is not sent at all."""
    rig = Rig()
    rig.ctrl.echo = False
    rig.ctrl.apply = False
    await rig.connect()
    sent = asyncio.ensure_future(rig.client.set_vmc_fan("001", 3))
    queued = asyncio.ensure_future(rig.client.set_vmc_fan("002", 3))
    await rig.clock.settle()
    await rig.clock.advance(1)
    assert len(rig.ctrl.writes) == 1 and not sent.done()
    await rig.client.close()
    await rig.clock.advance(rig.client.options.write_poll_interval)
    assert sent.done() and queued.done()
    with pytest.raises(RehomWriteNotConfirmedError) as info:
        await sent
    assert isinstance(info.value.__cause__, RehomNotReadyError)
    with pytest.raises(RehomNotReadyError):
        await queued
    assert len(rig.ctrl.writes) == 1


# -- temporary comfort -------------------------------------------------------------

AT_2205 = datetime(2026, 9, 25, 20, 5, tzinfo=UTC)  # 22:05 local: schedules at OFF
AT_2220 = AT_2205 + timedelta(minutes=15)
OVERRIDE_PATH = "PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"


def evening_rig(at: datetime) -> Rig:
    """House AUTO; connect() ends exactly at ``at``."""
    rig = Rig(start=at - timedelta(seconds=5))
    rig.ctrl.set_value("REHOM", "", "", "MODO", "2")
    rig.ctrl.set_value("REHOM", "", "", "SET_POINT", "0")
    return rig


def _window(rig: Rig) -> tuple[str | None, str | None]:
    override = rig.client.state.zones["001"].override
    assert override is not None
    clock = rig.client.state.device_clock

    def local(value: datetime | None) -> str | None:
        return None if value is None else clock.local_now(value).strftime("%Y-%m-%d %H:%M:%S")

    return local(override.set_at), local(override.expires_at)


async def test_temporary_comfort_created_then_extended_with_the_same_slots() -> None:
    rig = evening_rig(AT_2205)
    await rig.connect()
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is True
    (path, (created,), _t) = rig.ctrl.writes[0]
    assert path == OVERRIDES_WRITE_PATH
    assert (created["Impostazione"], created["Scadenza"]) == (
        "2026-09-25 22:05:00",
        "2026-09-25 22:34:59",
    )
    assert rig.client.state.zones["001"].override is not None
    assert rig.client.state.zones["001"].override.applies
    await rig.clock.advance_to(AT_2220)
    # 22:20 + 30 min: same slots (44-45), so the same Valore; only the window moves.
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is True
    assert len(rig.ctrl.writes) == 2
    (extended,) = rig.ctrl.writes[1][1]
    assert extended["Valore"] == created["Valore"]
    assert (extended["Impostazione"], extended["Scadenza"]) == (
        "2026-09-25 22:05:00",
        "2026-09-25 22:49:59",
    )
    assert _window(rig) == ("2026-09-25 22:05:00", "2026-09-25 22:49:59")
    # The same request again: value and window already reported, nothing sent.
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is False
    assert len(rig.ctrl.writes) == 2
    await rig.client.close()


async def test_temporary_comfort_echo_with_the_old_window_does_not_confirm() -> None:
    rig = evening_rig(AT_2205)
    await rig.connect()
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is True
    await rig.clock.advance_to(AT_2220)
    rig.ctrl.echo = False
    rig.ctrl.apply = False
    post = rig.ctrl.post_bulk_update

    async def stale_echo(path: str, records: Any, *, timeout: float | None = None) -> int:  # noqa: ASYNC109
        (record,) = records
        rig.connector.current.push(
            termo(
                OVERRIDE_PATH,
                record["Valore"],
                issuedAt="2026-09-25 22:05:00",
                expiresAt="2026-09-25 22:34:59",
            )
        )
        return await post(path, records, timeout=timeout)

    rig.ctrl.post_bulk_update = stale_echo  # type: ignore[method-assign]
    with pytest.raises(RehomWriteNotConfirmedError) as info:
        await rig.run(rig.client.set_temporary_comfort("001", 30), advance=300)
    assert info.value.__cause__ is None
    assert len(rig.ctrl.writes) == 2  # the extension was sent once, never retried
    assert _window(rig) == ("2026-09-25 22:05:00", "2026-09-25 22:34:59")
    await rig.client.close()


async def test_temporary_comfort_over_an_expired_row_with_the_same_slots() -> None:
    """A week-old row on the same preset has the same Valore: the new window is still sent."""
    rig = evening_rig(AT_2205)
    levels = rig.ctrl.program("001", "1")
    levels[44:46] = ["3", "3"]
    rig.ctrl.set_override(
        {
            "Gruppo": "PROG_OVERRIDE",
            "Unita": "001",
            "SubUni": "1",
            "Key": "PROG_GIORNO_ESTATE",
            "Impostazione": "2026-09-18 22:05:00",
            "Scadenza": "2026-09-18 22:34:59",
        },
        ",".join(levels),
    )
    await rig.connect()
    override = rig.client.state.zones["001"].override
    assert override is not None and not override.applies
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is True
    ((_path, (record,), _t),) = rig.ctrl.writes
    assert record["Valore"] == ",".join(levels)
    assert (record["Impostazione"], record["Scadenza"]) == (
        "2026-09-25 22:05:00",
        "2026-09-25 22:34:59",
    )
    override = rig.client.state.zones["001"].override
    assert override is not None and override.applies
    await rig.client.close()


def _seed_override(rig: Rig, start: str, end: str) -> str:
    """Zone 001 override for slots 44-45 (22:00-23:00) with the given window."""
    levels = rig.ctrl.program("001", "1")
    levels[44:46] = ["3", "3"]
    value = ",".join(levels)
    rig.ctrl.set_override(
        {
            "Unita": "001",
            "SubUni": "1",
            "Key": "PROG_GIORNO_ESTATE",
            "Impostazione": start,
            "Scadenza": end,
        },
        value,
    )
    return value


async def test_temporary_comfort_window_compared_as_device_times() -> None:
    """A REST row spelled with "T" names the same window: nothing to send."""
    rig = evening_rig(AT_2205)
    _seed_override(rig, "2026-09-25T22:05:00", "2026-09-25T22:34:59")
    await rig.connect()
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is False
    assert rig.ctrl.writes == []
    await rig.client.close()


async def test_temporary_comfort_with_only_a_later_start_is_sent() -> None:
    """Same Valore and Scadenza, but the stored row starts later: the window differs."""
    rig = evening_rig(AT_2205)
    value = _seed_override(rig, "2026-09-25 22:10:00", "2026-09-25 22:34:59")
    await rig.connect()
    assert await rig.run(rig.client.set_temporary_comfort("001", 30)) is True
    ((_path, (record,), _t),) = rig.ctrl.writes
    assert record["Valore"] == value
    assert (record["Impostazione"], record["Scadenza"]) == (
        "2026-09-25 22:05:00",
        "2026-09-25 22:34:59",
    )
    await rig.client.close()


async def test_plan_flushes_frames_still_in_the_notify_window() -> None:
    """The panel stops VMC 001; the frame is applied but not yet published."""
    rig = Rig()
    await rig.connect()
    rig.connector.current.push(termo("DEUM.001..ST_MODE", "0"))
    await rig.clock.settle()  # applied to the store, the 0.1 s batch window still open
    assert rig.client.record_values()["DEUM.001..ST_MODE"] == "0"
    assert rig.client.state.vmcs["001"].mode is not VmcMode.STOP
    with pytest.raises(RehomWriteRefusedError) as info:
        await rig.run(rig.client.set_vmc_fan("001", 2))
    assert info.value.reason == "fan_locked_by_mode"
    assert rig.ctrl.writes == []
    await rig.client.close()
