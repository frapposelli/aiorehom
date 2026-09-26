"""Minimal normalised record store.

Records are keyed by the 4-tuple ``(Gruppo, Unita, SubUni, Key)`` with every
part normalised to ``str``: ``None`` becomes ``""`` and numbers become their
``str()`` (never a falsy-or: ``SubUni 0`` stays ``"0"``).  ``path`` is always
rebuilt as ``f"{G}.{U}.{S}.{K}"`` with explicit ``str()``.

WebSocket ``termo`` frames are routed by their ``gruppo`` field:
``PROG_OVERRIDE`` frames belong to the overrides store and are stored with
``Gruppo="PROG_OVERRIDE"`` whatever the path prefix says.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

RecordKey = tuple[str, str, str, str]
StoreRole = Literal["interface", "overrides"]

OVERRIDE_GRUPPO: Final = "PROG_OVERRIDE"
_IDENTITY_FIELDS = ("Gruppo", "Unita", "SubUni", "Key")
#: Groups whose Key may contain dots (UTEN: the Key is a user name, e.g. "mario.rossi").
_DOTTED_KEY_GROUPS: Final = frozenset({"UTEN"})
#: Groups whose Key is personal data (a user name); shown as ``<user>`` (``admin`` kept).
_USER_KEY_GROUPS: Final = frozenset({"UTEN"})


def norm(value: object) -> str:
    """Normalise one identity/value field to ``str`` (``None`` -> ``""``)."""
    return "" if value is None else str(value)


def make_path(gruppo: object, unita: object, subuni: object, key: object) -> str:
    """Build the WS address of a record."""
    return f"{norm(gruppo)}.{norm(unita)}.{norm(subuni)}.{norm(key)}"


def record_key(row: Mapping[str, Any]) -> RecordKey:
    """Return the normalised identity tuple of a record."""
    return (
        norm(row.get("Gruppo")),
        norm(row.get("Unita")),
        norm(row.get("SubUni")),
        norm(row.get("Key")),
    )


def split_path(path: object) -> RecordKey | None:
    """Split a WS path into ``(Gruppo, Unita, SubUni, Key)``, or ``None`` for another shape.

    A path must have exactly four dot-separated parts, except in ``UTEN``,
    where the Key is a user name that may itself contain dots: there,
    everything after the third dot is the Key.
    """
    if not isinstance(path, str):
        return None
    parts = path.split(".")
    if len(parts) == 4:
        return (parts[0], parts[1], parts[2], parts[3])
    if len(parts) > 4 and parts[0] in _DOTTED_KEY_GROUPS:
        gruppo, unita, subuni, key = path.split(".", 3)
        return (gruppo, unita, subuni, key)
    return None


def display_key(key: RecordKey) -> RecordKey:
    """``key`` with a ``UTEN`` user name replaced by ``<user>`` (``admin`` kept)."""
    if key[0] in _USER_KEY_GROUPS and key[3] not in ("admin", ""):
        return (key[0], key[1], key[2], "<user>")
    return key


def display_path(key: RecordKey) -> str:
    """Record path safe to print: ``UTEN`` user names are masked."""
    return make_path(*display_key(key))


def normalise_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a normalised copy of a record.

    Identity fields and ``Valore`` become strings, ``path`` is rebuilt, and a
    received ``path`` that differs from the rebuilt one is kept as
    ``path_received`` (a live check for Q9).
    """
    key = record_key(row)
    out: dict[str, Any] = dict(zip(_IDENTITY_FIELDS, key, strict=True))
    out["Valore"] = norm(row.get("Valore"))
    for name, value in row.items():
        if name in out or name == "path":
            continue
        out[name] = value
    out["path"] = make_path(*key)
    received = row.get("path")
    if received is not None and received != out["path"]:
        out["path_received"] = received
    return out


