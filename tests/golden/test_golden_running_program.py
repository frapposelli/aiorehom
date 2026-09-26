"""``running_program`` vs the vendor ``computeRunningProgram`` (full cartesian product)."""

from __future__ import annotations

from typing import Any

from aiorehom.logic import (
    Program,
    parse_master_mode,
    parse_master_set_point,
    parse_zone_setp,
    running_program,
    split_program,
)

from ._vectors import cases, load, mismatches, report

NAME = "running_program.json"


def _normalise(program: Program | None) -> str | None:
    """Same normalisation as the generator: level -> digit, invalid slot -> ``"?"``."""
    if program is None:
        return None
    return ",".join("?" if level is None else str(int(level)) for level in program)


def _check(case: dict[str, Any]) -> str | None:
    out = running_program(
        mode=parse_master_mode(case["mode"]),
        set_point=parse_master_set_point(case["set_point"]),
        setp=parse_zone_setp(case["setp"]),
        today=split_program(case["today"]),
        is_crono=case["crono"],
        crono=split_program(case["crono_program"]),
    )
    got = _normalise(out)
    if got != case["out"]:
        inputs = {k: case[k] for k in ("mode", "set_point", "setp", "crono")}
        return f"{inputs} today={case['today'] is not None}: {got!r} != {case['out']!r}"
    return None


def test_meta() -> None:
    meta = load(NAME)["meta"]
    assert meta["function"] == "computeRunningProgram"
    assert meta["count"] == len(cases(NAME)) == 5 * 6 * 7 * 2 * 3 * 2


def test_running_program_matches_vendor_js() -> None:
    failures = mismatches(cases(NAME), _check)
    assert not failures, report(failures)


def test_vectors_cover_every_branch() -> None:
    outs = {case["out"] for case in cases(NAME)}
    assert None in outs
    assert {",".join([d] * 48) for d in "0123"} <= outs
    assert any(out is not None and "?" in out for out in outs)
