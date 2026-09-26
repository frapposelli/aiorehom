"""Private file output: files 0o600, directories 0o700, JSON and JSONL helpers."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

FILE_MODE: Final = 0o600
DIR_MODE: Final = 0o700


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with milliseconds and a ``Z`` suffix."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_stamp() -> str:
    """Compact UTC timestamp for directory names, e.g. ``20260925T101500Z``."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` and any missing parents with mode 0o700.

    Directories that already exist are left untouched (their mode is the
    user's business); every directory created here is exactly 0o700.
    """
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=DIR_MODE, exist_ok=True)
        os.chmod(directory, DIR_MODE)
    if not path.is_dir():
        raise NotADirectoryError(str(path))
    return path


def _open_private(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def write_private_bytes(path: Path, data: bytes) -> None:
    fd = _open_private(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_private_text(path: Path, text: str) -> None:
    write_private_bytes(path, text.encode("utf-8"))


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_json(path: Path, obj: Any) -> None:
    write_private_text(path, dump_json(obj))


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[Any]:
    lines: list[Any] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                lines.append(json.loads(stripped))
    return lines


class JsonlWriter:
    """Append-only JSONL file (mode 0o600), flushed after every line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        fd = _open_private(path, os.O_WRONLY | os.O_APPEND)
        self._handle = os.fdopen(fd, "a", encoding="utf-8")
        self.lines = 0

    def write(self, obj: Any) -> None:
        self._handle.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._handle.flush()
        self.lines += 1

    def write_timestamped(self, kind: str, payload: Any) -> None:
        """Write ``{"t": <UTC ISO>, "mono": <monotonic s>, kind: payload}``."""
        self.write({"t": utc_now_iso(), "mono": round(time.monotonic(), 3), kind: payload})

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
