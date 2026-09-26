"""``aiorehom.logic.running``: ``computeRunningProgram`` branches."""

from __future__ import annotations

import itertools

import pytest

from aiorehom.enums import Level, MasterMode, MasterSetPoint, ProgramSource, ZoneSetp
from aiorehom.logic import Program, program_source, running_program, split_program

from ._helpers import runs

TODAY = split_program(runs((1, 24), (3, 20), (1, 4)))
CRONO = split_program(runs((0, 10), (2, 38)))
OFF48: Program = (Level.OFF,) * 48

M = MasterMode
SP = MasterSetPoint
Z = ZoneSetp
PS = ProgramSource


@pytest.mark.parametrize(
    ("mode", "set_point", "setp", "is_crono", "source"),
    [
        (M.OFF, SP.UNSET, Z.UNSET, False, PS.HOUSE_OFF),
        (M.OFF, SP.COMFORT, Z.COMFORT, True, PS.HOUSE_OFF),
        (M.OFF, SP.UNSET, Z.PROBE_OFF, False, PS.PROBE_OFF),  # MODO=0 with SETP=5
        (M.MANUAL, SP.COMFORT, Z.PROBE_OFF, False, PS.PROBE_OFF),
        (M.AUTO, SP.UNSET, Z.PROBE_OFF, True, PS.PROBE_OFF),  # crono with SETP=5
        (None, None, Z.PROBE_OFF, False, PS.PROBE_OFF),
        (M.MANUAL, SP.ECONOMY, Z.UNSET, False, PS.HOUSE_MANUAL),
        (M.MANUAL, SP.PRE_COMFORT, Z.COMFORT, True, PS.HOUSE_MANUAL),
        (M.MANUAL, SP.COMFORT, None, False, PS.HOUSE_MANUAL),
        (M.MANUAL, SP.UNSET, Z.UNSET, False, None),
        (M.MANUAL, None, Z.UNSET, False, None),
        (M.AUTO, SP.UNSET, Z.UNSET, True, PS.CRONO),
        (M.AUTO, SP.UNSET, Z.COMFORT, True, PS.CRONO),
        (M.AUTO, SP.UNSET, None, True, PS.CRONO),
        (M.AUTO, SP.COMFORT, Z.UNSET, False, PS.SCHEDULE),
        (M.AUTO, SP.UNSET, Z.OFF, False, PS.ZONE_MANUAL),
        (M.AUTO, SP.UNSET, Z.ECONOMY, False, PS.ZONE_MANUAL),
        (M.AUTO, SP.UNSET, Z.PRE_COMFORT, False, PS.ZONE_MANUAL),
        (M.AUTO, SP.UNSET, Z.COMFORT, False, PS.ZONE_MANUAL),
        (M.AUTO, SP.UNSET, None, False, None),
        (None, SP.COMFORT, Z.UNSET, False, None),
        (None, None, None, True, None),
    ],
)
def test_program_source(
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    setp: ZoneSetp | None,
    is_crono: bool,
    source: ProgramSource | None,
) -> None:
    assert program_source(mode=mode, set_point=set_point, setp=setp, is_crono=is_crono) is source


def _run(
    mode: MasterMode | None,
    set_point: MasterSetPoint | None,
    setp: ZoneSetp | None,
    *,
    is_crono: bool = False,
    today: Program | None = TODAY,
    crono: Program | None = CRONO,
) -> Program | None:
    return running_program(
        mode=mode, set_point=set_point, setp=setp, today=today, is_crono=is_crono, crono=crono
    )


def test_off_branches() -> None:
    assert _run(M.OFF, SP.COMFORT, Z.UNSET) == OFF48
    assert _run(M.OFF, SP.UNSET, Z.PROBE_OFF) == OFF48
    assert _run(M.AUTO, SP.UNSET, Z.PROBE_OFF, is_crono=True) == OFF48


@pytest.mark.parametrize(
    ("set_point", "level"),
    [(SP.ECONOMY, Level.ECONOMY), (SP.PRE_COMFORT, Level.PRE_COMFORT), (SP.COMFORT, Level.COMFORT)],
)
def test_house_manual(set_point: MasterSetPoint, level: Level) -> None:
    assert _run(M.MANUAL, set_point, Z.ECONOMY) == (level,) * 48


def test_house_manual_without_set_point() -> None:
    assert _run(M.MANUAL, SP.UNSET, Z.UNSET) is None
    assert _run(M.MANUAL, None, Z.UNSET) is None


def test_crono() -> None:
    assert _run(M.AUTO, SP.UNSET, Z.COMFORT, is_crono=True) == CRONO
    # A missing (or malformed) preset 99 runs 48 x OFF, the useRunningProgram default.
    assert _run(M.AUTO, SP.UNSET, Z.UNSET, is_crono=True, crono=None) == OFF48


def test_schedule() -> None:
    assert _run(M.AUTO, SP.UNSET, Z.UNSET) == TODAY
    assert _run(M.AUTO, SP.UNSET, Z.UNSET, today=None) is None


@pytest.mark.parametrize(
    ("setp", "level"),
    [
        (Z.OFF, Level.OFF),
        (Z.ECONOMY, Level.ECONOMY),
        (Z.PRE_COMFORT, Level.PRE_COMFORT),
        (Z.COMFORT, Level.COMFORT),
    ],
)
def test_zone_manual(setp: ZoneSetp, level: Level) -> None:
    assert _run(M.AUTO, SP.UNSET, setp) == (level,) * 48


def test_no_branch() -> None:
    assert _run(M.AUTO, SP.UNSET, None) is None
    assert _run(None, SP.COMFORT, Z.UNSET) is None


def test_running_program_agrees_with_program_source() -> None:
    """``None`` exactly when no branch runs, or SCHEDULE with no program for today."""
    modes = [*MasterMode, None]
    set_points = [*MasterSetPoint, None]
    setps = [*ZoneSetp, None]
    for mode, set_point, setp, crono in itertools.product(modes, set_points, setps, (False, True)):
        source = program_source(mode=mode, set_point=set_point, setp=setp, is_crono=crono)
        for today in (TODAY, None):
            out = _run(mode, set_point, setp, is_crono=crono, today=today)
            expected_none = source is None or (source is PS.SCHEDULE and today is None)
            assert (out is None) == expected_none, (mode, set_point, setp, crono, today)
