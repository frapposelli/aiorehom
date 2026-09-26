"""Live stores of the read path: records, plant conf, config, alive and forecast.

:class:`StoreSet` holds everything the controller told us, keyed exactly as on
the wire (the raw string 4-tuple ``(Gruppo, Unita, SubUni, Key)``; ``DEUM.1``,
``DEUM.001`` and ``DEUM.000`` are three different records).  It applies
WebSocket frames (:meth:`StoreSet.apply_frame`) and REST snapshots
(:meth:`StoreSet.replace`) and reports what changed *semantically* as a
:class:`ChangeSet`: a frame whose value is numerically equal to the stored one
(``"24"`` vs ``"24.0"``) is not a change and leaves the stored spelling alone
(so it can never move the built state), and a ``remove`` followed within
``remove_coalesce_window`` seconds by an ``update`` of the same record is a
replace, not a removal.  Values that are identifiers rather than numbers
(``PROG_SETT*`` preset ids: ``"01"`` does not name preset ``"1"``) are
compared as exact strings.

``METEO_DATA`` frames never become records: they feed the WS-only
:class:`ForecastCache`.

Everything that reaches a store has been through :mod:`aiorehom.redact`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Protocol

from .exceptions import RehomResponseError
from .redact import redact_dict, redact_records, redact_ws_frame
from .store import OVERRIDE_GRUPPO, RecordKey, make_path, norm, record_key, split_path
from .values import values_equal

__all__ = [
    "LOCAL_TIME_KEY",
    "ChangeSet",
    "ForecastCache",
    "ForecastSnapshot",
    "RowView",
    "Snapshot",
    "SnapshotRow",
    "StoreCounters",
    "StoreSet",
    "StoreView",
    "config_diff",
    "parse_config",
    "parse_plant_conf",
    "parse_record_rows",
    "record_values_equal",
]

#: ``/api/config/`` key that changes every second; never a change.
LOCAL_TIME_KEY: Final = "LOCAL_TIME"
METEO_DATA_GRUPPO: Final = "METEO_DATA"
_OVERRIDE_PROGRAM_PREFIX: Final = "PROG_GIORNO_"
_FORECAST_KEY_RE: Final = re.compile(r"dt(\d+)")
#: Keys whose value is an identifier matched as an exact string (a weekday's
#: preset id is looked up by ``SubUni``), so numeric equality does not apply.
_EXACT_VALUE_KEY_PREFIXES: Final = ("PROG_SETT",)


# ---------------------------------------------------------------------------
# Views handed to the builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RowView:
    """Read-only copy of one record row."""

    gruppo: str
    unita: str
    subuni: str
    key: str
    value: str
    impostazione: str | None
    scadenza: str | None
    changed_at: datetime
    frame_at: datetime | None

    @property
    def path(self) -> str:
        return make_path(self.gruppo, self.unita, self.subuni, self.key)


@dataclass(frozen=True, slots=True)
class ForecastSnapshot:
    """The ``METEO_DATA`` items of the current burst."""

    received_at: datetime  # first frame of the current burst
    items: Mapping[int, str]  # dt epoch -> raw JSON string


class StoreView(Protocol):
    """What the state builder may read.  Valid only during one synchronous ``build()``."""

    @property
    def synced_at(self) -> datetime: ...
    @property
    def live_since(self) -> datetime | None: ...
    @property
    def alive(self) -> Mapping[str, Any]: ...
    @property
    def config(self) -> Mapping[str, Any]: ...
    @property
    def plant_conf(self) -> Mapping[str, str]: ...
    @property
    def forecast(self) -> ForecastSnapshot | None: ...
    @property
    def last_commissioning_at(self) -> datetime | None: ...
    def value(self, gruppo: str, unita: str, subuni: str, key: str) -> str | None: ...
    def row(self, gruppo: str, unita: str, subuni: str, key: str) -> RowView | None: ...
    def rows(self, gruppo: str, unita: str | None = None) -> Iterator[RowView]: ...
    def rows_with_key(self, key: str) -> Iterator[RowView]: ...
    def override_rows(self) -> Iterator[RowView]: ...


# ---------------------------------------------------------------------------
# Change accounting
# ---------------------------------------------------------------------------


@dataclass
class ChangeSet:
    """Mutable accumulator of semantic changes.

    ``changed`` holds record paths (``G.U.S.K``; override rows as
    ``PROG_OVERRIDE.<u>.<s>.<k>``) that were added, changed or removed;
    ``removed`` those that no longer exist (always a subset of ``changed``).
    ``touched`` marks a store change that has no path (the commissioning
    timestamp, the WS live flag): it asks for a rebuild but is never reported.
    """

    changed: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)
    conf_changed: set[str] = field(default_factory=set)
    config_changed: set[str] = field(default_factory=set)
    forecast_changed: bool = False
    touched: bool = False

    def empty(self) -> bool:
        """True when nothing at all changed (not even a path-less store change)."""
        return not self.reportable() and not self.touched

    def reportable(self) -> bool:
        """True when a consumer-visible change set is non-empty."""
        return bool(
            self.changed
            or self.removed
            or self.conf_changed
            or self.config_changed
            or self.forecast_changed
        )

    def merge(self, other: ChangeSet) -> None:
        """Add ``other`` (a later change) to this one.

        ``changed`` is never netted out; a path removed earlier and re-added by
        ``other`` leaves ``removed``.
        """
        self.changed |= other.changed
        self.removed -= other.changed - other.removed
        self.removed |= other.removed
        self.conf_changed |= other.conf_changed
        self.config_changed |= other.config_changed
        self.forecast_changed = self.forecast_changed or other.forecast_changed
        self.touched = self.touched or other.touched


@dataclass
class StoreCounters:
    """Frame accounting of one :class:`StoreSet` (feeds ``SyncStats``)."""

    frames_ignored: int = 0
    noop_updates: int = 0
    changed_updates: int = 0
    removes_coalesced: int = 0
    removes_applied: int = 0
    forecast_frames: int = 0


# ---------------------------------------------------------------------------
# Snapshots (REST) and their validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    key: RecordKey
    value: str
    impostazione: str | None = None
    scadenza: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A validated, redacted REST snapshot (not yet applied)."""

    interface: tuple[SnapshotRow, ...]
    overrides: tuple[SnapshotRow, ...]
    plant_conf: Mapping[str, str]
    config: Mapping[str, Any]
    config_received_at: datetime


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _unexpected(path: str) -> RehomResponseError:
    return RehomResponseError(None, f"GET {path}: unexpected payload")


