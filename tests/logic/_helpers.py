"""Builders shared by the logic tests (synthetic data only)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from aiorehom.enums import Level, MasterMode, MasterSetPoint, Season, ZoneSetp
from aiorehom.logic import (
    OverrideRow,
    ScheduleRow,
    WeekPrograms,
    ZoneInputs,
    season_suffix,
    week_programs,
)

#: Fixture plant temperatures (TEMP_OFF/MAN/PRE/COM = 38/29/26/24, summer).
TEMPERATURES: Mapping[Level, float | None] = {
    Level.OFF: 38.0,
    Level.ECONOMY: 29.0,
    Level.PRE_COMFORT: 26.0,
    Level.COMFORT: 24.0,
}
#: 2026-09-20 is a Sunday; 2026-09-25 (the fixture day) a Friday.
SUNDAY = datetime(2026, 9, 20)
FRIDAY_NOON = datetime(2026, 9, 25, 12, 7, 13)


def csv(levels: Iterable[int | str]) -> str:
    """Comma-join slot values."""
    return ",".join(str(v) for v in levels)


def runs(*segments: tuple[int, int]) -> str:
    """A 48-slot CSV from ``(level, count)`` runs."""
    out: list[int] = []
    for level, count in segments:
        out.extend([level] * count)
    assert len(out) == 48, len(out)
    return csv(out)


def constant(level: int) -> str:
    """48 x ``level``."""
    return csv([level] * 48)


def with_slots(base: int, slots: Iterable[int], level: int) -> str:
    """48 x ``base`` with ``slots`` set to ``level``."""
    values = [base] * 48
    for slot in slots:
        values[slot] = level
    return csv(values)


def prog_rows(
    season: Season, presets: Mapping[str, str], bindings: Mapping[int | str, str]
) -> tuple[ScheduleRow, ...]:
    """``PROG`` rows: ``PROG_GIORNO_<S>`` per preset, then ``PROG_SETT_<S>`` per day."""
    suffix = season_suffix(season)
    rows = [
        ScheduleRow(preset, f"PROG_GIORNO_{suffix}", value) for preset, value in presets.items()
    ]
    rows += [ScheduleRow(str(day), f"PROG_SETT_{suffix}", p) for day, p in bindings.items()]
    return tuple(rows)


def week(
    season: Season, presets: Mapping[str, str], bindings: Mapping[int | str, str]
) -> WeekPrograms:
    """``week_programs`` over PROG rows only."""
    return week_programs(prog_rows(season, presets, bindings), (), season)


def every_day(preset: str) -> dict[int | str, str]:
    """Bind all 7 weekdays to ``preset``."""
    return dict.fromkeys(range(7), preset)


def override_row(
    subuni: str,
    value: str,
    set_at: str | None,
    expires_at: str | None,
    *,
    season: Season = Season.SUMMER,
) -> OverrideRow:
    """A ``PROG_OVERRIDE`` row for ``season``."""
    return OverrideRow(
        subuni=subuni,
        key=f"PROG_GIORNO_{season_suffix(season)}",
        value=value,
        set_at_raw=set_at,
        expires_at_raw=expires_at,
    )


def zone_inputs(**changes: Any) -> ZoneInputs:
    """AUTO / SCHEDULE / summer defaults, preset 1 bound every day; override with ``changes``."""
    base = ZoneInputs(
        mode=MasterMode.AUTO,
        set_point=MasterSetPoint.UNSET,
        season=Season.SUMMER,
        is_crono=False,
        setp=ZoneSetp.UNSET,
        forced=False,
        forced_setpoint=None,
        offset_raw="0",
        calling=False,
        temperatures=TEMPERATURES,
        week=week(Season.SUMMER, {"1": runs((1, 24), (3, 20), (1, 4))}, every_day("1")),
        override_rows=(),
        now_local=FRIDAY_NOON,
    )
    return replace(base, **changes)
