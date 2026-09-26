"""Secret redaction for records, dict payloads, WebSocket frames and HAR files.

Redaction replaces a secret value with a marker that keeps only its presence,
type and length: ``"<redacted len=N>"`` for a string of ``N`` characters
(``len=0`` for an empty string), ``"<redacted null>"`` for ``null``,
``"<redacted int len=N>"``/``"<redacted float len=N>"`` for numbers,
``"<redacted bool>"`` for booleans and ``"<redacted object>"``/``"<redacted list>"``
(no length) for containers.  Every public function returns
``(redacted_copy, count)`` (or a new dict for HAR files) and never mutates its
input.  Redaction is idempotent: an existing marker is kept as-is and not
counted again.

Pseudonymisation of personal data (names, serials, addresses) is a separate
step used only for test fixtures; see :mod:`aiorehom.pseudonymise`.
"""

from __future__ import annotations

import base64
import binascii
import copy
import fnmatch
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

__all__ = [
    "EXEMPT_KEYS",
    "SECRET_KEY_RE",
    "SECRET_PAIRS",
    "is_redacted",
    "is_secret_key",
    "is_secret_record",
    "marker_presence",
    "pseudonymise_fixture",
    "redact_capture_file",
    "redact_dict",
    "redact_har",
    "redact_records",
    "redact_ws_frame",
    "redacted_marker",
]

#: Catch-all for key names that look like secrets.
SECRET_KEY_RE: Final = re.compile(r"(passw|pwd|token|secret|api_?key|meteo_key)", re.IGNORECASE)
#: Exact key names that match the catch-all but are not secrets.
EXEMPT_KEYS: Final = frozenset({"ALLARM_METEO_KEY"})
#: Every record of these groups is treated as secret.
SECRET_GROUPS: Final = frozenset({"UTEN"})
#: Explicit (Gruppo, Key) secrets.
SECRET_PAIRS: Final = frozenset(
    {
        ("WIFI", "PASSWORD"),
        ("WEBSERVER", "PwdWiFi"),
        ("CONFIG", "METEO_KEY"),
        ("CONFIG", "REMOTE_TOKEN"),
    }
)
#: Dict keys that are always redacted, wherever they appear.
ALWAYS_SECRET_DICT_KEYS: Final = frozenset({"token", "password"})

_MARKER_RE: Final = re.compile(
    r"<redacted (?:len=\d+|(?:int|float) len=\d+|null|bool|object|list)>"
)
#: Record fields that identify a record or carry metadata; never secret themselves.
_RECORD_META_FIELDS: Final = frozenset(
    {"Gruppo", "Unita", "SubUni", "Key", "path", "Stato", "Flusso", "Impostazione", "Scadenza"}
)
#: WS termo fields that are addressing/metadata, never secret.
_TERMO_META_FIELDS: Final = frozenset({"domain", "type", "path", "gruppo", "issuedAt", "expiresAt"})
_BUS_META_FIELDS: Final = frozenset({"domain", "type", "key"})
#: Key/value pair shapes found in dict payloads (plant-conf writes, bus frames).
_KV_SHAPES: Final = (("key", "value"), ("Key", "Valore"), ("Key", "value"), ("name", "value"))


# ---------------------------------------------------------------------------
# Predicates and marker
# ---------------------------------------------------------------------------


def redacted_marker(value: object) -> str:
    """Return the replacement string for ``value`` (presence, type and length only)."""
    if isinstance(value, str):
        return f"<redacted len={len(value)}>"
    if value is None:
        return "<redacted null>"
    if isinstance(value, bool):
        return "<redacted bool>"
    if isinstance(value, int):
        return f"<redacted int len={len(str(value))}>"
    if isinstance(value, float):
        return f"<redacted float len={len(str(value))}>"
    if isinstance(value, (list, tuple)):
        return "<redacted list>"
    return "<redacted object>"


