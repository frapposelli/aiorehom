"""Zone override ("boost") selection and applicability.

Library rule, stricter than the web UI: the web UI applies any override
whose SubUni equals today's preset, without checking the mode, crono or the
expiry.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ..enums import MasterMode, Season, ZoneSetp
from .parse import parse_device_timestamp
from .schedule import season_suffix, split_override_program
from .types import OverrideRow, OverrideState

__all__ = ["override_applicable", "zone_override"]


def _subuni_order(subuni: str) -> tuple[int, int, str]:
    """Sort key: canonical integers numerically first, then other strings."""
    if subuni.isascii() and subuni.isdigit():
        return (0, int(subuni), subuni)
    return (1, 0, subuni)


def _fallback_row(candidates: Sequence[OverrideRow]) -> OverrideRow:
    """The candidate with the latest expiry (``None`` last); ties -> the lowest SubUni."""
    with_expiry: list[tuple[datetime, OverrideRow]] = []
    without_expiry: list[OverrideRow] = []
    for row in candidates:
        expires_at = parse_device_timestamp(row.expires_at_raw)
        if expires_at is None:
            without_expiry.append(row)
        else:
            with_expiry.append((expires_at, row))
    if with_expiry:
        latest = max(expires_at for expires_at, _ in with_expiry)
        pool = [row for expires_at, row in with_expiry if expires_at == latest]
    else:
        pool = without_expiry
    return min(pool, key=lambda row: _subuni_order(row.subuni))


def override_applicable(*, mode: MasterMode | None, setp: ZoneSetp | None, is_crono: bool) -> bool:
    """Whether the plant/zone state allows an override to apply at all."""
    return mode is MasterMode.AUTO and setp is ZoneSetp.UNSET and not is_crono


def zone_override(
    rows: Sequence[OverrideRow],
    *,
    season: Season | None,
    today_preset: str | None,
    mode: MasterMode | None,
    setp: ZoneSetp | None,
    is_crono: bool,
    now_local: datetime,
) -> OverrideState | None:
    """The zone's primary override row for ``season``, or ``None``.

    Candidates are the rows with ``Key == PROG_GIORNO_<season>``.  The primary
    row is the one bound to ``today_preset`` (the last one if several), else
    the one with the latest expiry (which can apply only on another day).  It
    applies only in AUTO with SETP UNSET, not crono, bound to today's preset,
    with a valid program, both timestamps parsed and ``set_at <= now_local <
    expires_at + 1 s``: ``Scadenza`` is the last second of the override,
    inclusive as a whole second (the UI writes set + H h - 1 s), so it ends at
    the same instant as the one the level-change walker announces.
    """
    if season is None:
        return None
    key = f"PROG_GIORNO_{season_suffix(season)}"
    candidates = [row for row in rows if row.key == key]
    if not candidates:
        return None
    today_rows = [row for row in candidates if row.subuni == today_preset]
    row = today_rows[-1] if today_rows else _fallback_row(candidates)
    program = split_override_program(row.value)
    set_at = parse_device_timestamp(row.set_at_raw)
    expires_at = parse_device_timestamp(row.expires_at_raw)
    applies = (
        override_applicable(mode=mode, setp=setp, is_crono=is_crono)
        and today_preset is not None
        and row.subuni == today_preset
        and program is not None
        and set_at is not None
        and expires_at is not None
        and set_at <= now_local
        # whole-second timestamps: truncating now is ``now < expires_at + 1 s``
        # without overflowing at 9999-12-31 23:59:59 ("forever")
        and now_local.replace(microsecond=0) <= expires_at
    )
    return OverrideState(
        row=row, program=program, set_at=set_at, expires_at=expires_at, applies=applies
    )
