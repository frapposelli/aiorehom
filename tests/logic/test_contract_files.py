"""``enums.py`` and ``values.py`` are contract files.

When a design document is given (``$REHOM_DESIGN_DOC``), both files must
match the listings of its appendix; otherwise that check is skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import aiorehom
from aiorehom.enums import HistorySeries

PACKAGE = Path(aiorehom.__file__).parent
HEADINGS = {
    "enums.py": "### A.1 `src/aiorehom/enums.py`",
    "values.py": "### A.2 `src/aiorehom/values.py`",
}


def _design_doc() -> Path | None:
    env = os.environ.get("REHOM_DESIGN_DOC")
    if not env:
        return None
    path = Path(env)
    return path if path.is_file() else None


def _appendix_listing(doc: str, heading: str) -> str:
    start = doc.index("```python\n", doc.index(heading)) + len("```python\n")
    end = doc.index("\n```\n", start)
    return doc[start:end] + "\n"


@pytest.mark.parametrize("name", sorted(HEADINGS))
def test_contract_file_matches_design_appendix(name: str) -> None:
    doc = _design_doc()
    if doc is None:
        pytest.skip("design notes not available (set REHOM_DESIGN_DOC)")
    expected = _appendix_listing(doc.read_text(encoding="utf-8"), HEADINGS[name])
    assert (PACKAGE / name).read_text(encoding="utf-8") == expected


def test_history_series_per_zone() -> None:
    per_zone = {s for s in HistorySeries if s.per_zone}
    assert per_zone == {
        HistorySeries.TEMP_AMBIENTE,
        HistorySeries.SETP_CORRENTE,
        HistorySeries.UMIDITA,
    }