def marker_presence(marker: object) -> str | None:
    """Describe a redaction marker: ``"null"``, ``"empty"``, ``"N chars"``, ``"int"``...

    Returns ``None`` if ``marker`` is not a redaction marker.
    """
    if not isinstance(marker, str) or not is_redacted(marker):
        return None
    body = marker[len("<redacted ") : -1]
    if body.startswith("len="):
        length = int(body[len("len=") :])
        return "empty" if length == 0 else f"{length} chars"
    return body.split(" ", 1)[0]


def is_redacted(value: object) -> bool:
    """True if ``value`` already is a redaction marker."""
    return isinstance(value, str) and _MARKER_RE.fullmatch(value) is not None


def _norm(value: object) -> str:
    return "" if value is None else str(value)


def is_secret_key(key: object) -> bool:
    """Catch-all test on a key name (record Key, dict key, bus key)."""
    name = _norm(key)
    if name in EXEMPT_KEYS:
        return False
    return SECRET_KEY_RE.search(name) is not None


def is_secret_record(gruppo: object, key: object) -> bool:
    """True if the record ``(gruppo, key)`` carries a secret ``Valore``."""
    g = _norm(gruppo)
    k = _norm(key)
    if g in SECRET_GROUPS:
        return True
    if (g, k) in SECRET_PAIRS:
        return True
    return is_secret_key(k)


def _is_secret_dict_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    return key.lower() in ALWAYS_SECRET_DICT_KEYS or is_secret_key(key)


class _Counter:
    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


def _mark(value: object, counter: _Counter) -> object:
    if is_redacted(value):
        return value
    counter.n += 1
    return redacted_marker(value)


# ---------------------------------------------------------------------------
# Core recursive redaction
# ---------------------------------------------------------------------------


_RECORD_META_FIELDS_LOWER: Final = frozenset(f.lower() for f in _RECORD_META_FIELDS)


def _ci_get(row: Mapping[Any, Any], name: str) -> object:
    """Case-insensitive field lookup (``Gruppo``/``gruppo``/``GRUPPO``)."""
    for field, value in row.items():
        if isinstance(field, str) and field.lower() == name:
            return value
    return None


def _looks_like_record(obj: Mapping[Any, Any]) -> bool:
    return _ci_get(obj, "gruppo") is not None and _ci_get(obj, "key") is not None


def _path_is_secret(path: object) -> bool:
    parts = _norm(path).split(".")
    if parts[0] in SECRET_GROUPS or (parts[0], parts[-1]) in SECRET_PAIRS:
        return True
    return any(is_secret_key(part) for part in parts)


def _record_is_secret(row: Mapping[Any, Any]) -> bool:
    """Fail closed: a row whose group cannot be determined counts as secret."""
    gruppo = _ci_get(row, "gruppo")
    path = _ci_get(row, "path")
    if gruppo is None and path is None:
        return True
    if gruppo is not None and is_secret_record(gruppo, _ci_get(row, "key")):
        return True
    return path is not None and _path_is_secret(path)


def _redact_record(row: Mapping[Any, Any], counter: _Counter) -> dict[Any, Any]:
    secret = _record_is_secret(row)
    out: dict[Any, Any] = {}
    for field, value in row.items():
        if isinstance(field, str) and field.lower() in _RECORD_META_FIELDS_LOWER:
            out[field] = copy.deepcopy(value)
        elif secret or _is_secret_dict_key(field):
            out[field] = _mark(value, counter)
        else:
            out[field] = _redact_obj(value, counter)
    return out


def _kv_secret(obj: Mapping[Any, Any]) -> str | None:
    """If ``obj`` is a key/value pair whose key is secret, return the value field name."""
    for key_field, value_field in _KV_SHAPES:
        if key_field in obj and value_field in obj and is_secret_key(obj[key_field]):
            return value_field
    return None


def _redact_obj(obj: object, counter: _Counter) -> Any:
    if isinstance(obj, Mapping):
        if _looks_like_record(obj):
            return _redact_record(obj, counter)
        secret_value_field = _kv_secret(obj)
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if key == secret_value_field or _is_secret_dict_key(key):
                out[key] = _mark(value, counter)
            else:
                out[key] = _redact_obj(value, counter)
        return out
    if isinstance(obj, (list, tuple)):
        return [_redact_obj(item, counter) for item in obj]
    return copy.deepcopy(obj)


