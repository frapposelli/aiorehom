"""Offline analysis of a capture directory: inventory summary and record-level diff.

Both commands print to stdout, which may end up in a chat transcript, so:

* every capture file is re-redacted on load (a capture may be older,
  hand-edited or not produced by the probe);
* ``UTEN`` user names are always shown as ``<user>``;
* zone/VMC names (inventory) and personal values (diff: names, locality,
  SSID, MAC, serials, IP addresses) are masked unless explicitly requested;
* secrets are reported by presence only (``present``/``empty``/``null``),
  never with their length, and any redaction marker is printed as
  ``<redacted>``.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from ._files import read_json, read_jsonl
from .logic.presence import WIDTH_VMCS, WIDTH_ZONES, unit_index
from .pseudonymise import is_personal_field
from .redact import (
    is_redacted,
    is_secret_record,
    marker_presence,
    redact_capture_file,
    redact_dict,
    redact_records,
)
from .store import (
    RecordStore,
    StoreDiff,
    diff,
    display_key,
    display_path,
    make_path,
    norm,
    record_key,
)

__all__ = [
    "DIFF_STORES",
    "build_inventory",
    "catalog_coverage",
    "diff_captures",
    "format_diff",
    "format_inventory",
    "load_capture",
    "load_diff_stores",
    "load_volatile_patterns",
    "mask_diff",
    "secret_presence",
    "stores_not_compared",
]

REHOM_KEYS: Final = (
    "MODO",
    "SET_POINT",
    "STAGIONE",
    "FORZATURA_STAGIONE",
    "TERMO_READONLY",
    "SERVER_ON",
    "ALG_ATTIVO",
    "ABILITA_DOMOTICA",
    "PRESENZA_AT9091",
    "VER_SOFT",
    "VER_SOFT_KNX",
    "ALLARME_BUS",
)
VECTOR_KEYS: Final = (
    "PRESENZA_SONDE",
    "STATO_SONDE",
    "PRESENZA_DEUM",
    "STATO_DEUM",
    "PRESENZA_AT9091",
    "STATO_AT9091",
    "PRESENZA_ATT",
    "STATO_ATT",
)
ZONE_FIELDS: Final = ("NOME", "TEMP_AMBIENTE", "UMIDITA", "SETP_CORRENTE", "ATTIVA")
VMC_FIELDS: Final = ("NOME", "ST_MODE", "COM_VENTILA", "ST_STATO_DEUM")
PLANT_FLAGS: Final = ("CONFIGURA_ON", "CONF_ZONE_ON", "CONF_DEUM_ON", "stagione")
_PLACEHOLDER_RE: Final = re.compile(r"\{([^{}]*)\}")
HIDDEN: Final = "<hidden>"
PERSONAL: Final = "<personal>"
#: How any redaction marker is printed (presence only: never a length).
REDACTED: Final = "<redacted>"
#: Stores compared by ``diff`` and the file each one is loaded from.
DIFF_STORES: Final = {
    "interface": "interface.json",
    "overrides": "overrides.json",
    "plant_conf": "plant_conf.json",
    "config": "config.json",
}
_IPV4_RE: Final = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_MAC_RE: Final = re.compile(
    r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_capture(directory: Path) -> dict[str, Any]:
    """Load every ``*.json`` and ``*.jsonl`` file of a capture directory (not recursive)."""
    capture: dict[str, Any] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        try:
            if path.suffix == ".json":
                capture[path.name] = read_json(path)
            elif path.suffix == ".jsonl":
                capture[path.name] = read_jsonl(path)
        except (OSError, ValueError):
            capture[path.name] = {"_unreadable": True}
    return capture


def _rows(capture: Mapping[str, Any], name: str) -> list[Mapping[str, Any]]:
    data = capture.get(name)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]
    return []


def _vector(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",")]


def _flag(vector: list[str], index: int | None) -> bool | None:
    """``vector[index] == "1"``; ``None`` for a stray unit id or a short vector."""
    if index is None or index >= len(vector):
        return None
    return vector[index] == "1"


def secret_presence(value: object) -> str:
    """``present``/``empty``/``null`` (or a type name) for a secret; never its length."""
    presence = marker_presence(value)
    if presence is None:  # not a marker (cannot happen after re-redaction)
        return "empty" if value in ("", None) else "present"
    if presence.endswith(" chars"):
        return "present"
    return presence


def _hide_marker(value: Any) -> Any:
    return REDACTED if is_redacted(value) else value


def _hide_markers(obj: Any) -> Any:
    """Copy of ``obj`` with every redaction marker replaced by ``<redacted>``."""
    if isinstance(obj, Mapping):
        return _hide_markers_dict(obj)
    if isinstance(obj, list):
        return [_hide_markers(value) for value in obj]
    return _hide_marker(obj)


def _hide_markers_dict(obj: Mapping[Any, Any]) -> dict[Any, Any]:
    return {key: _hide_markers(value) for key, value in obj.items()}


# ---------------------------------------------------------------------------
# Catalogue coverage
# ---------------------------------------------------------------------------


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    pos = 0
    for match in _PLACEHOLDER_RE.finditer(pattern):
        out.append(re.escape(pattern[pos : match.start()]))
        inner = match.group(1)
        spec = inner.split(":", 1)[1] if ":" in inner else ""
        if spec and "|" in spec:
            out.append("(?:" + "|".join(re.escape(alt) for alt in spec.split("|")) + ")")
        else:
            out.append(".*")
        pos = match.end()
    out.append(re.escape(pattern[pos:]))
    return re.compile("".join(out))


def _match_records(
    entries: object, pairs: list[tuple[str, str]]
) -> tuple[list[str], list[str], list[str]]:
    """``(seen, not_seen, uncatalogued)`` for catalogue record entries against pairs."""
    matched: set[tuple[str, str]] = set()
    seen: list[str] = []
    not_seen: list[str] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        g_pat = str(entry.get("gruppo", ""))
        k_pat = str(entry.get("key", ""))
        g_re = _pattern_regex(g_pat)
        k_re = _pattern_regex(k_pat)
        hits = [p for p in pairs if g_re.fullmatch(p[0]) and k_re.fullmatch(p[1])]
        label = f"{g_pat}.{k_pat}"
        if hits:
            seen.append(label)
            matched.update(hits)
        else:
            not_seen.append(label)
    uncatalogued = [f"{g}.{k}" for g, k in pairs if (g, k) not in matched]
    return seen, not_seen, uncatalogued


def catalog_coverage(
    catalog: Mapping[str, Any],
    interface_pairs: Iterable[tuple[str, str]],
    plant_keys: Iterable[str],
    config_keys: Iterable[str],
    override_pairs: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """Which catalogue entries were seen / not seen, and what was seen but is not catalogued.

    Interface pairs are matched against ``interface_records`` only, override
    pairs against ``override_records`` only.
    """
    seen, not_seen, uncatalogued = _match_records(
        catalog.get("interface_records", []), sorted(set(interface_pairs))
    )
    o_seen, o_not_seen, o_uncatalogued = _match_records(
        catalog.get("override_records", []), sorted(set(override_pairs))
    )

    plant = sorted(set(plant_keys))
    plant_seen: list[str] = []
    plant_not_seen: list[str] = []
    plant_matched: set[str] = set()
    for entry in catalog.get("plant_conf_keys", []):
        if not isinstance(entry, Mapping):
            continue
        pat = str(entry.get("key", ""))
        key_re = _pattern_regex(pat)
        key_hits = [k for k in plant if key_re.fullmatch(k)]
        (plant_seen if key_hits else plant_not_seen).append(pat)
        plant_matched.update(key_hits)

    config = sorted(set(config_keys))
    cfg_catalogued = [
        str(e.get("key")) for e in catalog.get("api_config_fields", []) if isinstance(e, Mapping)
    ]
    return {
        "interface_seen": seen,
        "interface_not_seen": not_seen,
        "interface_uncatalogued": uncatalogued,
        "overrides_seen": o_seen,
        "overrides_not_seen": o_not_seen,
        "overrides_uncatalogued": o_uncatalogued,
        "plant_conf_seen": plant_seen,
        "plant_conf_not_seen": plant_not_seen,
        "plant_conf_uncatalogued": [k for k in plant if k not in plant_matched],
        "config_seen": [k for k in cfg_catalogued if k in config],
        "config_not_seen": [k for k in cfg_catalogued if k not in config],
        "config_uncatalogued": [k for k in config if k not in cfg_catalogued],
    }


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def _find_markers(obj: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if is_redacted(value):
                found.append((path, value))
            else:
                found.extend(_find_markers(value, path))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_find_markers(item, f"{prefix}[{index}]"))
    return found


def _units(store: RecordStore, gruppo: str, exclude: Iterable[str]) -> set[str]:
    skip = set(exclude)
    return {
        row["Unita"]
        for row in store.to_rows()
        if row["Gruppo"] == gruppo and row["Unita"] not in skip
    }


def _store_info(store: RecordStore) -> dict[str, Any]:
    return {"rows": len(store), "raw_rows": store.raw_rows, "duplicates": store.duplicates}


def build_inventory(
    capture: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
    *,
    hide_names: bool = False,
) -> dict[str, Any]:
    """Structured summary of a capture (re-redacted here; names hidden on request)."""
    capture = {name: redact_capture_file(name, content)[0] for name, content in capture.items()}
    interface_rows = _rows(capture, "interface.json")
    override_rows = _rows(capture, "overrides.json")
    store = RecordStore(interface_rows)
    overrides = RecordStore(override_rows)

    def rehom(key: str) -> str | None:
        return store.value("REHOM", "", "", key)

    vectors = {key: rehom(key) for key in VECTOR_KEYS}
    presence = _vector(vectors["PRESENZA_SONDE"])
    online = _vector(vectors["STATO_SONDE"])
    zone_ids = {f"{i + 1:03d}" for i, flag in enumerate(presence) if flag == "1"}
    zone_ids |= _units(store, "ZONA", ("", "000"))
    zones = []
    for zone in sorted(zone_ids):
        index = unit_index(zone, width=WIDTH_ZONES)  # stray ids ("1", "025"...) map to nothing
        entry: dict[str, Any] = {
            "id": zone,
            "present": _flag(presence, index),
            "online": _flag(online, index),
        }
        for key in ZONE_FIELDS:
            entry[key] = store.value("ZONA", zone, "", key)
        if hide_names and entry["NOME"] is not None:
            entry["NOME"] = HIDDEN
        zones.append(entry)

    deum_presence = _vector(vectors["PRESENZA_DEUM"])
    deum_online = _vector(vectors["STATO_DEUM"])
    vmc_ids = {f"{i + 1:03d}" for i, flag in enumerate(deum_presence) if flag == "1"}
    vmc_ids |= _units(store, "DEUM", ("",))
    vmcs = []
    for unit in sorted(vmc_ids):
        index = unit_index(unit, width=WIDTH_VMCS)  # DEUM "1"/"2"/"000" are not VMCs 001/002
        vmc: dict[str, Any] = {
            "id": unit,
            "present": _flag(deum_presence, index),
            "online": _flag(deum_online, index),
        }
        for key in VMC_FIELDS:
            vmc[key] = store.value("DEUM", unit, "", key)
        if hide_names and vmc["NOME"] is not None:
            vmc["NOME"] = HIDDEN
        vmcs.append(vmc)

    stray_units = {
        "ZONA": sorted(
            unit
            for unit in _units(store, "ZONA", ("",))
            if unit_index(unit, width=WIDTH_ZONES) is None
        ),
        "DEUM": sorted(
            unit
            for unit in _units(store, "DEUM", ("",))
            if unit_index(unit, width=WIDTH_VMCS) is None
        ),
    }

    lock_rows = [
        {"store": name, "path": display_path(record_key(row)), "Valore": row["Valore"]}
        for name, source in (("interface", store), ("overrides", overrides))
        for row in source.to_rows()
        if row["Key"] == "WEBSERVER"
    ]

    plant_conf = capture.get("plant_conf.json")
    plant_flags = {
        key: (
            plant_conf.get(key) if isinstance(plant_conf, Mapping) and key in plant_conf else None
        )
        for key in PLANT_FLAGS
    }

    me = capture.get("me.json")
    me_username = me.get("username") if isinstance(me, Mapping) else None
    secrets: list[dict[str, Any]] = []
    user_index = 0
    for row in store.to_rows():
        if not is_secret_record(row["Gruppo"], row["Key"]):
            continue
        name = f"{row['Gruppo']}.{row['Key']}"
        extra: dict[str, Any] = {}
        if row["Gruppo"] == "UTEN" and row["Key"] != "admin":
            user_index += 1
            name = f"UTEN.<user #{user_index}>"
            if isinstance(me_username, str):
                extra["matches_me_username"] = row["Key"] == me_username
        secrets.append({"name": name, "presence": secret_presence(row["Valore"]), **extra})
    for file_name in ("config.json", "plant_conf.json", "me.json", "alive.json"):
        for path, marker in _find_markers(capture.get(file_name)):
            secrets.append({"name": f"{file_name}:{path}", "presence": secret_presence(marker)})

    group_rows = Counter(row["Gruppo"] for row in store.to_rows())
    group_keys = {
        g: len({row["Key"] for row in store.to_rows() if row["Gruppo"] == g}) for g in group_rows
    }

    history: dict[str, Any] = {}
    for name in sorted(capture):
        if name.startswith("history_") and isinstance(capture[name], list):
            points = [p for p in capture[name] if isinstance(p, Mapping)]
            history[name] = {
                "points": len(points),
                "first": points[0].get("Tempo") if points else None,
                "last": points[-1].get("Tempo") if points else None,
                "fields": sorted({str(k) for p in points for k in p}),
            }

    config = capture.get("config.json")
    alive = capture.get("alive.json")
    domotica = {
        name: (len(value) if isinstance(value, list) else type(value).__name__)
        for name, value in sorted(capture.items())
        if name.startswith("domotica_")
    }
    inventory: dict[str, Any] = {
        "files": sorted(capture),
        "alive": alive if isinstance(alive, Mapping) else None,
        "config": {
            k: config.get(k)
            for k in ("LOCAL_TIME", "TIMEZONE", "VERSION")
            if isinstance(config, Mapping)
        },
        "names_hidden": hide_names,
        "zones": zones,
        "zones_flagged_present": sum(1 for z in zones if z["present"]),
        "vmcs": vmcs,
        "vmcs_flagged_present": sum(1 for v in vmcs if v["present"]),
        "stray_units": stray_units,
        "vectors": vectors,
        "rehom": {key: rehom(key) for key in REHOM_KEYS},
        "webserver_lock_rows": lock_rows,
        "plant_conf_flags": plant_flags,
        "secrets": secrets,
        "interface_rows": len(store),
        "override_rows": len(overrides),
        "records": {"interface": _store_info(store), "overrides": _store_info(overrides)},
        "groups": {g: {"rows": group_rows[g], "keys": group_keys[g]} for g in sorted(group_rows)},
        "history": history,
        "domotica": domotica,
    }
    if catalog is not None:
        # UTEN keys are user names: report them as "<user>" (admin kept).
        def pairs(source: RecordStore) -> list[tuple[str, str]]:
            return [
                (key[0], key[3])
                for key in (display_key(record_key(row)) for row in source.to_rows())
            ]

        plant_keys = list(plant_conf) if isinstance(plant_conf, Mapping) else []
        config_keys = list(config) if isinstance(config, Mapping) else []
        inventory["catalog"] = catalog_coverage(
            catalog,
            pairs(store),
            [str(k) for k in plant_keys],
            [str(k) for k in config_keys],
            override_pairs=pairs(overrides),
        )
    return _hide_markers_dict(inventory)


def _fmt(value: object) -> str:
    return "-" if value is None else str(value)


def format_inventory(inventory: Mapping[str, Any], *, hide_names: bool = False) -> str:
    """Render :func:`build_inventory` output as a human summary."""
    lines: list[str] = []
    add = lines.append
    add(f"Files: {', '.join(inventory['files']) or '(none)'}")
    if inventory.get("alive") is not None:
        add(f"alive.json: {json.dumps(inventory['alive'], ensure_ascii=False)}")
    if inventory.get("config"):
        add("config: " + ", ".join(f"{k}={_fmt(v)}" for k, v in inventory["config"].items()))
    add("")
    zones_present = sum(1 for z in inventory["zones"] if z["present"])
    add(
        f"Zone units ({len(inventory['zones'])} with rows or flagged; "
        f"{zones_present} flagged present in PRESENZA_SONDE):"
    )
    for zone in inventory["zones"]:
        name = HIDDEN if hide_names and zone["NOME"] is not None else _fmt(zone["NOME"])
        add(
            f"  {zone['id']}  present={_fmt(zone['present'])} online={_fmt(zone['online'])} "
            f"name={name} temp={_fmt(zone['TEMP_AMBIENTE'])} hum={_fmt(zone['UMIDITA'])} "
            f"SETP_CORRENTE={_fmt(zone['SETP_CORRENTE'])} ATTIVA={_fmt(zone['ATTIVA'])}"
        )
    vmcs_present = sum(1 for v in inventory["vmcs"] if v["present"])
    add(
        f"VMC units ({len(inventory['vmcs'])} with rows or flagged; "
        f"{vmcs_present} flagged present in PRESENZA_DEUM):"
    )
    for vmc in inventory["vmcs"]:
        name = HIDDEN if hide_names and vmc["NOME"] is not None else _fmt(vmc["NOME"])
        add(
            f"  {vmc['id']}  present={_fmt(vmc['present'])} online={_fmt(vmc['online'])} "
            f"name={name} ST_MODE={_fmt(vmc['ST_MODE'])} COM_VENTILA={_fmt(vmc['COM_VENTILA'])} "
            f"ST_STATO_DEUM={_fmt(vmc['ST_STATO_DEUM'])}"
        )
    stray = inventory.get("stray_units") or {}
    add(
        "Stray unit rows (not a unit id of the PRESENZA vectors; never a unit): "
        + "; ".join(f"{group}: {', '.join(ids) or '-'}" for group, ids in stray.items())
    )
    add("PRESENZA/STATO vectors:")
    for key, value in inventory["vectors"].items():
        add(f"  {key} = {_fmt(value)}")
    add("REHOM:")
    for key, value in inventory["rehom"].items():
        add(f"  {key} = {_fmt(value)}")
    add("WEBSERVER lock rows (Key == WEBSERVER, both stores):")
    if not inventory["webserver_lock_rows"]:
        add("  (none: UI default '1' applies)")
    for row in inventory["webserver_lock_rows"]:
        add(f"  [{row['store']}] {row['path']} = {row['Valore']}")
    add("Plant-conf installer flags:")
    for key, value in inventory["plant_conf_flags"].items():
        add(f"  {key} = {'(absent)' if value is None else value}")
    add("Secret rows (redacted; presence only):")
    if not inventory["secrets"]:
        add("  (none)")
    for secret in inventory["secrets"]:
        extra = ""
        if "matches_me_username" in secret:
            extra = f" (same as /me/ username: {secret['matches_me_username']})"
        add(f"  {secret['name']} = {secret['presence']}{extra}")
    add(f"Records: interface={inventory['interface_rows']} overrides={inventory['override_rows']}")
    records = inventory.get("records")
    if records:
        add(
            f"  raw rows: interface={records['interface']['raw_rows']} "
            f"overrides={records['overrides']['raw_rows']}"
        )
        dups = [
            f"[{store}] {path} x{count}"
            for store in ("interface", "overrides")
            for path, count in records[store]["duplicates"].items()
        ]
        if dups:
            add(f"  duplicate identities (last row wins): {', '.join(dups)}")
    for group, counts in inventory["groups"].items():
        add(f"  {group}: {counts['rows']} rows, {counts['keys']} keys")
    if inventory["history"]:
        add("History:")
        for name, info in inventory["history"].items():
            add(
                f"  {name}: {info['points']} points {_fmt(info['first'])} .. {_fmt(info['last'])}"
                f" fields={','.join(info['fields'])}"
            )
    if inventory["domotica"]:
        add("Domotica: " + ", ".join(f"{k}={v}" for k, v in inventory["domotica"].items()))
    catalog = inventory.get("catalog")
    if catalog:
        add("")
        add("Record catalogue coverage:")
        for label, key in (
            ("interface records seen", "interface_seen"),
            ("interface records NOT seen", "interface_not_seen"),
            ("interface keys not in catalogue", "interface_uncatalogued"),
            ("override records seen", "overrides_seen"),
            ("override records NOT seen", "overrides_not_seen"),
            ("override keys not in catalogue", "overrides_uncatalogued"),
            ("plant-conf keys seen", "plant_conf_seen"),
            ("plant-conf keys NOT seen", "plant_conf_not_seen"),
            ("plant-conf keys not in catalogue", "plant_conf_uncatalogued"),
            ("config fields seen", "config_seen"),
            ("config fields NOT seen", "config_not_seen"),
            ("config fields not in catalogue", "config_uncatalogued"),
        ):
            items = catalog.get(key, [])
            add(f"  {label} ({len(items)}): {', '.join(items) if items else '-'}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def _dict_store(data: Any, gruppo: str) -> RecordStore:
    rows: list[dict[str, Any]] = []
    if isinstance(data, Mapping):
        for key, value in data.items():
            text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            rows.append(
                {"Gruppo": gruppo, "Unita": "", "SubUni": "", "Key": norm(key), "Valore": text}
            )
    return RecordStore(rows)


def load_diff_stores(directory: Path) -> dict[str, RecordStore]:
    """Re-redacted stores compared by ``rehom-probe diff``.

    The stores are interface, overrides, plant_conf and config.

    A store whose file is missing is absent from the result.
    """
    stores: dict[str, RecordStore] = {}
    for name in ("interface", "overrides"):
        path = directory / DIFF_STORES[name]
        if path.is_file():
            data, _count = redact_records(read_json(path))
            stores[name] = (
                RecordStore(r for r in data if isinstance(r, Mapping))
                if isinstance(data, list)
                else RecordStore()
            )
    for name, gruppo in (("plant_conf", "PLANT_CONF"), ("config", "CONFIG_API")):
        path = directory / DIFF_STORES[name]
        if path.is_file():
            data, _count = redact_dict(read_json(path))
            stores[name] = _dict_store(data, gruppo)
    return stores


def load_volatile_patterns(path: Path) -> list[str]:
    """Read ignore patterns: a JSON list, or one fnmatch pattern per line (``#`` comments).

    A ``.json`` file must hold a JSON list.  Any other file is read as a JSON
    list if it parses as one, else line by line (so a first pattern such as
    ``[ZP]*.TEMP_AMBIENTE`` is a pattern, not broken JSON).
    """
    text = path.read_text(encoding="utf-8")
    data: Any = None
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError(f"{path.name}: expected a JSON list of patterns")
    elif text.lstrip().startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if isinstance(data, list):
        return [str(p) for p in data if isinstance(p, str) and p]
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _ignored(store: str, path: str, patterns: Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(f"{store}:{path}", p) for p in patterns
    )


def stores_not_compared(dir_a: Path, dir_b: Path) -> dict[str, str]:
    """Stores whose file exists in only one capture: ``{store: "missing in A"|"missing in B"}``."""
    out: dict[str, str] = {}
    for name, file_name in DIFF_STORES.items():
        in_a = (dir_a / file_name).is_file()
        in_b = (dir_b / file_name).is_file()
        if in_a != in_b:
            out[name] = f"{file_name} missing in {'B' if in_a else 'A'}"
    return out


def diff_captures(dir_a: Path, dir_b: Path, ignore: Iterable[str] = ()) -> dict[str, StoreDiff]:
    """Record-level diff of two capture directories, store by store.

    Only stores captured in **both** directories are compared (see
    :func:`stores_not_compared` for the others): comparing against a missing
    file would look like a mass addition or removal.  Values are re-redacted;
    use :func:`mask_diff` before printing.
    """
    patterns = list(ignore)
    stores_a = load_diff_stores(dir_a)
    stores_b = load_diff_stores(dir_b)
    result: dict[str, StoreDiff] = {}
    for name in DIFF_STORES:
        if name not in stores_a or name not in stores_b:
            continue
        delta = diff(stores_a[name], stores_b[name])
        filtered = StoreDiff(
            added=[r for r in delta.added if not _ignored(name, r["path"], patterns)],
            removed=[r for r in delta.removed if not _ignored(name, r["path"], patterns)],
            changed=[c for c in delta.changed if not _ignored(name, c["path"], patterns)],
        )
        result[name] = filtered
    return result


def _mask_text(value: Any) -> Any:
    """Replace IPv4 addresses (except 0.x, 127.x, 255.x) and MAC addresses in a string."""
    if not isinstance(value, str):
        return value

    def ip(match: re.Match[str]) -> str:
        first = int(match.group(1))
        return match.group(0) if first in (0, 127, 255) else "<ip>"

    return _MAC_RE.sub("<mac>", _IPV4_RE.sub(ip, value))


def _mask_row(row: Mapping[str, Any], personal: bool) -> dict[str, Any]:
    out = dict(row)
    key = record_key(row)
    shown = display_key(key)
    if shown != key:
        out["Key"] = shown[3]
        out["path"] = make_path(*shown)
    out.pop("path_received", None)
    if personal:
        value = out.get("Valore")
        if is_personal_field(key[0], key[3]):
            out["Valore"] = PERSONAL if value not in (None, "") else value
        else:
            out["Valore"] = _mask_text(value)
    return _hide_markers_dict(out)


def _mask_change(change: Mapping[str, Any], personal: bool) -> dict[str, Any]:
    out = dict(change)
    parts = [str(part) for part in change["key"]]
    key = (parts[0], parts[1], parts[2], parts[3])
    shown = display_key(key)
    out["key"] = list(shown)
    out["path"] = make_path(*shown)
    old, new = change.get("old"), change.get("new")
    masked_old, masked_new = old, new
    if personal:
        if is_personal_field(key[0], key[3]):
            masked_old, masked_new = PERSONAL, PERSONAL
        else:
            masked_old, masked_new = _mask_text(old), _mask_text(new)
    masked_old, masked_new = _hide_marker(masked_old), _hide_marker(masked_new)
    if (masked_old, masked_new) != (old, new):
        out["old"], out["new"] = masked_old, masked_new
        out["masked"] = "changed" if old != new else "unchanged"
    if "fields" in out:
        out["fields"] = _hide_markers_dict(out["fields"])
    return out


def mask_diff(result: Mapping[str, StoreDiff], *, personal: bool = True) -> dict[str, StoreDiff]:
    """Copy of a diff that is safe to print.

    ``UTEN`` user names always become ``<user>`` and every redaction marker
    ``<redacted>`` (a marker's length is never printed).  With ``personal=True``,
    values of personal fields (names, locality, SSID, MAC, serials: see
    :func:`~aiorehom.pseudonymise.is_personal_field`) become ``<personal>`` and
    IPv4/MAC addresses inside other values become ``<ip>``/``<mac>``; a masked
    change carries ``"masked": "changed"|"unchanged"``.
    """
    return {
        name: StoreDiff(
            added=[_mask_row(r, personal) for r in delta.added],
            removed=[_mask_row(r, personal) for r in delta.removed],
            changed=[_mask_change(c, personal) for c in delta.changed],
        )
        for name, delta in result.items()
    }


def format_diff(
    result: Mapping[str, StoreDiff], not_compared: Mapping[str, str] | None = None
) -> str:
    lines: list[str] = []
    total = 0
    for name, reason in (not_compared or {}).items():
        lines.append(f"! {name}: not compared ({reason})")
    for name, delta in result.items():
        for row in delta.added:
            lines.append(f"+ {name} {row['path']} = {_hide_marker(row.get('Valore'))!r}")
        for row in delta.removed:
            lines.append(f"- {name} {row['path']} (was {_hide_marker(row.get('Valore'))!r})")
        for change in delta.changed:
            raw_old, raw_new = change["old"], change["new"]
            old, new = _hide_marker(raw_old), _hide_marker(raw_new)
            if "masked" in change and old == new:
                text = f"~ {name} {change['path']}: {old!r} ({change['masked']})"
            elif old == new and raw_old != raw_new:  # only the hidden marker lengths differ
                text = f"~ {name} {change['path']}: {old!r} (changed)"
            else:
                text = f"~ {name} {change['path']}: {old!r} -> {new!r}"
            if "fields" in change:
                text += " " + ", ".join(
                    f"{field}: {_hide_marker(f_old)!r} -> {_hide_marker(f_new)!r}"
                    for field, (f_old, f_new) in change["fields"].items()
                )
            lines.append(text)
        total += len(delta.added) + len(delta.removed) + len(delta.changed)
    summary = ", ".join(
        f"{name}: +{len(d.added)} -{len(d.removed)} ~{len(d.changed)}" for name, d in result.items()
    )
    if not_compared:
        summary += (", " if summary else "") + "not compared: " + ", ".join(not_compared)
    lines.append(f"{total} difference(s)" + (f" ({summary})" if summary else ""))
    return "\n".join(lines) + "\n"
