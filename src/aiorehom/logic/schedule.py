"""Zone schedules: slots, programs and week maps.

``*_compat`` functions reproduce how the controller web UI builds a zone's
week map (behaviour-compatible reimplementations, checked against golden
vectors); :func:`week_programs` is the library policy built on top of them.
Times are naive device-local wall time.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final

from ..enums import Level, ScheduleSource, Season
from ..values import parse_int
from .types import Program, ScheduleRow, WeekPrograms

__all__ = [
    "CRONO_PRESET",
    "SLOTS_PER_DAY",
    "SLOT_DURATION",
    "js_unary_plus",
    "next_slot_boundary",
    "program_index_map_compat",
    "program_map_compat",
    "season_suffix",
    "slot_of",
    "slot_start",
    "split_override_program",
    "split_program",
    "week_programs",
    "weekday_js",
]

SLOTS_PER_DAY: Final = 48
SLOT_DURATION: Final = timedelta(minutes=30)
#: Reserved zone preset holding the crono program.
CRONO_PRESET: Final = "99"
_WEEKDAY_SUBUNI: Final = ("0", "1", "2", "3", "4", "5", "6")

# ECMAScript WhiteSpace + LineTerminator code points (StringToNumber trims these).
_JS_WHITESPACE: Final = "\t\n\v\f\r " + "".join(
    chr(code)
    for code in (
        0x00A0,
        0x1680,
        *range(0x2000, 0x200B),
        0x2028,
        0x2029,
        0x202F,
        0x205F,
        0x3000,
        0xFEFF,
    )
)
_JS_DECIMAL_RE: Final = re.compile(
    r"[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
)
_JS_RADIX_RE: Final = re.compile(r"0(?:([xX])([0-9a-fA-F]+)|([oO])([0-7]+)|([bB])([01]+))")


# ---------------------------------------------------------------------------
# Slot helpers
# ---------------------------------------------------------------------------


def slot_of(t: datetime) -> int:
    """Half-hour slot of the day, 0..47."""
    return (t.hour * 60 + t.minute) // 30


def weekday_js(t: datetime) -> int:
    """JS ``Date.getDay()``: 0 = Sunday .. 6 = Saturday."""
    return t.isoweekday() % 7


def slot_start(t: datetime) -> datetime:
    """``t`` floored to :00/:30 (seconds and microseconds zeroed)."""
    return t.replace(minute=30 if t.minute >= 30 else 0, second=0, microsecond=0)


def next_slot_boundary(t: datetime) -> datetime:
    """Start of the slot after the one containing ``t``."""
    return slot_start(t) + SLOT_DURATION


def season_suffix(season: Season) -> str:
    """Schedule key suffix: ``"ESTATE"`` (summer) or ``"INVERNO"`` (winter)."""
    return "ESTATE" if season is Season.SUMMER else "INVERNO"


# ---------------------------------------------------------------------------
# Program strings
# ---------------------------------------------------------------------------


def _levels(items: Sequence[str]) -> Program:
    out: list[Level | None] = []
    for item in items:
        value = parse_int(item)
        out.append(Level(value) if value is not None and 0 <= value <= 3 else None)
    return tuple(out)


def split_program(raw: str | None) -> Program | None:
    """A 48-value ``PROG_GIORNO_*`` CSV -> levels; ``None`` if missing, empty or not 48 items.

    An item that is not an integer 0..3 becomes a ``None`` slot.
    """
    if not raw:
        return None
    items = raw.split(",")
    if len(items) != SLOTS_PER_DAY:
        return None
    return _levels(items)


def split_override_program(raw: str | None) -> Program | None:
    """Like :func:`split_program`, but >= 48 items are accepted and the first 48 used (UI)."""
    if not raw:
        return None
    items = raw.split(",")
    if len(items) < SLOTS_PER_DAY:
        return None
    return _levels(items[:SLOTS_PER_DAY])


# ---------------------------------------------------------------------------
# Web UI-compatible week map
# ---------------------------------------------------------------------------


def js_unary_plus(value: str | int | None) -> float:
    """ECMAScript ``+value`` for a string, a number or ``undefined`` (``None``).

    Strings follow StringToNumber: surrounding JS whitespace is trimmed, ``""``
    is 0, decimal literals (with exponent), ``Infinity`` and unsigned
    ``0x``/``0o``/``0b`` literals are numbers, and everything else is NaN.
    """
    if value is None:
        return math.nan
    if isinstance(value, int):
        return float(value)
    text = value.strip(_JS_WHITESPACE)
    if not text:
        return 0.0
    if _JS_DECIMAL_RE.fullmatch(text) is not None:
        return float(text.replace("Infinity", "inf"))
    radix = _JS_RADIX_RE.fullmatch(text)
    if radix is not None:
        digits = radix.group(2) or radix.group(4) or radix.group(6)
        base = 16 if radix.group(1) else 8 if radix.group(3) else 2
        return float(int(digits, base))
    return math.nan


def _compat_suffix(season_raw: str | int | None) -> str:
    return "ESTATE" if js_unary_plus(season_raw) == 1.0 else "INVERNO"


def program_index_map_compat(
    season_raw: str | int | None, rows: Sequence[ScheduleRow]
) -> dict[str, str]:
    """Web UI-compatible index map: SubUni -> preset id of ``PROG_SETT_<S>`` rows.

    ``S`` is ESTATE when the season converts to the number 1 (see
    :func:`js_unary_plus`), else INVERNO.  Rows are read in order and the last
    one wins.
    """
    key = f"PROG_SETT_{_compat_suffix(season_raw)}"
    out: dict[str, str] = {}
    for row in rows:
        if row.key == key:
            out[row.subuni] = row.value
    return out


def _programs(rows: Sequence[ScheduleRow], suffix: str) -> dict[str, str]:
    key = f"PROG_GIORNO_{suffix}"
    out: dict[str, str] = {}
    for row in rows:
        if row.key == key:
            out[row.subuni] = row.value
    return out


def program_map_compat(
    season_raw: str | int | None, rows: Sequence[ScheduleRow]
) -> dict[str, str | None]:
    """Web UI-compatible week map: index-map key -> program CSV (``None`` = dangling)."""
    programs = _programs(rows, _compat_suffix(season_raw))
    return {
        day: programs.get(preset)
        for day, preset in program_index_map_compat(season_raw, rows).items()
    }


# ---------------------------------------------------------------------------
# Library policy
# ---------------------------------------------------------------------------


def _has_season_rows(rows: Sequence[ScheduleRow], suffix: str) -> bool:
    keys = (f"PROG_SETT_{suffix}", f"PROG_GIORNO_{suffix}")
    return any(row.key in keys for row in rows)


def week_programs(
    prog_rows: Sequence[ScheduleRow], mirror_rows: Sequence[ScheduleRow], season: Season
) -> WeekPrograms:
    """A zone's weekly programs for ``season`` (library policy).

    The rows come from ``PROG`` when it holds any ``PROG_SETT_<S>`` or
    ``PROG_GIORNO_<S>`` row, else from the ZONA mirror when that does; the two
    sources are never mixed.  Only SubUni ``"0"``..``"6"`` bind weekdays (exact
    strings: ``"00"`` does not bind Sunday).  A dangling binding gives a
    ``None`` program (the web UI throws there).  Preset ``"99"`` is the crono
    program.
    """
    suffix = season_suffix(season)
    rows: Sequence[ScheduleRow]
    if _has_season_rows(prog_rows, suffix):
        rows, source = prog_rows, ScheduleSource.PROG
    elif _has_season_rows(mirror_rows, suffix):
        rows, source = mirror_rows, ScheduleSource.ZONA_MIRROR
    else:
        return WeekPrograms(
            index_map=MappingProxyType({}),
            program_map=MappingProxyType({}),
            crono=None,
            source=ScheduleSource.NONE,
        )
    season_raw = "1" if season is Season.SUMMER else "0"
    raw_index = program_index_map_compat(season_raw, rows)
    programs = _programs(rows, suffix)
    index_map: dict[int, str] = {}
    program_map: dict[int, Program | None] = {}
    for day, subuni in enumerate(_WEEKDAY_SUBUNI):
        preset = raw_index.get(subuni)
        if preset is None:
            continue
        index_map[day] = preset
        program_map[day] = split_program(programs.get(preset))
    return WeekPrograms(
        index_map=MappingProxyType(index_map),
        program_map=MappingProxyType(program_map),
        crono=split_program(programs.get(CRONO_PRESET)),
        source=source,
    )
