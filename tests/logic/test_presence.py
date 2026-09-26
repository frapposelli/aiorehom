"""``aiorehom.logic.presence``."""

from __future__ import annotations

import pytest

from aiorehom.logic import (
    WIDTH_ACTUATORS,
    WIDTH_FANCOILS,
    WIDTH_VMCS,
    WIDTH_ZONES,
    UnitPresence,
    present_units,
    unit_id,
    unit_index,
)

FIXTURE_SONDE = "1,1,1,0,0,0,0,0,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0"


def test_widths() -> None:
    assert (WIDTH_ZONES, WIDTH_FANCOILS, WIDTH_VMCS, WIDTH_ACTUATORS) == (24, 24, 3, 3)


def test_unit_id() -> None:
    assert unit_id(0) == "001"
    assert unit_id(23) == "024"


@pytest.mark.parametrize(
    ("unit", "width", "expected"),
    [
        ("001", 24, 0),
        ("024", 24, 23),
        ("003", 3, 2),
        ("004", 3, None),
        ("025", 24, None),
        ("000", 24, None),
        ("1", 3, None),
        ("2", 3, None),
        ("01", 3, None),
        ("0001", 3, None),
        (" 001", 3, None),
        ("abc", 3, None),
        ("", 3, None),
        ("\u0660\u0660\u0661", 3, None),  # non-ASCII digits
    ],
)
def test_unit_index(unit: str, width: int, expected: int | None) -> None:
    assert unit_index(unit, width=width) == expected


def test_fixture_zones() -> None:
    units = present_units(FIXTURE_SONDE, FIXTURE_SONDE, width=24)
    assert list(units) == ["001", "002", "003", "009", "010", "011"]
    assert units["009"] == UnitPresence(unit="009", index=8, online=True)


def test_online_states() -> None:
    units = present_units("1,1,1,1", "1,0,x,", width=24)
    assert [u.online for u in units.values()] == [True, False, None, None]


@pytest.mark.parametrize("status", [None, "", "1"])
def test_missing_or_short_status_is_unknown(status: str | None) -> None:
    units = present_units("1,0,1", status, width=3)
    assert units["003"].online is None
    if status == "1":
        assert units["001"].online is True
    else:
        assert units["001"].online is None


def test_width_truncation() -> None:
    units = present_units("1,1,0,1,1", "1,1,1,1,1", width=3)
    assert list(units) == ["001", "002"]


def test_whitespace_and_numeric_forms() -> None:
    units = present_units(" 1 , 1.0 ,0, 2 ,true", " 0 ,1.0,1,1,1", width=24)
    assert list(units) == ["001", "002"]
    assert units["001"].online is False
    assert units["002"].online is True


def test_missing_presence() -> None:
    assert present_units(None, "1,1", width=3) == {}
    assert present_units("", None, width=3) == {}
