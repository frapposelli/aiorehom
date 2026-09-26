"""Parsing and semantic comparison of interface ``Valore`` strings.

Contract file: its behaviour is public API.
The controller writes minimal decimals (``"24"``, ``"-1"``, ``"24.1"``) while
Python-side writers keep a trailing ``.0`` (``"24.0"``): values are always
compared numerically when both sides are numbers, and raw strings are kept
for logging.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

__all__ = [
    "TEMP_TOLERANCE",
    "parse_bool01",
    "parse_decimal",
    "parse_float",
    "parse_int",
    "split_csv",
    "temps_equal",
    "values_equal",
]

#: Two temperatures closer than this are equal (the wire resolution is 0.1 degC).
TEMP_TOLERANCE: Final = 0.05
#: Plain decimal: optional sign, digits with an optional fraction.  No exponent,
#: no underscores, no NaN/Infinity, no thousands separators.
_NUMBER_RE: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def parse_decimal(raw: str | None) -> Decimal | None:
    """``Decimal`` of a plain decimal string (surrounding whitespace ignored), else ``None``."""
    if raw is None:
        return None
    text = raw.strip()
    if _NUMBER_RE.fullmatch(text) is None:
        return None
    return Decimal(text)


def parse_float(raw: str | None) -> float | None:
    """``float`` of a plain decimal string, else ``None``."""
    value = parse_decimal(raw)
    return None if value is None else float(value)


def parse_int(raw: str | None) -> int | None:
    """``int`` of an integral decimal string (``"2"``, ``"2.0"``, ``"-1"``), else ``None``."""
    value = parse_decimal(raw)
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


def parse_bool01(raw: str | None) -> bool | None:
    """``"1"`` -> ``True``, ``"0"`` -> ``False`` (numerically: ``"1.0"`` too), else ``None``."""
    value = parse_int(raw)
    if value == 1:
        return True
    if value == 0:
        return False
    return None


def split_csv(raw: str | None) -> tuple[str, ...]:
    """Split a CSV value into stripped items; ``None`` and ``""`` give ``()``."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(","))


def values_equal(a: str | None, b: str | None) -> bool:
    """Semantic equality of two raw values.

    Equal when the strings are identical, or when both are plain decimals with
    the same numeric value (``"24" == "24.0"``, ``"-1" == "-1.0"``).  ``None``
    equals only ``None``.  CSV and text values are compared as strings.
    """
    if a == b:
        return True
    if a is None or b is None:
        return False
    da = parse_decimal(a)
    db = parse_decimal(b)
    return da is not None and db is not None and da == db


def temps_equal(a: float | None, b: float | None, tolerance: float = TEMP_TOLERANCE) -> bool:
    """Two temperatures are equal within ``tolerance``; ``None`` equals only ``None``."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) < tolerance
