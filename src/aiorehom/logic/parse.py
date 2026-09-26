"""Wire value -> enum parsers and device timestamps.

Every parser takes the raw ``Valore`` exactly as stored (``None`` = row absent)
and returns ``None`` for unknown or unparseable values; none of them raises.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import IntEnum
from typing import Final

from ..enums import (
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
from ..values import parse_decimal, parse_int

__all__ = [
    "nonempty",
    "parse_availability",
    "parse_device_timestamp",
    "parse_fan_speed",
    "parse_level",
    "parse_master_mode",
    "parse_master_set_point",
    "parse_season",
    "parse_vmc_mode",
    "parse_vmc_state",
    "parse_zone_setp",
]

_TIMESTAMP_RE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}")


def _enum_of[E: IntEnum](cls: type[E], raw: str | None) -> E | None:
    value = parse_int(raw)
    if value is None:
        return None
    try:
        return cls(value)
    except ValueError:
        return None


def parse_season(raw: str | None) -> Season | None:
    """``REHOM.STAGIONE``: 1 -> summer, 0 -> winter, anything else -> ``None``.

    Deviation: the web UI treats anything except 1 as winter.
    """
    value = parse_int(raw)
    if value == 1:
        return Season.SUMMER
    if value == 0:
        return Season.WINTER
    return None


def parse_master_mode(raw: str | None) -> MasterMode | None:
    """``REHOM.MODO``."""
    return _enum_of(MasterMode, raw)


def parse_master_set_point(raw: str | None) -> MasterSetPoint | None:
    """``REHOM.SET_POINT``."""
    return _enum_of(MasterSetPoint, raw)


def parse_zone_setp(raw: str | None) -> ZoneSetp | None:
    """``ZONA.<z>..SETP_CORRENTE`` (a level, not a temperature)."""
    return _enum_of(ZoneSetp, raw)


def parse_level(raw: str | None) -> Level | None:
    """One schedule slot value, ``0``..``3``."""
    return _enum_of(Level, raw)


def parse_vmc_mode(raw: str | None) -> VmcMode | None:
    """``DEUM.<d>..ST_MODE`` / ``ST_MODE_FORZATO``."""
    return _enum_of(VmcMode, raw)


def parse_vmc_state(raw: str | None) -> VmcState | None:
    """``DEUM.<d>..ST_STATO_DEUM``."""
    return _enum_of(VmcState, raw)


def parse_fan_speed(raw: str | None) -> FanSpeed | None:
    """Discrete ``COM_VENTILA``."""
    return _enum_of(FanSpeed, raw)


def parse_availability(raw: str | None, *, missing: Availability) -> Availability:
    """An ``ABILITA_*``-style level.

    ``None`` (row absent) gives ``missing``.  Otherwise, numerically: 2 ->
    WRITABLE, any other value > 0 -> READ_ONLY (the UI's ``+level > 0``), and
    anything else (<= 0 or not a number) -> HIDDEN.
    """
    if raw is None:
        return missing
    value = parse_decimal(raw)
    if value is None or value <= 0:
        return Availability.HIDDEN
    if value == 2:
        return Availability.WRITABLE
    return Availability.READ_ONLY


def parse_device_timestamp(raw: str | None) -> datetime | None:
    """Naive device-local ``YYYY-MM-DD HH:MM:SS`` (``" "`` or ``"T"``), no offset, else ``None``."""
    if raw is None:
        return None
    text = raw.strip()
    if _TIMESTAMP_RE.fullmatch(text) is None:
        return None
    try:
        return datetime.strptime(text.replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def nonempty(raw: str | None) -> str | None:
    """The stripped string, or ``None`` if it is missing or empty."""
    if raw is None:
        return None
    text = raw.strip()
    return text or None