def parse_record_rows(payload: object, *, path: str, overrides: bool = False) -> list[SnapshotRow]:
    """Validate and redact an ``/interface/`` or ``/overrides/`` payload.

    The payload must be a list; items that are not mappings are skipped.  For
    the overrides store, a row whose ``Key`` starts with ``PROG_GIORNO_`` is
    re-grouped under ``PROG_OVERRIDE`` whatever the REST ``Gruppo`` says.
    """
    if not isinstance(payload, list):
        raise _unexpected(path)
    redacted, _count = redact_records(payload)
    out: list[SnapshotRow] = []
    for item in redacted:
        if not isinstance(item, Mapping):
            continue
        key = record_key(item)
        if overrides and key[3].startswith(_OVERRIDE_PROGRAM_PREFIX):
            key = (OVERRIDE_GRUPPO, key[1], key[2], key[3])
        out.append(
            SnapshotRow(
                key=key,
                value=norm(item.get("Valore")),
                impostazione=_opt_str(item.get("Impostazione")),
                scadenza=_opt_str(item.get("Scadenza")),
            )
        )
    return out


def parse_plant_conf(payload: object) -> dict[str, str]:
    """Validate and redact a ``/plant/conf/`` payload into ``{str: str}``."""
    if not isinstance(payload, Mapping):
        raise _unexpected("/api/plant/conf/")
    redacted, _count = redact_dict(payload)
    return {str(k): norm(v) for k, v in redacted.items()}


def parse_config(payload: object) -> dict[str, Any]:
    """Validate and redact a ``/config/`` payload."""
    if not isinstance(payload, Mapping):
        raise _unexpected("/api/config/")
    redacted, _count = redact_dict(payload)
    return {str(k): v for k, v in redacted.items()}


