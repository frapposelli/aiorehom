"""``aiorehom.logic.parse``."""

from __future__ import annotations

from datetime import datetime

import pytest

from aiorehom.enums import (
    Availability,
    FanSpeed,
    Level,
    MasterMode,
    MasterSetPoint,
    Season,
    VmcMode,
    VmcState,
    ZoneSetp,
)
from aiorehom.logic import (
    nonempty,
    parse_availability,
    parse_device_timestamp,
    parse_fan_speed,
    parse_level,
    parse_master_mode,
    parse_master_set_point,
    parse_season,
    parse_vmc_mode,
    parse_vmc_state,
    parse_zone_setp,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", Season.SUMMER),
        ("1.0", Season.SUMMER),
        ("0", Season.WINTER),
        (" 0 ", Season.WINTER),
        ("2", None),  # deviation: the UI treats anything except 1 as winter
        ("", None),
        ("x", None),
        (None, None),
    ],
)
def test_parse_season(raw: str | None, expected: Season | None) -> None:
    assert parse_season(raw) is expected


def test_enum_parsers() -> None:
    assert parse_master_mode("2") is MasterMode.AUTO
    assert parse_master_mode("3") is None
    assert parse_master_mode("1.5") is None
    assert parse_master_set_point("0") is MasterSetPoint.UNSET
    assert parse_master_set_point("4") is None
    assert parse_zone_setp("5") is ZoneSetp.PROBE_OFF
    assert parse_zone_setp("5.0") is ZoneSetp.PROBE_OFF
    assert parse_zone_setp("6") is None
    assert parse_zone_setp("-1") is None
    assert parse_level("3") is Level.COMFORT
    assert parse_level("4") is None
    assert parse_vmc_mode("8") is VmcMode.VENTILATE
    assert parse_vmc_mode("9") is None
    assert parse_vmc_state("3") is VmcState.FORCED
    assert parse_vmc_state(None) is None
    assert parse_fan_speed("4") is FanSpeed.ATTENUATED
    assert parse_fan_speed("50") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2", Availability.WRITABLE),
        ("2.0", Availability.WRITABLE),
        ("1", Availability.READ_ONLY),
        ("3", Availability.READ_ONLY),  # +level > 0 but not 2
        ("0.5", Availability.READ_ONLY),
        ("0", Availability.HIDDEN),
        ("-2", Availability.HIDDEN),
        ("abc", Availability.HIDDEN),
        ("", Availability.HIDDEN),
    ],
)
def test_parse_availability(raw: str, expected: Availability) -> None:
    assert parse_availability(raw, missing=Availability.READ_ONLY) is expected


def test_parse_availability_missing() -> None:
    assert parse_availability(None, missing=Availability.READ_ONLY) is Availability.READ_ONLY
    assert parse_availability(None, missing=Availability.HIDDEN) is Availability.HIDDEN


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-25 12:10:00", datetime(2026, 9, 25, 12, 10)),
        ("2026-09-25T12:44:59", datetime(2026, 9, 25, 12, 44, 59)),
        (" 2026-03-29 02:30:00 ", datetime(2026, 3, 29, 2, 30)),  # naive: DST gap is fine
        ("2026-09-25 12:10", None),
        ("2026-09-25 12:10:00+02:00", None),
        ("2026-09-25 12:10:00Z", None),
        ("2026-09-25 12:10:00.5", None),
        ("2026-02-30 12:10:00", None),
        ("2026-09-25 25:00:00", None),
        ("25/09/2026 12:10:00", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_device_timestamp(raw: str | None, expected: datetime | None) -> None:
    assert parse_device_timestamp(raw) == expected


def test_nonempty() -> None:
    assert nonempty(None) is None
    assert nonempty("") is None
    assert nonempty("   ") is None
    assert nonempty(" Zona 001 ") == "Zona 001"
