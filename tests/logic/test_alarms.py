"""``aiorehom.logic.alarms``: conditions, token presence and the debouncing tracker."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from aiorehom.enums import AlarmSource, DeviceKind, LockState
from aiorehom.logic import (
    GENERIC_ALARM_KINDS,
    MIN_ALARM_DEBOUNCE,
    VMC_ALARM_KEYS,
    AlarmCondition,
    AlarmInputs,
    AlarmTracker,
    GenericAlarmRow,
    TrackedAlarm,
    UnitPresence,
    compute_alarms,
    token_configured,
)

K = DeviceKind


def _presence(*units: str, online: bool | None = True) -> dict[str, UnitPresence]:
    return {u: UnitPresence(unit=u, index=int(u) - 1, online=online) for u in units}


def _inputs(**changes: object) -> AlarmInputs:
    base = AlarmInputs(
        presence={
            K.ZONE: _presence("001", "002", "003", "009", "010", "011"),
            K.VMC: _presence("001", "002"),
            K.ACTUATOR: _presence("001", "002"),
            K.FANCOIL: {},
        },
        vmc_flags={
            "001": dict.fromkeys(VMC_ALARM_KEYS, "0"),
            "002": dict.fromkeys(VMC_ALARM_KEYS, "0"),
        },
        vmc_free_cooling={"001": ("0", "0"), "002": ("0", "0")},
        generic=(),
        process=(
            ("1", "Start main daemon"),
            ("14", "Temperatura cpu web server 64.5 'C"),
            ("101", ""),
            ("114", ""),
        ),
        bus_raw="1",
        watchdog_raw="0",
        internet_raw="1",
        remote_token_raw="<redacted len=36>",
        weather_key_raw="0",
        lock=LockState.NORMAL,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def _ids(inputs: AlarmInputs) -> list[str]:
    return [condition.id for condition in compute_alarms(inputs)]


def test_nominal_fixture_has_no_alarms() -> None:
    assert compute_alarms(_inputs()) == ()


def test_all_rows_missing_is_not_an_alarm() -> None:
    """The web UI raises missing ALLARME_BUS / WATCHDOG_ALARM / STATO; the library does not."""
    inputs = AlarmInputs(
        presence={K.ZONE: _presence("001", online=None)},
        vmc_flags={},
        vmc_free_cooling={},
        generic=(),
        process=(),
        bus_raw=None,
        watchdog_raw=None,
        internet_raw=None,
        remote_token_raw=None,
        weather_key_raw=None,
        lock=LockState.NORMAL,
    )
    assert compute_alarms(inputs) == ()


def test_unit_not_responding() -> None:
    presence = {
        K.ZONE: {**_presence("001"), **_presence("009", online=False)},
        K.VMC: _presence("002", online=False),
        K.ACTUATOR: _presence("003", online=False),
        K.FANCOIL: _presence("010", online=False),
    }
    conditions = compute_alarms(_inputs(presence=presence, vmc_flags={}, vmc_free_cooling={}))
    assert [c.id for c in conditions] == [
        "actuator:003:not_responding",
        "fancoil:010:not_responding",
        "vmc:002:not_responding",
        "zone:009:not_responding",
    ]
    zone = conditions[-1]
    assert zone == AlarmCondition(
        id="zone:009:not_responding",
        source=AlarmSource.UNIT_NOT_RESPONDING,
        device=K.ZONE,
        unit="009",
        code=None,
        text=None,
    )


def test_vmc_flag() -> None:
    flags = {"001": {**dict.fromkeys(VMC_ALARM_KEYS, "0"), "ALLARM_SONDA_RICIRCOLO": "1"}}
    (condition,) = compute_alarms(_inputs(vmc_flags=flags))
    assert condition.id == "vmc:001:ALLARM_SONDA_RICIRCOLO"
    assert condition.source is AlarmSource.VMC_FLAG
    assert condition.text == "recirculation air probe"
    assert condition.unit == "001"


def test_vmc_flags_need_a_present_vmc_and_a_known_key() -> None:
    flags = {
        "003": {"ALLARM_ESPANSIONE": "1"},  # not present
        "001": {"COM_SBRINA": "1", "ALLARME": "250", "ALLARM_ESPANSIONE": None},  # defrost
        "002": {"ALLARM_ESPANSIONE": "1.0"},
    }
    assert _ids(_inputs(vmc_flags=flags)) == ["vmc:002:ALLARM_ESPANSIONE"]


def test_vmc_alarm_key_order() -> None:
    """The documented alarm order."""
    assert list(VMC_ALARM_KEYS) == [
        "ALLARM_ESPANSIONE",
        "ALLARM_PRESS_ALTA_PRESS",
        "ALLARM_PRESSOSTATO_FILTRO",
        "ALLARM_SBRINAMENTO",
        "ALLARM_SONDA_ANTIGELO",
        "ALLARM_SONDA_LIVELLO",
        "ALLARM_SONDA_RICIRCOLO",
        "ALLARM_SONDA_RINNOVO",
    ]


def test_free_cooling_error() -> None:
    both = {"001": ("1", "1"), "002": ("1", "0")}
    (condition,) = compute_alarms(_inputs(vmc_free_cooling=both))
    assert condition.id == "vmc:001:free_cooling_error"
    assert condition.source is AlarmSource.FREE_COOLING
    assert condition.text == "free-cooling error"
    off = {"001": ("0", "1"), "002": (None, "1")}
    assert compute_alarms(_inputs(vmc_free_cooling=off)) == ()


def test_generic_alarm() -> None:
    generic = (
        GenericAlarmRow("ZONA", "002", "114", "zone fault"),
        GenericAlarmRow("DEUM", "", "150", "vmc bus fault"),
        GenericAlarmRow("ATT", "001", "101", "actuator fault"),
    )
    conditions = compute_alarms(_inputs(generic=generic))
    assert [c.id for c in conditions] == [
        "generic:ATT:001:101",
        "generic:DEUM::150",
        "generic:ZONA:002:114",
    ]
    by_id = {c.id: c for c in conditions}
    assert by_id["generic:ZONA:002:114"].device is K.ZONE
    assert by_id["generic:ZONA:002:114"].code == 114
    assert by_id["generic:ZONA:002:114"].text == "zone fault"
    assert by_id["generic:DEUM::150"].unit is None
    assert by_id["generic:DEUM::150"].device is K.VMC
    assert by_id["generic:ATT:001:101"].source is AlarmSource.GENERIC


@pytest.mark.parametrize(
    "row",
    [
        GenericAlarmRow("ZONA", "004", "114", "absent zone"),
        GenericAlarmRow("ZONA", "4", "114", "stray id"),
        GenericAlarmRow("AT9091", "001", "114", "no fancoils"),
        GenericAlarmRow("ZONA", "001", "100", "code <= 100"),
        GenericAlarmRow("ZONA", "001", "14", "code <= 100"),
        GenericAlarmRow("ZONA", "001", "114", ""),
        GenericAlarmRow("ZONA", "001", "x", "bad code"),
        GenericAlarmRow("REHOM", "", "114", "unknown group"),
    ],
)
def test_generic_alarm_ignored(row: GenericAlarmRow) -> None:
    assert compute_alarms(_inputs(generic=(row,))) == ()


def test_generic_kinds() -> None:
    assert dict(GENERIC_ALARM_KINDS) == {
        "ZONA": K.ZONE,
        "DEUM": K.VMC,
        "AT9091": K.FANCOIL,
        "ATT": K.ACTUATOR,
    }


def test_process_state() -> None:
    process = (("114", "plant fault"), ("14", "cpu text"), ("101", ""), ("x", "garbage"))
    (condition,) = compute_alarms(_inputs(process=process))
    assert condition == AlarmCondition(
        id="plant:process:114",
        source=AlarmSource.PROCESS,
        device=K.PLANT,
        unit=None,
        code=114,
        text="plant fault",
    )
    assert compute_alarms(_inputs(process=(("114", ""),))) == ()
    assert compute_alarms(_inputs(process=(("14", "Temperatura cpu 70 'C"),))) == ()


@pytest.mark.parametrize(
    ("changes", "alarm_id", "source"),
    [
        ({"bus_raw": "0"}, "hub:bus", AlarmSource.BUS),
        ({"watchdog_raw": "1"}, "hub:watchdog", AlarmSource.WATCHDOG),
        ({"internet_raw": "0"}, "hub:internet", AlarmSource.INTERNET),
        ({"lock": LockState.SERIAL_DOWN}, "hub:serial_line", AlarmSource.SERIAL_LINE),
        ({"weather_key_raw": "1"}, "hub:weather_service", AlarmSource.WEATHER_SERVICE),
        ({"weather_key_raw": "abc"}, "hub:weather_service", AlarmSource.WEATHER_SERVICE),
    ],
)
def test_hub_alarms(changes: dict[str, object], alarm_id: str, source: AlarmSource) -> None:
    (condition,) = compute_alarms(_inputs(**changes))
    assert condition.id == alarm_id
    assert condition.source is source
    assert condition.device is K.HUB


@pytest.mark.parametrize(
    "changes",
    [
        {"bus_raw": "1"},
        {"bus_raw": None},
        {"bus_raw": "x"},
        {"watchdog_raw": None},
        {"internet_raw": None},
        {"internet_raw": "0", "remote_token_raw": None},
        {"internet_raw": "0", "remote_token_raw": ""},
        {"internet_raw": "0", "remote_token_raw": "<redacted len=0>"},
        {"lock": LockState.CRONO},  # crono is not an alarm
        {"weather_key_raw": None},
        {"weather_key_raw": "0.0"},
    ],
)
def test_hub_non_alarms(changes: dict[str, object]) -> None:
    assert compute_alarms(_inputs(**changes)) == ()


@pytest.mark.parametrize(
    ("raw", "configured"),
    [
        (None, False),
        ("", False),
        ("<redacted len=0>", False),
        ("<redacted null>", False),
        ("<redacted len=36>", True),
        ("<redacted object>", True),
        ("plain-token", True),
    ],
)
def test_token_configured(raw: str | None, configured: bool) -> None:
    assert token_configured(raw) is configured


def test_sorted_and_deduplicated() -> None:
    generic = (
        GenericAlarmRow("ZONA", "001", "114", "first"),
        GenericAlarmRow("ZONA", "001", "114.0", "second"),
    )
    conditions = compute_alarms(_inputs(generic=generic, bus_raw="0", watchdog_raw="1"))
    assert [c.id for c in conditions] == ["generic:ZONA:001:114", "hub:bus", "hub:watchdog"]
    assert conditions[0].text == "second"


# ---------------------------------------------------------------------------
# AlarmTracker
# ---------------------------------------------------------------------------

T0 = datetime(2026, 9, 25, 10, 37, 10, tzinfo=UTC)
RICIRCOLO = AlarmCondition(
    id="vmc:001:ALLARM_SONDA_RICIRCOLO",
    source=AlarmSource.VMC_FLAG,
    device=K.VMC,
    unit="001",
    code=None,
    text="recirculation air probe",
)
BUS = AlarmCondition("hub:bus", AlarmSource.BUS, K.HUB, None, None, None)


def test_tracker_rejects_short_debounce() -> None:
    assert timedelta(seconds=30) == MIN_ALARM_DEBOUNCE
    with pytest.raises(ValueError, match="30"):
        AlarmTracker(timedelta(seconds=29))
    assert AlarmTracker(timedelta(seconds=30)).debounce == timedelta(seconds=30)


def test_flap_shorter_than_window_is_never_debounced() -> None:
    """The fixture's 14 s VMC flag (10:37:10-10:37:24) with a 30 s window."""
    tracker = AlarmTracker(timedelta(seconds=30))
    for offset in (0, 5, 13.9):
        view = tracker.update([RICIRCOLO], T0 + timedelta(seconds=offset))
        assert [t.condition.id for t in view.active] == [RICIRCOLO.id]
        assert view.debounced == ()
        assert view.next_maturity == T0 + timedelta(seconds=30)
    view = tracker.update([], T0 + timedelta(seconds=14))
    assert view == tracker.update([], T0 + timedelta(seconds=60))
    assert view.active == ()
    assert view.next_maturity is None


