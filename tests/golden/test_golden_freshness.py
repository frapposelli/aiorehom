"""The golden vectors match the current vendor sources, and carry no vendor code.

When the vendor web-UI source is available (``$REHOM_WEBUI_SRC``), each
vector file's recorded source hashes must match; otherwise the vectors are
stale and must be regenerated (see ``tools/golden/README.md``).  Without the
sources (for example on CI), the hash check is skipped.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ._vectors import GOLDEN_DIR, VECTOR_FILES, load, webui_src

EXPECTED_META_KEYS = {
    "function",
    "sources",
    "source_sha256",
    "seed",
    "node",
    "esbuild",
    "lodash",
    "count",
}


@pytest.mark.parametrize("name", VECTOR_FILES)
def test_vectors_are_fresh(name: str) -> None:
    src = webui_src()
    if src is None:
        pytest.skip("vendor web-UI source not available (set REHOM_WEBUI_SRC)")
    meta = load(name)["meta"]
    for rel in meta["sources"]:
        digest = hashlib.sha256((src / rel).read_bytes()).hexdigest()
        assert digest == meta["source_sha256"][rel], (
            f"{name}: {rel} changed; re-run npm run generate in tools/golden"
        )


@pytest.mark.parametrize("name", VECTOR_FILES)
def test_vector_file_shape(name: str) -> None:
    data = load(name)
    assert set(data) == {"meta", "cases"}
    meta = data["meta"]
    assert set(meta) == EXPECTED_META_KEYS
    assert meta["seed"] == 20260925
    assert meta["count"] == len(data["cases"])
    assert set(meta["source_sha256"]) == set(meta["sources"])


@pytest.mark.parametrize("name", VECTOR_FILES)
def test_one_case_per_line(name: str) -> None:
    lines = (GOLDEN_DIR / name).read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith('{"meta": ')
    assert lines[1] == '"cases": ['
    assert lines[-1] == "]}"
    body = lines[2:-1]
    assert len(body) == load(name)["meta"]["count"]
    for line in body:
        json.loads(line.removesuffix(","))


@pytest.mark.parametrize("name", VECTOR_FILES)
def test_no_vendor_code(name: str) -> None:
    text = (GOLDEN_DIR / name).read_text(encoding="utf-8")
    for marker in ("function", "=>", "import ", "export ", "useMemo", "mapValues"):
        body = text.split("\n", 1)[1]  # the meta line names the function
        assert marker not in body, f"{name} contains {marker!r}"
