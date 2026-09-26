"""The compat program maps vs the vendor ``getProgramIndexMap`` / ``getProgramMap``."""

from __future__ import annotations

from typing import Any

from aiorehom.logic import program_index_map_compat, program_map_compat

from ._vectors import cases, load, mismatches, report, schedule_rows

NAME = "program_map.json"


def _check(case: dict[str, Any]) -> str | None:
    rows = schedule_rows(case["rows"])
    season = case["season"]  # ints stay ints, null is undefined
    index = program_index_map_compat(season, rows)
    programs = program_map_compat(season, rows)
    if index != case["out"]["index"]:
        return f"season={season!r} index {index!r} != {case['out']['index']!r}"
    if programs != case["out"]["programs"]:
        return f"season={season!r} programs {programs!r} != {case['out']['programs']!r}"
    return None


def test_meta() -> None:
    meta = load(NAME)["meta"]
    assert meta["function"] == "getProgramIndexMap+getProgramMap"
    assert meta["count"] == len(cases(NAME)) == 500


def test_program_maps_match_vendor_js() -> None:
    failures = mismatches(cases(NAME), _check)
    assert not failures, report(failures)


def test_vectors_cover_seasons_and_dangling() -> None:
    items = cases(NAME)
    seasons = {repr(case["season"]) for case in items}
    assert seasons == {repr(s) for s in ("0", "1", 1, 0, "", "2", "01", None)}
    assert any(None in case["out"]["programs"].values() for case in items)
    assert any("00" in case["out"]["index"] for case in items)