def test_debounced_exactly_at_window() -> None:
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    almost = tracker.update([RICIRCOLO], T0 + timedelta(seconds=29, microseconds=999_999))
    assert almost.debounced == ()
    view = tracker.update([RICIRCOLO], T0 + timedelta(seconds=30))
    assert [t.first_seen for t in view.debounced] == [T0]
    assert view.next_maturity is None


def test_next_maturity_is_the_earliest_pending() -> None:
    tracker = AlarmTracker(timedelta(seconds=60))
    tracker.update([BUS], T0)
    view = tracker.update([BUS, RICIRCOLO], T0 + timedelta(seconds=20))
    assert view.next_maturity == T0 + timedelta(seconds=60)
    view = tracker.update([BUS, RICIRCOLO], T0 + timedelta(seconds=61))
    assert [t.condition.id for t in view.debounced] == ["hub:bus"]
    assert view.next_maturity == T0 + timedelta(seconds=80)
    assert [t.condition.id for t in view.active] == ["hub:bus", RICIRCOLO.id]


def test_first_seen_survives_text_change_and_resets_after_gap() -> None:
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    changed = replace(RICIRCOLO, text="new text", code=7)
    view = tracker.update([changed], T0 + timedelta(seconds=10))
    assert view.active[0].first_seen == T0
    assert view.active[0].condition.text == "new text"
    tracker.update([], T0 + timedelta(seconds=11))
    view = tracker.update([RICIRCOLO], T0 + timedelta(seconds=12))
    assert view.active[0].first_seen == T0 + timedelta(seconds=12)


