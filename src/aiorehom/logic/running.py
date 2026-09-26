"""The program a zone is running today, as the controller web UI computes it.

A behaviour-compatible reimplementation, checked against golden vectors (see
``tools/golden``).  The branch is exposed separately as :func:`program_source`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ..enums import Level, MasterMode, MasterSetPoint, ProgramSource, ZoneSetp
from .schedule import SLOTS_PER_DAY
from .types import Program

__all__ = ["program_source", "running_program"]

_SET_POINT_LEVEL: Final[Mapping[MasterSetPoint | None, Level]] = {
    MasterSetPoint.ECONOMY: Level.ECONOMY,
    MasterSetPoint.PRE_COMFORT: Level.PRE_COMFORT,
    MasterSetPoint.COMFORT: Level.COMFORT,
}
_SETP_LEVEL: Final[Mapping[ZoneSetp | None, Level]] = {
    ZoneSetp.OFF: Level.OFF,
    ZoneSetp.ECONOMY: Level.ECONOMY,
    ZoneSetp.PRE_COMFORT: Level.PRE_COMFORT,
    ZoneSetp.COMFORT: Level.COMFORT,
}


def _constant(level: Level) -> Program:
    return (level,) * SLOTS_PER_DAY


def program_source(
    *,
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    setp: ZoneSetp | None,
    is_crono: bool,
) -> ProgramSource | None:
    """Which branch of the running-program calculation applies; ``None`` = no program.

    The probe check comes first, so ``MODO=0`` with ``SETP=5`` gives PROBE_OFF.
    """
    if mode is MasterMode.OFF or setp is ZoneSetp.PROBE_OFF:
        return ProgramSource.PROBE_OFF if setp is ZoneSetp.PROBE_OFF else ProgramSource.HOUSE_OFF
    if mode is MasterMode.MANUAL:
        return ProgramSource.HOUSE_MANUAL if set_point in _SET_POINT_LEVEL else None
    if mode is MasterMode.AUTO:
        if is_crono:
            return ProgramSource.CRONO
        if setp is ZoneSetp.UNSET:
            return ProgramSource.SCHEDULE
        return ProgramSource.ZONE_MANUAL if setp in _SETP_LEVEL else None
    return None


def running_program(
    *,
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    setp: ZoneSetp | None,
    today: Program | None,
    is_crono: bool,
    crono: Program | None,
) -> Program | None:
    """The zone's 48-slot program for today (same branches as :func:`program_source`).

    * HOUSE_OFF / PROBE_OFF: 48 x OFF.
    * HOUSE_MANUAL: 48 x the master set point's level.
    * CRONO: ``crono``, or 48 x OFF when it is ``None`` (the web UI's default).
    * SCHEDULE: ``today`` (``None`` if unbound, dangling or malformed).
    * ZONE_MANUAL: 48 x ``Level(setp - 1)``.
    * no branch: ``None``.
    """
    if mode is MasterMode.OFF or setp is ZoneSetp.PROBE_OFF:
        return _constant(Level.OFF)
    if mode is MasterMode.MANUAL:
        level = _SET_POINT_LEVEL.get(set_point)
        return None if level is None else _constant(level)
    if mode is MasterMode.AUTO:
        if is_crono:
            return crono if crono is not None else _constant(Level.OFF)
        if setp is ZoneSetp.UNSET:
            return today
        level = _SETP_LEVEL.get(setp)
        return None if level is None else _constant(level)
    return None
