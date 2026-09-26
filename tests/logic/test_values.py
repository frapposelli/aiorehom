"""``aiorehom.values``: parsing and semantic comparison of ``Valore`` strings."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aiorehom.values import (
    TEMP_TOLERANCE,
    parse_bool01,
    parse_decimal,
    parse_float,
    parse_int,
    split_csv,
    temps_equal,
    values_equal,
)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("24", "24.0"),
        ("-1", "-1.0"),
        ("24.1", "24.10"),
        ("0", "-0"),
        ("0", "0.0"),
        (" 24 ", "24"),
        ("+3", "3"),
        (".5", "0.5"),
        ("5.", "5"),
        ("abc", "abc"),
        ("", ""),
        (None, None),
        ("1,2,3", "1,2,3"),
        ("<redacted len=3>", "<redacted len=3>"),
    ],
)
def test_values_equal(a: str | None, b: str | None) -> None:
    assert values_equal(a, b)
    assert values_equal(b, a)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("20.2", "20.200000000000003"),
        ("24", "24.1"),
        (None, ""),
        (None, "0"),
        ("", "0"),
        ("1,2,3", "1,2,3.0"),  # CSV values compare as strings
        ("1,0", "1"),
        ("abc", "ABC"),
        ("<redacted len=3>", "<redacted len=4>"),
        ("1e1", "10"),  # no exponent: not a plain decimal
        ("NaN", "NaN "),
        ("Infinity", "inf"),
    ],
)
def test_values_differ(a: str | None, b: str | None) -> None:
    assert not values_equal(a, b)
    assert not values_equal(b, a)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("24", Decimal(24)),
        ("-1.5", Decimal("-1.5")),
        (" 7 ", Decimal(7)),
        ("+2", Decimal(2)),
        ("", None),
        ("   ", None),
        (None, None),
        ("1e3", None),
        ("1_000", None),
        ("NaN", None),
        ("inf", None),
        ("1,5", None),
        ("0x10", None),
    ],
)
def test_parse_decimal(raw: str | None, expected: Decimal | None) -> None:
    assert parse_decimal(raw) == expected


def test_parse_float() -> None:
    assert parse_float("24.1") == 24.1
    assert parse_float("26") == 26.0
    assert parse_float("abc") is None
    assert parse_float(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2", 2), ("2.0", 2), ("-1", -1), ("-1.0", -1), ("2.5", None), ("x", None), (None, None)],
)
def test_parse_int(raw: str | None, expected: int | None) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("1.0", True),
        ("0", False),
        ("0.0", False),
        ("2", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_bool01(raw: str | None, expected: bool | None) -> None:
    assert parse_bool01(raw) is expected


def test_split_csv() -> None:
    assert split_csv(None) == ()
    assert split_csv("") == ()
    assert split_csv("1, 0 ,1") == ("1", "0", "1")
    assert split_csv(",") == ("", "")


def test_temps_equal_tolerance() -> None:
    assert TEMP_TOLERANCE == 0.05
    assert temps_equal(24.1, 24.1)
    assert temps_equal(24.0, 24.04)
    assert not temps_equal(24.0, 24.05)
    assert not temps_equal(24.1, 24.0)
    assert not temps_equal(26.0, 24.0)
    assert temps_equal(None, None)
    assert not temps_equal(None, 24.0)
    assert not temps_equal(24.0, None)
    assert temps_equal(24.0, 24.4, tolerance=0.5)