def _config_values_equal(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return values_equal(a, b)
    return type(a) is type(b) and a == b


def config_diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> set[str]:
    """Keys added, removed or changed between two configs (``LOCAL_TIME`` ignored)."""
    keys = (set(old) | set(new)) - {LOCAL_TIME_KEY}
    out: set[str] = set()
    for key in keys:
        if (key in old) != (key in new) or not _config_values_equal(old.get(key), new.get(key)):
            out.add(key)
    return out


# ---------------------------------------------------------------------------
# Forecast cache
# ---------------------------------------------------------------------------


class ForecastCache:
    """``METEO_DATA`` items of the latest burst (WS only, never in a REST snapshot).

    A frame more than ``burst_gap`` seconds after the previous one starts a new
    burst: the items are cleared and ``received_at`` restarts.
    """

    def __init__(self, burst_gap: float = 60.0) -> None:
        self._gap = burst_gap
        self._items: dict[int, str] = {}
        self._received_at: datetime | None = None
        self._last_mono: float | None = None
        self._snapshot: ForecastSnapshot | None = None

    def _touch(self, received_at: datetime, received_mono: float) -> None:
        if self._last_mono is None or received_mono - self._last_mono > self._gap:
            self._items = {}
            self._received_at = received_at
        self._last_mono = received_mono
        self._snapshot = None

    def apply(self, key: str, value: str, received_at: datetime, received_mono: float) -> bool:
        """Store ``items[int(key[2:])] = value``; ``False`` (ignored) unless ``key`` = ``dt<n>``."""
        match = _FORECAST_KEY_RE.fullmatch(key)
        if match is None:
            return False
        self._touch(received_at, received_mono)
        self._items[int(match.group(1))] = value
        return True

    def remove(self, key: str, received_at: datetime, received_mono: float) -> bool:
        """Drop one item (a ``remove`` frame); ``False`` (ignored) unless ``key`` is ``dt<n>``."""
        match = _FORECAST_KEY_RE.fullmatch(key)
        if match is None:
            return False
        self._touch(received_at, received_mono)
        self._items.pop(int(match.group(1)), None)
        return True

    def snapshot(self) -> ForecastSnapshot | None:
        """The current burst, or ``None`` before the first ``METEO_DATA`` frame."""
        if self._received_at is None:
            return None
        if self._snapshot is None:
            self._snapshot = ForecastSnapshot(
                received_at=self._received_at, items=MappingProxyType(dict(self._items))
            )
        return self._snapshot


# ---------------------------------------------------------------------------
# Record store with indexes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Row:
    value: str
    impostazione: str | None
    scadenza: str | None
    changed_at: datetime
    frame_at: datetime | None


def _view(key: RecordKey, row: _Row) -> RowView:
    return RowView(
        gruppo=key[0],
        unita=key[1],
        subuni=key[2],
        key=key[3],
        value=row.value,
        impostazione=row.impostazione,
        scadenza=row.scadenza,
        changed_at=row.changed_at,
        frame_at=row.frame_at,
    )


def record_values_equal(key: str, a: str, b: str) -> bool:
    """Semantic equality of two ``Valore`` strings of the record ``Key`` ``key``.

    Numeric-aware (``values_equal``) except for identifier keys (``PROG_SETT*``),
    which must match exactly.
    """
    if key.startswith(_EXACT_VALUE_KEY_PREFIXES):
        return a == b
    return values_equal(a, b)


def _same_row(
    key: RecordKey, row: _Row, value: str, impostazione: str | None, scadenza: str | None
) -> bool:
    return (
        record_values_equal(key[3], row.value, value)
        and row.impostazione == impostazione
        and row.scadenza == scadenza
    )


class _RecordStore:
    """Insertion-ordered records plus (gruppo), (gruppo, unita) and key indexes."""

    def __init__(self) -> None:
        self.rows: dict[RecordKey, _Row] = {}
        self._by_g: dict[str, dict[RecordKey, _Row]] = {}
        self._by_gu: dict[tuple[str, str], dict[RecordKey, _Row]] = {}
        self._by_k: dict[str, dict[RecordKey, _Row]] = {}
        #: Pending removes: key -> monotonic deadline.
        self.pending: dict[RecordKey, float] = {}

    def insert(self, key: RecordKey, row: _Row) -> None:
        self.rows[key] = row
        self._by_g.setdefault(key[0], {})[key] = row
        self._by_gu.setdefault((key[0], key[1]), {})[key] = row
        self._by_k.setdefault(key[3], {})[key] = row

    def drop(self, key: RecordKey) -> None:
        del self.rows[key]
        self.pending.pop(key, None)
        _drop(self._by_g, key[0], key)
        _drop(self._by_gu, (key[0], key[1]), key)
        _drop(self._by_k, key[3], key)

    def load(self, rows: Iterable[tuple[RecordKey, _Row]]) -> None:
        """Replace everything with ``rows`` (unique keys, store order)."""
        self.rows = {}
        self._by_g = {}
        self._by_gu = {}
        self._by_k = {}
        self.pending = {}
        for key, row in rows:
            self.insert(key, row)

    def by_gruppo(self, gruppo: str, unita: str | None) -> Iterator[tuple[RecordKey, _Row]]:
        bucket = self._by_g.get(gruppo) if unita is None else self._by_gu.get((gruppo, unita))
        if bucket:
            yield from list(bucket.items())

    def by_key(self, key: str) -> Iterator[tuple[RecordKey, _Row]]:
        bucket = self._by_k.get(key)
        if bucket:
            yield from list(bucket.items())

    def replace(self, snapshot_rows: Iterable[SnapshotRow], at: datetime, out: ChangeSet) -> None:
        """Swap in ``snapshot_rows``; record added/changed/removed paths in ``out``.

        Duplicate identities in the snapshot: the last row wins, at the position
        of the first (the UI's ``keyBy``).
        """
        latest: dict[RecordKey, SnapshotRow] = {}
        for snap in snapshot_rows:
            latest[snap.key] = snap
        old = self.rows
        new_rows: list[tuple[RecordKey, _Row]] = []
        for key, snap in latest.items():
            previous = old.get(key)
            value = snap.value
            if previous is not None and _same_row(
                key, previous, snap.value, snap.impostazione, snap.scadenza
            ):
                # a semantic no-op keeps the stored spelling: the state cannot move
                value, changed_at, frame_at = previous.value, previous.changed_at, previous.frame_at
            else:
                changed_at = at
                frame_at = None if previous is None else previous.frame_at
                out.changed.add(make_path(*key))
            new_rows.append(
                (key, _Row(value, snap.impostazione, snap.scadenza, changed_at, frame_at))
            )
        for key in old:
            if key not in latest:
                path = make_path(*key)
                out.changed.add(path)
                out.removed.add(path)
        self.load(new_rows)


def _drop[K](index: dict[K, dict[RecordKey, _Row]], ikey: K, key: RecordKey) -> None:
    bucket = index[ikey]
    del bucket[key]
    if not bucket:
        del index[ikey]


# ---------------------------------------------------------------------------
# The store set
# ---------------------------------------------------------------------------


class StoreSet:
    """Every store of one controller; implements :class:`StoreView` directly."""

    def __init__(
        self, *, remove_coalesce_window: float = 1.0, forecast_burst_gap: float = 60.0
    ) -> None:
        if not 0 < remove_coalesce_window <= 5:
            raise ValueError("remove_coalesce_window must be in (0, 5] seconds")
        if forecast_burst_gap <= 0:
            raise ValueError("forecast_burst_gap must be positive")
        self._window = float(remove_coalesce_window)
        self._interface = _RecordStore()
        self._overrides = _RecordStore()
        self._plant_conf: dict[str, str] = {}
        self._config: dict[str, Any] = {}
        self._alive: dict[str, Any] = {}
        self._forecast = ForecastCache(forecast_burst_gap)
        self._synced_at: datetime | None = None
        self._live_since: datetime | None = None
        self._last_commissioning_at: datetime | None = None
        self.counters = StoreCounters()

    # -- StoreView ------------------------------------------------------------

    @property
    def synced_at(self) -> datetime:
        if self._synced_at is None:
            raise RuntimeError("no snapshot has been applied yet")
        return self._synced_at

    @property
    def has_snapshot(self) -> bool:
        return self._synced_at is not None

    @property
    def live_since(self) -> datetime | None:
        return self._live_since

    @live_since.setter
    def live_since(self, value: datetime | None) -> None:
        self._live_since = value

    @property
    def alive(self) -> Mapping[str, Any]:
        return MappingProxyType(self._alive)

    @property
    def config(self) -> Mapping[str, Any]:
        return MappingProxyType(self._config)

    @property
    def plant_conf(self) -> Mapping[str, str]:
        return MappingProxyType(self._plant_conf)

    @property
    def forecast(self) -> ForecastSnapshot | None:
        return self._forecast.snapshot()

    @property
    def last_commissioning_at(self) -> datetime | None:
        return self._last_commissioning_at

    def value(self, gruppo: str, unita: str, subuni: str, key: str) -> str | None:
        """Interface ``Valore`` of one record, or ``None`` if the row is absent."""
        row = self._interface.rows.get((gruppo, unita, subuni, key))
        return None if row is None else row.value

    def row(self, gruppo: str, unita: str, subuni: str, key: str) -> RowView | None:
        """Interface row, or ``None``."""
        rkey = (gruppo, unita, subuni, key)
        row = self._interface.rows.get(rkey)
        return None if row is None else _view(rkey, row)

    def rows(self, gruppo: str, unita: str | None = None) -> Iterator[RowView]:
        """Interface rows of ``gruppo`` (and ``unita``, if given), in store order."""
        for key, row in self._interface.by_gruppo(gruppo, unita):
            yield _view(key, row)

    def rows_with_key(self, key: str) -> Iterator[RowView]:
        """Interface rows whose ``Key`` is exactly ``key``, in store order."""
        for rkey, row in self._interface.by_key(key):
            yield _view(rkey, row)

    def override_rows(self) -> Iterator[RowView]:
        """Overrides-store rows, in store order."""
        for key, row in list(self._overrides.rows.items()):
            yield _view(key, row)

    # -- extra read access (client, diagnostics) -------------------------------

    def interface_rows(self) -> Iterator[RowView]:
        for key, row in list(self._interface.rows.items()):
            yield _view(key, row)

    @property
    def interface_size(self) -> int:
        return len(self._interface.rows)

    @property
    def pending_removes(self) -> int:
        return len(self._interface.pending) + len(self._overrides.pending)

    def next_expiry(self) -> float | None:
        """Earliest pending-remove deadline (monotonic), or ``None``."""
        deadlines = [*self._interface.pending.values(), *self._overrides.pending.values()]
        return min(deadlines) if deadlines else None

    # -- mutations -----------------------------------------------------------

    def set_alive(self, body: object) -> bool:
        """Replace the ``/alive/`` store (a non-mapping body stores ``{}``)."""
        new = dict(body) if isinstance(body, Mapping) else {}
        if new == self._alive:
            return False
        self._alive = new
        return True

    def set_live_since(self, value: datetime | None) -> ChangeSet:
        """Record when the WS went live (``None``: not live).  Path-less change."""
        changed = value != self._live_since
        self._live_since = value
        return ChangeSet(touched=changed)

    def expire(self, now_mono: float) -> ChangeSet:
        """Apply every pending remove whose deadline is ``<= now_mono``."""
        out = ChangeSet()
        for store in (self._interface, self._overrides):
            due = [key for key, deadline in store.pending.items() if deadline <= now_mono]
            for key in due:
                store.drop(key)
                path = make_path(*key)
                out.changed.add(path)
                out.removed.add(path)
                self.counters.removes_applied += 1
        return out

    def apply_frame(self, frame: object, received_at: datetime, received_mono: float) -> ChangeSet:
        """Apply one WebSocket frame (redacted here again, idempotently)."""
        out = self.expire(received_mono)
        frame, _count = redact_ws_frame(frame)
        counters = self.counters
        if not isinstance(frame, Mapping) or "domotica" in frame:
            counters.frames_ignored += 1
            return out
        domain = frame.get("domain")
        if domain == "bus":
            self._apply_bus(frame, received_at, out)
        elif domain == "termo":
            self._apply_termo(frame, received_at, received_mono, out)
        else:
            counters.frames_ignored += 1
        return out

    def _apply_bus(self, frame: Mapping[str, Any], received_at: datetime, out: ChangeSet) -> None:
        counters = self.counters
        key = norm(frame.get("key"))
        kind = frame.get("type")
        if key.startswith("RIS_"):
            self._last_commissioning_at = received_at
            out.touched = True
            counters.frames_ignored += 1
            return
        if not key or kind not in ("update", "remove"):
            counters.frames_ignored += 1
            return
        if kind == "update":
            value = norm(frame.get("value"))
            old = self._plant_conf.get(key)
            if old is not None and values_equal(old, value):
                # plant conf is exposed raw in the model: keep the old spelling so
                # a numerically equal refresh can never change the state
                counters.noop_updates += 1
            else:
                self._plant_conf[key] = value
                counters.changed_updates += 1
                out.conf_changed.add(key)
            return
        if self._plant_conf.pop(key, None) is None:
            counters.frames_ignored += 1
            return
        counters.removes_applied += 1
        out.conf_changed.add(key)

    def _apply_termo(
        self,
        frame: Mapping[str, Any],
        received_at: datetime,
        received_mono: float,
        out: ChangeSet,
    ) -> None:
        counters = self.counters
        parts = split_path(frame.get("path"))
        kind = frame.get("type")
        if parts is None or kind not in ("update", "remove"):
            counters.frames_ignored += 1
            return
        gruppo = frame.get("gruppo")
        if gruppo == METEO_DATA_GRUPPO or parts[0] == METEO_DATA_GRUPPO:
            if kind == "update":
                ok = self._forecast.apply(
                    parts[3], norm(frame.get("value")), received_at, received_mono
                )
            else:
                ok = self._forecast.remove(parts[3], received_at, received_mono)
            if ok:
                counters.forecast_frames += 1
                out.forecast_changed = True
            else:
                counters.frames_ignored += 1
            return
        is_override = gruppo == OVERRIDE_GRUPPO or (gruppo is None and parts[0] == OVERRIDE_GRUPPO)
        store = self._overrides if is_override else self._interface
        key: RecordKey = (OVERRIDE_GRUPPO, parts[1], parts[2], parts[3]) if is_override else parts
        if kind == "remove":
            if key in store.rows and key not in store.pending:
                store.pending[key] = received_mono + self._window
            else:
                counters.frames_ignored += 1
            return
        value = norm(frame.get("value"))
        if store.pending.pop(key, None) is not None:
            counters.removes_coalesced += 1
        row = store.rows.get(key)
        if row is None:
            store.insert(
                key,
                _Row(
                    value=value,
                    impostazione=_opt_str(frame.get("issuedAt")),
                    scadenza=_opt_str(frame.get("expiresAt")),
                    changed_at=received_at,
                    frame_at=received_at,
                ),
            )
            counters.changed_updates += 1
            out.changed.add(make_path(*key))
            return
        row.frame_at = received_at
        impostazione = _opt_str(frame["issuedAt"]) if "issuedAt" in frame else row.impostazione
        scadenza = _opt_str(frame["expiresAt"]) if "expiresAt" in frame else row.scadenza
        if _same_row(key, row, value, impostazione, scadenza):
            # "24" -> "24.0": not a change, and the stored spelling is kept, so a
            # suppressed frame can never change the built state (raw fields such as
            # plant.vmc_type_code are exposed as stored)
            counters.noop_updates += 1
            return
        row.value = value
        row.impostazione = impostazione
        row.scadenza = scadenza
        row.changed_at = received_at
        counters.changed_updates += 1
        out.changed.add(make_path(*key))

    def replace(self, snapshot: Snapshot, at: datetime) -> ChangeSet:
        """Swap in a REST snapshot in one step and return the semantic diff.

        Pending removes are cleared (a missing row is removed at once), unchanged
        rows keep ``changed_at``/``frame_at``, the config diff ignores
        ``LOCAL_TIME`` and the forecast cache is kept (it is WS-only).
        """
        out = ChangeSet()
        self._interface.replace(snapshot.interface, at, out)
        self._overrides.replace(snapshot.overrides, at, out)
        old_conf = self._plant_conf
        out.conf_changed |= _conf_diff(old_conf, snapshot.plant_conf)
        self._plant_conf = {
            key: old_conf[key] if key in old_conf and values_equal(old_conf[key], value) else value
            for key, value in snapshot.plant_conf.items()
        }
        out.config_changed |= config_diff(self._config, snapshot.config)
        self._config = dict(snapshot.config)
        self._synced_at = at
        return out

    def replace_config(self, config: Mapping[str, Any]) -> ChangeSet:
        """Swap in a new ``/config/`` (the periodic config poll)."""
        out = ChangeSet(config_changed=config_diff(self._config, config))
        self._config = dict(config)
        return out

    # -- diagnostics ------------------------------------------------------------

    def dump_records(self, *, overrides: bool = False) -> list[dict[str, Any]]:
        """JSON-ready rows (``Gruppo, Unita, SubUni, Key, Valore, path`` [+ timestamps])."""
        store = self._overrides if overrides else self._interface
        out: list[dict[str, Any]] = []
        for key, row in store.rows.items():
            item: dict[str, Any] = {
                "Gruppo": key[0],
                "Unita": key[1],
                "SubUni": key[2],
                "Key": key[3],
                "Valore": row.value,
                "path": make_path(*key),
            }
            if row.impostazione is not None:
                item["Impostazione"] = row.impostazione
            if row.scadenza is not None:
                item["Scadenza"] = row.scadenza
            out.append(item)
        return out


def _conf_diff(old: Mapping[str, str], new: Mapping[str, str]) -> set[str]:
    out: set[str] = set()
    for key in set(old) | set(new):
        if (key in old) != (key in new) or not values_equal(old.get(key), new.get(key)):
            out.add(key)
    return out