def test_reset() -> None:
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    tracker.reset()
    view = tracker.update([RICIRCOLO], T0 + timedelta(seconds=40))
    assert view.active[0].first_seen == T0 + timedelta(seconds=40)
    assert view.debounced == ()


def _tracked_ids(entries: tuple[TrackedAlarm, ...]) -> list[str]:
    return [entry.condition.id for entry in entries]


def test_short_dropout_of_a_debounced_alarm_is_bridged() -> None:
    """Clear debounce: a matured alarm missing for less than the window never clears."""
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    assert _tracked_ids(tracker.update([RICIRCOLO], T0 + timedelta(seconds=30)).debounced) == [
        RICIRCOLO.id
    ]
    gone = tracker.update([], T0 + timedelta(seconds=40))
    assert gone.active == ()
    assert _tracked_ids(gone.debounced) == [RICIRCOLO.id]  # held, as last seen
    assert gone.debounced[0].first_seen == T0
    assert gone.next_maturity == T0 + timedelta(seconds=70)  # its release
    back = tracker.update([RICIRCOLO], T0 + timedelta(seconds=45))
    assert [entry.first_seen for entry in back.active] == [T0]  # the same streak
    assert [entry.first_seen for entry in back.debounced] == [T0]
    assert back.next_maturity is None


