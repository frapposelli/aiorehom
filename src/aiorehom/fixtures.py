"""Turn a capture directory into sanitised fixtures (redaction again + pseudonymisation)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ._files import ensure_private_dir, write_json, write_private_text
from .inventory import load_capture
from .probe import records_tsv
from .pseudonymise import pseudonymise_fixture
from .redact import redact_capture_file
from .store import RecordStore

__all__ = ["redact_capture_file", "sanitize_capture"]


def sanitize_capture(capture_dir: Path, out_dir: Path, salt: str) -> dict[str, Any]:
    """Write redacted + pseudonymised copies of every JSON/JSONL file to ``out_dir``.

    Other files are skipped (``records.tsv`` is regenerated from the
    pseudonymised records).  Returns a summary with per-file redaction counts.
    """
    if capture_dir.resolve() == out_dir.resolve():
        raise ValueError("the output directory must differ from the capture directory")
    capture = load_capture(capture_dir)
    skipped = sorted(
        p.name for p in capture_dir.iterdir() if p.is_file() and p.suffix not in (".json", ".jsonl")
    )
    redacted: dict[str, Any] = {}
    counts: dict[str, int] = {}
    for name, content in capture.items():
        redacted[name], counts[name] = redact_capture_file(name, content)
    pseudo = pseudonymise_fixture(redacted, salt)
    ensure_private_dir(out_dir)
    for name, content in pseudo.items():
        if name.endswith(".jsonl") and isinstance(content, list):
            text = "".join(
                json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n"
                for line in content
            )
            write_private_text(out_dir / name, text)
        else:
            write_json(out_dir / name, content)
    stores: dict[str, RecordStore] = {}
    for store_name in ("interface", "overrides"):
        data = pseudo.get(f"{store_name}.json")
        if isinstance(data, list):
            stores[store_name] = RecordStore(r for r in data if isinstance(r, Mapping))
    written = sorted(pseudo)
    if stores:
        write_private_text(out_dir / "records.tsv", records_tsv(stores))
        written.append("records.tsv")
    return {
        "files": written,
        "skipped": [s for s in skipped if s != "records.tsv"],
        "redaction_counts": counts,
    }