def redact_records(rows: object) -> tuple[Any, int]:
    """Redact an interface/overrides record list (``[{Gruppo, Unita, SubUni, Key, Valore}]``).

    Every mapping in the top-level list is treated as a record (even when
    ``Gruppo`` is missing).  Anything else falls back to :func:`redact_dict`.
    """
    counter = _Counter()
    if isinstance(rows, list):
        out = [
            _redact_record(item, counter)
            if isinstance(item, Mapping)
            else _redact_obj(item, counter)
            for item in rows
        ]
        return out, counter.n
    return _redact_obj(rows, counter), counter.n


def redact_dict(obj: object) -> tuple[Any, int]:
    """Recursively redact a dict payload (``/config/``, ``/plant/conf/``, ``/me/``, ...).

    Values whose key matches the catch-all (except ``ALLARM_METEO_KEY``) or is
    ``token``/``password`` are redacted; nested records are redacted with the
    record rules; ``{"key": <secret>, "value": ...}`` pairs are redacted.
    """
    counter = _Counter()
    return _redact_obj(obj, counter), counter.n


# ---------------------------------------------------------------------------
# WebSocket frames
# ---------------------------------------------------------------------------


def _termo_is_secret(frame: Mapping[Any, Any]) -> bool:
    gruppo = frame.get("gruppo")
    if _norm(gruppo) in SECRET_GROUPS:
        return True
    path = frame.get("path")
    if not isinstance(path, str):
        # No usable address: we cannot tell what the value is, so hide it.
        return True
    parts = path.split(".")
    if len(parts) == 4:
        g, _u, _s, k = parts
        if is_secret_record(g, k):
            return True
        return gruppo is not None and is_secret_record(gruppo, k)
    return any(part in SECRET_GROUPS or is_secret_key(part) for part in parts)


def _redact_ws(frame: object, counter: _Counter) -> Any:
    if not isinstance(frame, Mapping):
        return _redact_obj(frame, counter)
    domain = frame.get("domain")
    if domain == "termo":
        secret = _termo_is_secret(frame)
        out: dict[Any, Any] = {}
        for key, value in frame.items():
            if key in _TERMO_META_FIELDS:
                out[key] = copy.deepcopy(value)
            elif secret or _is_secret_dict_key(key):
                out[key] = _mark(value, counter)
            else:
                out[key] = _redact_obj(value, counter)
        return out
    if domain == "bus":
        secret = "key" not in frame or is_secret_key(frame.get("key"))
        out = {}
        for key, value in frame.items():
            if key in _BUS_META_FIELDS:
                out[key] = copy.deepcopy(value)
            elif (secret and key == "value") or _is_secret_dict_key(key):
                out[key] = _mark(value, counter)
            else:
                out[key] = _redact_obj(value, counter)
        return out
    return _redact_obj(frame, counter)


def redact_ws_frame(frame: object) -> tuple[Any, int]:
    """Redact one decoded WebSocket frame.

    * ``{"domain": "termo", "path": "G.U.S.K", "value": ...}``: the path is split
      on ``.``; with exactly four parts the record rules apply to ``value``;
      otherwise the value is redacted if any part matches the key catch-all
      or a secret group.  A frame without a string path has its value redacted.
    * ``{"domain": "bus", "key": ..., "value": ...}``: the key catch-all.
    * anything else: recursive dict redaction.
    """
    counter = _Counter()
    return _redact_ws(frame, counter), counter.n


def redact_capture_file(name: str, content: Any) -> tuple[Any, int]:
    """Re-apply the redaction rules appropriate for one capture file.

    Used by every offline command (``inventory``, ``diff``,
    ``sanitize-fixtures``): a capture directory may be older, hand-edited or
    not produced by the probe, so its content is never trusted to be redacted.
    """
    if fnmatch.fnmatch(name, "interface*.json") or name == "overrides.json":
        return redact_records(content)
    if name.endswith(".jsonl") and isinstance(content, list):
        total = 0
        lines: list[Any] = []
        for line in content:
            if isinstance(line, Mapping) and "frame" in line:
                frame, count = redact_ws_frame(line["frame"])
                new = dict(line)
                new["frame"] = frame
                lines.append(new)
            else:
                redacted, count = redact_dict(line)
                lines.append(redacted)
            total += count
        return lines, total
    return redact_dict(content)