def test_debounced_alarm_is_released_after_the_window() -> None:
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    tracker.update([RICIRCOLO], T0 + timedelta(seconds=30))
    tracker.update([], T0 + timedelta(seconds=40))
    almost = tracker.update([], T0 + timedelta(seconds=69, microseconds=999_999))
    assert _tracked_ids(almost.debounced) == [RICIRCOLO.id]
    released = tracker.update([], T0 + timedelta(seconds=70))
    assert released.debounced == ()
    assert released.next_maturity is None
    # back after the release: a fresh streak that must mature again
    fresh = tracker.update([RICIRCOLO], T0 + timedelta(seconds=71))
    assert fresh.active[0].first_seen == T0 + timedelta(seconds=71)
    assert fresh.debounced == ()
    assert fresh.next_maturity == T0 + timedelta(seconds=101)


def test_held_alarm_and_pending_alarm_share_next_maturity() -> None:
    tracker = AlarmTracker(timedelta(seconds=30))
    tracker.update([RICIRCOLO], T0)
    tracker.update([RICIRCOLO], T0 + timedelta(seconds=30))
    view = tracker.update([BUS], T0 + timedelta(seconds=35))  # RICIRCOLO ends, BUS starts
    assert _tracked_ids(view.active) == [BUS.id]
    assert _tracked_ids(view.debounced) == [RICIRCOLO.id]
    assert view.next_maturity == T0 + timedelta(seconds=65)  # BUS matures, RICIRCOLO released
    view = tracker.update([BUS], T0 + timedelta(seconds=65))
    assert _tracked_ids(view.debounced) == [BUS.id]
    assert view.next_maturity is None
    tracker.update([], T0 + timedelta(seconds=66))  # BUS held now
    tracker.reset()
    assert tracker.update([], T0 + timedelta(seconds=67)).debounced == ()