class RecordStore:
    """A dictionary of records keyed by normalised identity.

    ``role`` (``"interface"`` or ``"overrides"``) makes :meth:`apply_ws` accept
    only the frames that belong to that store; ``None`` accepts both kinds.
    """

    def __init__(
        self, rows: Iterable[Mapping[str, Any]] | None = None, *, role: StoreRole | None = None
    ) -> None:
        if role not in (None, "interface", "overrides"):
            raise ValueError("role must be 'interface', 'overrides' or None")
        self.role: StoreRole | None = role
        self._rows: dict[RecordKey, dict[str, Any]] = {}
        self._raw_rows = 0
        self._duplicates: dict[RecordKey, int] = {}
        if rows is not None:
            self.load(rows)

    def load(self, rows: Iterable[Mapping[str, Any]]) -> None:
        """Replace the store content with ``rows`` (non-mapping items are skipped).

        Duplicate identities: the last row wins (same as the UI's ``keyBy``);
        they are counted in :attr:`duplicates` and :attr:`raw_rows`.
        """
        self._rows = {}
        self._raw_rows = 0
        self._duplicates = {}
        for row in rows:
            if isinstance(row, Mapping):
                self._raw_rows += 1
                normalised = normalise_row(row)
                key = record_key(normalised)
                if key in self._rows:
                    self._duplicates[key] = self._duplicates.get(key, 1) + 1
                self._rows[key] = normalised

    @property
    def raw_rows(self) -> int:
        """Number of rows given to the last :meth:`load` (duplicates included)."""
        return self._raw_rows

    @property
    def duplicates(self) -> dict[str, int]:
        """``{display path: rows sharing that identity}`` for identities loaded more than once.

        ``UTEN`` user names are masked in the paths.
        """
        return {display_path(key): count for key, count in sorted(self._duplicates.items())}

    def apply_ws(self, frame: Mapping[str, Any]) -> bool:
        """Apply a ``termo`` update/remove frame.

        Routing is by ``gruppo``: a ``PROG_OVERRIDE`` frame (or, without
        ``gruppo``, one whose path starts with ``PROG_OVERRIDE``) is stored with
        ``Gruppo="PROG_OVERRIDE"`` whatever its path prefix, and is ignored by a
        store with ``role="interface"``; a store with ``role="overrides"``
        ignores every other frame.  A new row keeps a differing received path as
        ``path_received``.  Returns ``True`` when the store changed.  Frames of
        another domain or with an unparseable path are ignored.
        """
        if frame.get("domain") != "termo":
            return False
        parts = split_path(frame.get("path"))
        if parts is None:
            return False
        gruppo = frame.get("gruppo")
        is_override = gruppo == OVERRIDE_GRUPPO or (gruppo is None and parts[0] == OVERRIDE_GRUPPO)
        if (self.role == "interface" and is_override) or (
            self.role == "overrides" and not is_override
        ):
            return False
        key: RecordKey = (OVERRIDE_GRUPPO, parts[1], parts[2], parts[3]) if is_override else parts
        kind = frame.get("type")
        if kind == "remove":
            return self._rows.pop(key, None) is not None
        if kind != "update":
            return False
        value = norm(frame.get("value"))
        current = self._rows.get(key)
        if current is None:
            current = {
                "Gruppo": key[0],
                "Unita": key[1],
                "SubUni": key[2],
                "Key": key[3],
                "Valore": value,
                "path": make_path(*key),
            }
            received = frame.get("path")
            if received != current["path"]:
                current["path_received"] = received
            self._rows[key] = current
            changed = True
        else:
            changed = current.get("Valore") != value
            current["Valore"] = value
        for src, dst in (("issuedAt", "Impostazione"), ("expiresAt", "Scadenza")):
            if src in frame:
                changed = changed or current.get(dst) != frame[src]
                current[dst] = frame[src]
        return changed

    def get(self, key: RecordKey) -> dict[str, Any] | None:
        row = self._rows.get(key)
        return dict(row) if row is not None else None

    def value(self, gruppo: str, unita: str, subuni: str, key: str) -> str | None:
        row = self._rows.get((gruppo, unita, subuni, key))
        return None if row is None else norm(row.get("Valore"))

    def keys(self) -> list[RecordKey]:
        return sorted(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, key: object) -> bool:
        return key in self._rows

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.to_rows())

    def to_rows(self) -> list[dict[str, Any]]:
        """Return copies of every row, sorted by identity."""
        return [dict(self._rows[key]) for key in sorted(self._rows)]


@dataclass
class StoreDiff:
    """Record-level difference between two stores."""

    added: list[dict[str, Any]] = field(default_factory=list)
    removed: list[dict[str, Any]] = field(default_factory=list)
    changed: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> dict[str, Any]:
        return {"added": self.added, "removed": self.removed, "changed": self.changed}


_DIFF_IGNORED_FIELDS = frozenset({"path", "path_received"})


def diff(
    a: RecordStore | Iterable[Mapping[str, Any]], b: RecordStore | Iterable[Mapping[str, Any]]
) -> StoreDiff:
    """Compare two stores (or row lists).

    ``changed`` entries are ``{"key", "path", "old", "new"}`` where old/new are
    the ``Valore`` strings, plus ``"fields": {name: [old, new]}`` when other
    fields (e.g. ``Scadenza``) differ.
    """
    store_a = a if isinstance(a, RecordStore) else RecordStore(a)
    store_b = b if isinstance(b, RecordStore) else RecordStore(b)
    keys_a = set(store_a.keys())
    keys_b = set(store_b.keys())
    result = StoreDiff()
    for key in sorted(keys_b - keys_a):
        row = store_b.get(key)
        if row is not None:
            result.added.append(row)
    for key in sorted(keys_a - keys_b):
        row = store_a.get(key)
        if row is not None:
            result.removed.append(row)
    for key in sorted(keys_a & keys_b):
        old = store_a.get(key) or {}
        new = store_b.get(key) or {}
        fields: dict[str, list[Any]] = {}
        for name in sorted(set(old) | set(new)):
            if name in _DIFF_IGNORED_FIELDS or name == "Valore":
                continue
            if old.get(name) != new.get(name):
                fields[name] = [old.get(name), new.get(name)]
        if old.get("Valore") != new.get("Valore") or fields:
            entry: dict[str, Any] = {
                "key": list(key),
                "path": make_path(*key),
                "old": old.get("Valore"),
                "new": new.get("Valore"),
            }
            if fields:
                entry["fields"] = fields
            result.changed.append(entry)
    return result