# ---------------------------------------------------------------------------
# HAR sanitiser
# ---------------------------------------------------------------------------

_DROP_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
_STORAGE_KEYS: Final = frozenset({"localstorage", "sessionstorage"})
_LOGIN_PATH_RE: Final = re.compile(r"/api/get-token/?")
_RECORD_REQUEST_RE: Final = re.compile(r"/api/(?:interface|overrides)/bulk_(?:update|delete)/?")
_RECORD_RESPONSE_RE: Final = re.compile(r"/api/(?:interface|overrides)/?")
_CONTROLLER_HINT_RE: Final = re.compile(
    r"/api/(?:alive|interface|overrides|get-token|plant/conf|config|me)/?|/ws/?"
)
_KEEP_TEXT_MIME_RE: Final = re.compile(
    r"^(?:text/html|text/css|(?:text|application)/javascript|image/[a-z0-9.+-]+|font/[a-z0-9.+-]+)",
    re.IGNORECASE,
)


def _strip_storage(obj: object, counts: dict[str, int]) -> Any:
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and any(name in key.lower() for name in _STORAGE_KEYS):
                counts["storage_blobs_dropped"] += 1
                continue
            out[key] = _strip_storage(value, counts)
        return out
    if isinstance(obj, list):
        return [_strip_storage(item, counts) for item in obj]
    return obj


def _filter_headers(headers: object, counts: dict[str, int]) -> list[Any]:
    if not isinstance(headers, list):
        return []
    kept: list[Any] = []
    for header in headers:
        if not isinstance(header, Mapping):
            counts["headers_removed"] += 1
            continue
        name = _norm(header.get("name")).lower()
        if name in _DROP_HEADERS or is_secret_key(name):
            counts["headers_removed"] += 1
            continue
        kept.append(dict(header))
    return kept


def _redact_query_list(items: object, counts: dict[str, int]) -> list[Any]:
    if not isinstance(items, list):
        return []
    out: list[Any] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        new = dict(item)
        if (
            _is_secret_dict_key(new.get("name"))
            and "value" in new
            and not is_redacted(new["value"])
        ):
            counts["values_redacted"] += 1
            new["value"] = redacted_marker(new["value"])
        out.append(new)
    return out


def _redact_pairs(
    pairs: list[tuple[str, str]], counts: dict[str, int]
) -> tuple[list[tuple[str, str]], bool]:
    changed = False
    out: list[tuple[str, str]] = []
    for name, value in pairs:
        if _is_secret_dict_key(name) and not is_redacted(value):
            out.append((name, redacted_marker(value)))
            counts["values_redacted"] += 1
            changed = True
        else:
            out.append((name, value))
    return out, changed


def _redact_url(url: str, counts: dict[str, int]) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        counts["values_redacted"] += 1
        return "<redacted url>"
    if not parts.query:
        return url
    new_pairs, changed = _redact_pairs(parse_qsl(parts.query, keep_blank_values=True), counts)
    if not changed:
        return url
    query = urlencode(new_pairs, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _redact_json_for_path(data: object, path: str, *, request: bool) -> tuple[Any, int]:
    if request:
        if _RECORD_REQUEST_RE.fullmatch(path):
            return redact_records(data)
        return redact_dict(data)
    if _RECORD_RESPONSE_RE.fullmatch(path):
        return redact_records(data)
    return redact_dict(data)


def _sanitise_post_data(
    post: dict[str, Any], path: str, is_login: bool, trusted: bool, counts: dict[str, int]
) -> dict[str, Any] | None:
    if is_login or not trusted:
        counts["login_bodies_dropped" if is_login else "foreign_bodies_dropped"] += 1
        return None
    out = dict(post)
    if "params" in out:
        out["params"] = _redact_query_list(out["params"], counts)
    text = out.get("text")
    if isinstance(text, str) and text:
        mime = _norm(out.get("mimeType")).lower()
        try:
            data = json.loads(text)
        except ValueError:
            if "x-www-form-urlencoded" in mime:
                new_pairs, _changed = _redact_pairs(parse_qsl(text, keep_blank_values=True), counts)
                out["text"] = urlencode(new_pairs, quote_via=quote)
            else:
                counts["non_json_bodies_dropped"] += 1
                out["text"] = ""
                out["comment"] = f"non-JSON body dropped by aiorehom (len={len(text)})"
        else:
            redacted, n = _redact_json_for_path(data, path, request=True)
            counts["values_redacted"] += n
            out["text"] = json.dumps(redacted, ensure_ascii=False)
    return out


def _keep_non_json(mime: str, path: str) -> bool:
    """Non-JSON bodies kept: images, and static web-UI assets outside ``/api/``."""
    if mime.startswith("image/"):
        return True
    return not path.startswith("/api/") and _KEEP_TEXT_MIME_RE.match(mime) is not None


def _drop_content_text(out: dict[str, Any], comment: str) -> dict[str, Any]:
    out.pop("text", None)
    out.pop("encoding", None)
    out["comment"] = comment
    return out


def _sanitise_content(
    content: dict[str, Any], path: str, is_login: bool, trusted: bool, counts: dict[str, int]
) -> dict[str, Any]:
    out = dict(content)
    text = out.get("text")
    if not isinstance(text, str) or not text:
        return out
    if is_login or not trusted:
        counts["login_bodies_dropped" if is_login else "foreign_bodies_dropped"] += 1
        return _drop_content_text(out, "body dropped by aiorehom")
    mime = _norm(out.get("mimeType")).lower()
    raw_text = text
    if _norm(out.get("encoding")).lower() == "base64":
        if _keep_non_json(mime, path):
            return out
        try:
            raw_text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            counts["non_json_bodies_dropped"] += 1
            return _drop_content_text(out, "undecodable body dropped by aiorehom")
    try:
        data = json.loads(raw_text)
    except ValueError:
        if _keep_non_json(mime, path):
            return out
        counts["non_json_bodies_dropped"] += 1
        return _drop_content_text(out, "non-JSON body dropped by aiorehom")
    redacted, n = _redact_json_for_path(data, path, request=False)
    counts["values_redacted"] += n
    out.pop("encoding", None)
    out["text"] = json.dumps(redacted, ensure_ascii=False)
    return out


def _sanitise_ws_messages(
    messages: object, trusted: bool, counts: dict[str, int]
) -> list[Any] | None:
    if not isinstance(messages, list):
        return None
    if not trusted:
        counts["foreign_bodies_dropped"] += 1
        return []
    out: list[Any] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        new = dict(message)
        counts["ws_frames"] += 1
        data = new.get("data")
        opcode = new.get("opcode", 1)
        if opcode != 1 or not isinstance(data, str):
            new["data"] = ""
            new["_aiorehom"] = "binary or missing payload dropped"
            out.append(new)
            continue
        try:
            frame = json.loads(data)
        except ValueError:
            new["data"] = ""
            new["_aiorehom"] = {"_raw_len": len(data), "_non_json": True}
            out.append(new)
            continue
        redacted, n = redact_ws_frame(frame)
        counts["ws_values_redacted"] += n
        new["data"] = json.dumps(redacted, ensure_ascii=False)
        out.append(new)
    return out


def _entry_host_path(entry: Mapping[str, Any]) -> tuple[str, str]:
    request = entry.get("request")
    url = _norm(request.get("url")) if isinstance(request, Mapping) else ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "", ""
    return host, parts.path


def _infer_controller_hosts(entries: Iterable[Any]) -> set[str]:
    hosts: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            host, path = _entry_host_path(entry)
            if host and _CONTROLLER_HINT_RE.fullmatch(path):
                hosts.add(host)
    return hosts


def _sanitise_entry(entry: dict[str, Any], hosts: set[str], counts: dict[str, int]) -> None:
    host, path = _entry_host_path(entry)
    trusted = host in hosts
    is_login = _LOGIN_PATH_RE.fullmatch(path) is not None
    request = entry.get("request")
    if isinstance(request, dict):
        request["headers"] = _filter_headers(request.get("headers"), counts)
        if "cookies" in request:
            request["cookies"] = []
        if isinstance(request.get("url"), str):
            request["url"] = _redact_url(request["url"], counts)
        if "queryString" in request:
            request["queryString"] = _redact_query_list(request["queryString"], counts)
        post = request.get("postData")
        if isinstance(post, dict):
            sanitised = _sanitise_post_data(post, path, is_login, trusted, counts)
            if sanitised is None:
                request.pop("postData", None)
            else:
                request["postData"] = sanitised
        elif post is not None:
            request.pop("postData", None)
    response = entry.get("response")
    if isinstance(response, dict):
        response["headers"] = _filter_headers(response.get("headers"), counts)
        if "cookies" in response:
            response["cookies"] = []
        content = response.get("content")
        if isinstance(content, dict):
            response["content"] = _sanitise_content(content, path, is_login, trusted, counts)
    if "_webSocketMessages" in entry:
        messages = _sanitise_ws_messages(entry["_webSocketMessages"], trusted, counts)
        if messages is None:
            entry.pop("_webSocketMessages", None)
        else:
            entry["_webSocketMessages"] = messages


def redact_har(
    har: Mapping[str, Any], controller_hosts: Iterable[str] | None = None
) -> dict[str, Any]:
    """Return a sanitised copy of a HAR document.

    * ``/api/get-token/``: request and response bodies dropped entirely.
    * ``Authorization``, ``Proxy-Authorization``, ``Cookie``, ``Set-Cookie`` and
      any header whose name matches the secret catch-all are removed; cookie
      arrays are emptied; secret-looking query parameters are redacted.
    * JSON request bodies (``bulk_update``/``bulk_delete`` record arrays: record
      rules; others: dict rules) and JSON response bodies (``/interface/``,
      ``/overrides/``: record rules; everything else: dict rules) are redacted.
    * ``_webSocketMessages`` (Chrome extension) are redacted with the WS rules.
    * ``localStorage``/``sessionStorage`` blobs are dropped wherever they appear.
    * Entries for hosts other than the controller keep their metadata but lose
      their bodies.  Non-JSON API bodies are dropped; static web-UI assets kept.

    ``controller_hosts`` defaults to the hosts that served Rehom API paths
    (``/api/alive/``, ``/api/interface/``, ``/ws/``, ...).
    The counts are reported under ``log._aiorehom_redaction``.
    """
    counts: dict[str, int] = {
        "entries": 0,
        "headers_removed": 0,
        "values_redacted": 0,
        "login_bodies_dropped": 0,
        "foreign_bodies_dropped": 0,
        "non_json_bodies_dropped": 0,
        "storage_blobs_dropped": 0,
        "ws_frames": 0,
        "ws_values_redacted": 0,
    }
    cleaned = _strip_storage(copy.deepcopy(dict(har)), counts)
    log = cleaned.get("log")
    if not isinstance(log, dict):
        cleaned["log"] = {"entries": [], "_aiorehom_redaction": counts}
        return dict(cleaned)
    entries = log.get("entries")
    if not isinstance(entries, list):
        entries = []
        log["entries"] = entries
    if controller_hosts is None:
        hosts = _infer_controller_hosts(entries)
    else:
        hosts = {h.lower() for h in controller_hosts}
    for entry in entries:
        if isinstance(entry, dict):
            counts["entries"] += 1
            _sanitise_entry(entry, hosts, counts)
    log["_aiorehom_redaction"] = counts
    return dict(cleaned)


def pseudonymise_fixture(capture: Mapping[str, Any], salt: str) -> dict[str, Any]:
    """Pseudonymise a capture for use as a test fixture (see :mod:`aiorehom.pseudonymise`)."""
    from .pseudonymise import pseudonymise_fixture as _impl  # noqa: PLC0415 (avoid cycle)

    return _impl(capture, salt)
