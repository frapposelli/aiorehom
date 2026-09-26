"""Pseudonymisation of personal data for test fixtures (separate from redaction).

Deterministic for a given salt: the same input and salt always give the same
output; different salts give unrelated pseudonyms.  Secrets must already be
redacted (see :mod:`aiorehom.redact`) -- this step handles *personal* data:

* ``NOME`` in any group -> ``"Zona <id>"`` / ``"VMC <id>"`` / ``"Fancoil <id>"``
  (``ZONA``/``DEUM``/``FANCOIL``) or ``"<Gruppo> <id>"`` (``ATTUATORE``...)
* ``REHOM.LOCALITA`` and METEO location rows -> ``"City"``
* ``METEO.POSIZIONE`` (GPS ``"lat,lon"``) -> fixed fake coordinates
  ``45.0...,10.0...`` with the same number of decimals; ``METEO`` ``LAT*``/``LON*``
  -> ``0.0``
* ``METEO_DATA`` rows and WS frames (OpenWeatherMap JSON strings): numeric
  weather fields are kept; coordinates, city/place names, country, ids (except
  ``weather[].id`` condition codes), sunrise/sunset, timezone and population
  are replaced; a value that is not JSON becomes ``"<pseudonymised>"``.  Any
  other JSON-string value loses its coordinates, places, country and
  sunrise/sunset too
* ``MATRICOLA``/``CUSTOM_ID``/``INSTALLATION_ID`` -> stable fake with the same
  shape (salted HMAC); an id that is a bare MAC (``/config/`` ``INSTALLATION_ID``
  is one) maps to the same fake as that MAC
* ``WEBSERVER.MacAddress`` -> ``02:00:00:xx:xx:xx`` (format kept); the real
  MAC is also replaced wherever else it appears (any case, any separator), and
  every MAC-looking string is replaced
* any IPv4 address -> ``192.0.2.x`` (stable mapping); any IPv6 address ->
  ``2001:db8::x`` (loopback and unspecified kept)
* ``WIFI.SSID`` -> ``"ssid"``, also wherever else the real SSID appears
* ``UTEN`` key names -> ``user1``, ``user2`` ... (``admin`` kept), including
  user names that contain dots
* ``/me/``: ``username`` -> ``userN``, ``email`` -> ``user@example.invalid``,
  ``first_name``/``last_name`` -> ``Name``/``Surname``, ``id`` -> ``1``
* ``/config/`` host names -> ``host-<hex>.example``; LAN host names
  (``*.local``, ``*.lan``...) and e-mail addresses in any string are replaced too
* free-text echoes: every real name, locality, coordinate, user name, e-mail,
  first/last name, SSID, alphanumeric serial and controller host name found in
  the capture is also replaced wherever else it appears in a string value
  (whole token, any case)
* domotica payloads: every string field except a small allowlist of
  structural fields (``id``, ``kind``, ``subkind``, ``icon``, ``tag``,
  ``knx_address``, ``ets_type``, control values in ``state``...) -> ``"<Kind> <field> <id>"``;
  ``name``/``description``/``knx_level0..2`` -> ``"<Kind> <id>"`` /
  ``"<Kind> description <id>"`` / ``"<Kind> level<n> <id>"``

Personal values are pseudonymised whatever their JSON type (a numeric
``MATRICOLA`` keeps being a number); containers are replaced wholesale.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, Final

from .redact import is_redacted
from .store import make_path, norm, split_path

__all__ = ["Pseudonymiser", "is_personal_field", "personal_kind", "pseudonymise_fixture"]

_IPV4_RE: Final = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
#: IPv6 candidates (validated with :mod:`ipaddress`; times such as ``10:41:06`` are rejected).
_IPV6_RE: Final = re.compile(
    r"(?<![0-9A-Za-z:.])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?:%[0-9A-Za-z_-]+)?"
    r"(?![0-9A-Za-z:.])"
)
_IPV6_FULL_COLONS: Final = 7
_DOC_NET6: Final = ipaddress.IPv6Network("2001:db8::/32")
_MAC_RE: Final = re.compile(r"([0-9A-Fa-f]{2})([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2}")
#: A separated MAC inside free text (not part of a longer hex/colon run).
_MAC_TEXT_RE: Final = re.compile(
    r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])"
)
_BARE_MAC_RE: Final = re.compile(r"[0-9A-Fa-f]{12}")
_MAC_SEP_RE: Final = re.compile(r"[:-]")
#: Our own fake MACs start with the locally administered prefix 02:00:00.
_FAKE_MAC_PREFIX: Final = "020000"
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
_FAKE_EMAIL: Final = "user@example.invalid"
#: LAN host names inside free text (public domains such as an API host are kept).
_LAN_HOST_RE: Final = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:local|lan|home|internal|intranet|localdomain|home\.arpa)(?![A-Za-z0-9-])",
    re.IGNORECASE,
)
#: Dict keys whose string value is a MAC address / an SSID, wherever they appear.
_MAC_KEY_RE: Final = re.compile(r"(?:^|_)MAC(?:_|ADDRESS|$)", re.IGNORECASE)
_SSID_KEY_RE: Final = re.compile(r"SSID", re.IGNORECASE)
_EMAIL_KEY_RE: Final = re.compile(r"E_?MAIL", re.IGNORECASE)
#: Dict keys (exact, any case) holding a serial number or installation id.
_SERIAL_KEY_RE: Final = re.compile(
    r"INSTALLATION_ID|INSTALL_ID|MATRICOLA|CUSTOM_ID|SERIAL|SERIAL_NUMBER|SERIALE", re.IGNORECASE
)
#: Dict keys (exact, lower case) holding a user name.
_USER_KEYS: Final = frozenset({"username", "user_name", "user", "utente"})
_TZ_KEY_RE: Final = re.compile(r"TIMEZONE|TIME_ZONE|TZ", re.IGNORECASE)
_TZ_VALUE_RE: Final = re.compile(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}")
#: Literals shorter than this are never replaced in free text (too ambiguous).
_MIN_LITERAL: Final = 3
_MIN_SSID_LITERAL: Final = _MIN_LITERAL
#: A numeric literal needs this many characters, or this many decimals (a coordinate).
_MIN_NUMERIC_LITERAL: Final = 8
_MIN_LITERAL_DECIMALS: Final = 3
_PSEUDONYMISED: Final = "<pseudonymised>"
_HOSTNAME_RE: Final = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:local|lan|home|internal|localdomain|arpa|com|net|org|it|io|eu|de|fr|uk|ch|info|biz)",
    re.IGNORECASE,
)
_VERSION_KEY_RE: Final = re.compile(r"^(?:VER|VERSION|VERSIONE)", re.IGNORECASE)
_METEO_LOCATION_RE: Final = re.compile(
    r"LOCALIT|CITT|CITY|LOCATION|LUOGO|PLACE|COMUNE", re.IGNORECASE
)
_METEO_COORD_RE: Final = re.compile(r"^(?:LAT|LON|LNG)", re.IGNORECASE)
_METEO_POSITION_RE: Final = re.compile(r"POSIZIONE|POSITION|COORD|GPS", re.IGNORECASE)
_WEATHER_GROUP: Final = "METEO_DATA"
#: Fixed fake coordinates (never derived from the real ones).
_FAKE_LAT: Final = 45.0
_FAKE_LON: Final = 10.0
_NUMBER_RE: Final = re.compile(r"-?\d+(?:\.(\d+))?")
_POSITION_SPLIT_RE: Final = re.compile(r"(\s*[,;]\s*)")
#: JSON keys (lower case) with location meaning, used for JSON-string values.
_LAT_KEYS: Final = frozenset({"lat", "latitude", "latitudine"})
_LON_KEYS: Final = frozenset({"lon", "lng", "long", "longitude", "longitudine"})
_POSITION_KEYS: Final = frozenset(
    {"coord", "coords", "coordinate", "coordinates", "posizione", "position", "gps", "geo"}
)
_PLACE_KEYS: Final = frozenset(
    {
        "city",
        "citta",
        "comune",
        "localita",
        "locality",
        "place",
        "location",
        "town",
        "village",
        "zip",
        "postcode",
        "postal_code",
    }
)
_COUNTRY_KEYS: Final = frozenset({"country", "country_code", "countrycode"})
_SUN_KEYS: Final = frozenset({"sunrise", "sunset"})
#: Extra location keys scrubbed only in weather payloads (``METEO_DATA``).
_WEATHER_PLACE_KEYS: Final = frozenset({"name", "state", "local_names"})
_WEATHER_ZERO_KEYS: Final = frozenset({"timezone", "timezone_offset", "population"})
#: ``id`` under this key is a weather condition code (kept); every other ``id`` is zeroed.
_WEATHER_CODE_PARENT: Final = "weather"
_NAME_KEYS: Final = {"ZONA": "Zona", "DEUM": "VMC", "FANCOIL": "Fancoil"}
_DOMOTICA_LABELS: Final = {
    "object": "Object",
    "objects": "Object",
    "full_objects": "Object",
    "rooms": "Room",
    "room_data": "Room",
    "scenarios": "Scenario",
    "members": "Member",
    "rules": "Rule",
    "config": "Config",
}
_ME_NAME_FIELDS: Final = {"first_name": "Name", "last_name": "Surname", "full_name": "Name Surname"}
#: Domotica fields whose string value is structural (not user-chosen text): kept.
_DOMOTICA_KEEP_KEYS: Final = frozenset(
    {
        "id",
        "kind",
        "subkind",
        "icon",
        "tag",
        "knx_address",
        "ets_type",
        "time",
        "day_of_week",
        "active",
        "detachment_priority",
        "triggers_scenario",
        "power_detatch_algorithm",
        "power_consumption_auto_detach",
        "power_consumption_limit",
    }
)
#: Domotica fields that hold an id: kept only when the string is all digits.
_DOMOTICA_ID_KEYS: Final = frozenset({"zone", "room", "scenario", "trigger", "objects_ids"})
#: Domotica dicts of control/feedback values (keys and values are protocol, not names).
_DOMOTICA_VALUE_DICTS: Final = frozenset({"state", "stable_state", "controls"})
_KNX_LEVEL_RE: Final = re.compile(r"knx_level(\d+)")
#: Any key name that suggests personal data (used by ``rehom-probe diff`` masking).
_PERSONAL_KEY_RE: Final = re.compile(
    r"NOME|NAME|LOCALIT|CITT|CITY|LOCATION|LUOGO|PLACE|COMUNE|SSID|MATRICOLA|SERIAL|CUSTOM_ID"
    r"|POSIZIONE|COORD|GPS|INSTALLATION_ID|E_?MAIL"
    r"|(?:^|_)MAC(?:_|ADDRESS|$)|(?:^|_)(?:LAT|LATITUDE|LON|LONG|LNG|LONGITUDE)(?:_|$)",
    re.IGNORECASE,
)


def personal_kind(gruppo: str, key: str) -> str | None:
    """Kind of personal data a record ``(Gruppo, Key)`` holds, or ``None``.

    One of ``name``, ``city``, ``weather``, ``position``, ``coord``, ``serial``,
    ``installation``, ``mac``, ``ssid``.
    """
    if key == "NOME":
        return "name"
    if (gruppo, key) == ("REHOM", "LOCALITA") or (
        gruppo == "METEO" and _METEO_LOCATION_RE.search(key)
    ):
        return "city"
    if gruppo == _WEATHER_GROUP:
        return "weather"
    if gruppo == "METEO" and _METEO_POSITION_RE.search(key):
        return "position"
    if gruppo == "METEO" and _METEO_COORD_RE.match(key):
        return "coord"
    if key in ("MATRICOLA", "CUSTOM_ID"):
        return "serial"
    if key == "INSTALLATION_ID":
        return "installation"
    if (gruppo, key) == ("WEBSERVER", "MacAddress"):
        return "mac"
    if (gruppo, key) == ("WIFI", "SSID"):
        return "ssid"
    return None


def is_personal_field(gruppo: str, key: str) -> bool:
    """True if a record or dict field is (or looks like) personal data."""
    return personal_kind(gruppo, key) is not None or _PERSONAL_KEY_RE.search(key) is not None


def _format_like(sample: str, value: float) -> str:
    """``value`` with as many decimals as the numeric string ``sample``."""
    match = _NUMBER_RE.fullmatch(sample.strip())
    decimals = len(match.group(1)) if match is not None and match.group(1) else 0
    return f"{value:.{decimals}f}"


def _fake_coordinate(value: Any, fake: float) -> Any:
    """A fixed fake coordinate of the same JSON type as ``value``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(fake)
    if isinstance(value, float):
        return fake
    if isinstance(value, str):
        if not value or is_redacted(value):
            return value
        if _NUMBER_RE.fullmatch(value.strip()):
            return _format_like(value, fake)
        return str(fake)
    return _PSEUDONYMISED


def fake_position(value: str) -> str:
    """Fixed fake ``"lat,lon"`` keeping the separator and the number of decimals."""
    parts = _POSITION_SPLIT_RE.split(value.strip())
    if len(parts) == 3 and all(_NUMBER_RE.fullmatch(p) for p in (parts[0], parts[2])):
        return _format_like(parts[0], _FAKE_LAT) + parts[1] + _format_like(parts[2], _FAKE_LON)
    return f"{_FAKE_LAT},{_FAKE_LON}"


def _zero_like(value: Any, text: str) -> Any:
    """A neutral value of the same JSON type (``0``, ``0.0``, ``text``/``"0"``)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return 0
    if isinstance(value, float):
        return 0.0
    if isinstance(value, str):
        if not value or is_redacted(value):
            return value
        return "0" if _NUMBER_RE.fullmatch(value.strip()) else text
    return _PSEUDONYMISED


def _constant(replacement: str) -> Callable[[re.Match[str]], str]:
    def repl(_match: re.Match[str]) -> str:
        return replacement

    return repl


class Pseudonymiser:
    """Stateful, deterministic pseudonymiser (one instance per capture)."""

    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("a non-empty salt is required")
        self._key = salt.encode("utf-8")
        self._ips: dict[str, str] = {}
        self._used_ips: set[int] = set()
        self._ip6s: dict[str, str] = {}
        self._users: dict[str, str] = {}
        self._hosts: dict[str, str] = {}
        self._mac_literals: list[re.Pattern[str]] = []
        self._literals: list[tuple[re.Pattern[str], str]] = []

    # -- primitives -----------------------------------------------------------

    def _stream(self, tag: str, value: str, length: int) -> bytes:
        out = b""
        counter = 0
        while len(out) < length:
            msg = f"{tag}\x00{counter}\x00{value}".encode()
            out += hmac.new(self._key, msg, hashlib.sha256).digest()
            counter += 1
        return out[:length]

    def fake_like(self, value: str, tag: str) -> str:
        """Replace digits/letters with salted pseudo-random ones, keeping the shape."""
        stream = self._stream(tag, value, max(1, len(value)))
        chars: list[str] = []
        for ch, byte in zip(value, stream, strict=False):
            if "0" <= ch <= "9":
                chars.append(chr(ord("0") + byte % 10))
            elif "A" <= ch <= "Z":
                chars.append(chr(ord("A") + byte % 26))
            elif "a" <= ch <= "z":
                chars.append(chr(ord("a") + byte % 26))
            else:
                chars.append(ch)
        return "".join(chars)

    def ip(self, address: str) -> str:
        """Map an IPv4 address to ``192.0.2.x`` (stable, collision-free within a capture)."""
        if address in self._ips:
            return self._ips[address]
        start = int.from_bytes(self._stream("ip", address, 2), "big") % 254
        for offset in range(254):
            candidate = 1 + (start + offset) % 254
            if candidate not in self._used_ips:
                self._used_ips.add(candidate)
                fake = f"192.0.2.{candidate}"
                self._ips[address] = fake
                return fake
        fake = f"192.0.2.{1 + start}"
        self._ips[address] = fake
        return fake

    def ip6(self, address: str) -> str:
        """Map an IPv6 address to ``2001:db8::x`` (documentation prefix, stable)."""
        key = ipaddress.IPv6Address(address).compressed
        if key not in self._ip6s:
            suffix = 1 + int.from_bytes(self._stream("ip6", key, 2), "big") % 0xFFFE
            self._ip6s[key] = f"2001:db8::{suffix:x}"
        return self._ip6s[key]

    def text_ips(self, text: str) -> str:
        """Replace every IPv4 address in ``text`` (netmasks, 0.x, 127.x and 192.0.2.x kept)."""

        def repl(match: re.Match[str]) -> str:
            octets = [int(g) for g in match.groups()]
            if any(o > 255 for o in octets):
                return match.group(0)
            if octets[0] in (0, 127, 255) or octets[:3] == [192, 0, 2]:
                return match.group(0)
            return self.ip(match.group(0))

        return _IPV4_RE.sub(repl, text)

    def _ipv6_in_text(self, match: re.Match[str]) -> str:
        found = match.group(0)
        address, sep, zone = found.partition("%")
        if "::" not in address and address.count(":") != _IPV6_FULL_COLONS:
            return found  # e.g. a time of day, or a MAC address
        try:
            parsed = ipaddress.IPv6Address(address)
        except ValueError:
            return found
        if parsed.is_loopback or parsed.is_unspecified or parsed in _DOC_NET6:
            return found
        return self.ip6(address) + sep + zone

    def mac(self, value: str) -> str:
        """``02:00:00`` + three salted bytes, keeping the separator and letter case.

        The fake depends only on the 12 hex digits, so the same MAC written with
        another separator or case maps to the same fake.
        """
        digits = _MAC_SEP_RE.sub("", value)
        if _BARE_MAC_RE.fullmatch(digits) is None:
            return self.fake_like(value, "mac")
        fake = _FAKE_MAC_PREFIX + self._stream("mac", digits.lower(), 3).hex()
        pairs = [fake[i : i + 2] for i in range(0, 12, 2)]
        match = _MAC_RE.fullmatch(value)
        if match is not None:
            out = match.group(2).join(pairs)
        elif _BARE_MAC_RE.fullmatch(value):
            out = fake
        else:
            out = ":".join(pairs)
        return out.upper() if any(c.isupper() for c in value) else out

    def installation_id(self, value: str) -> str:
        """An installation id: the MAC fake if it is a MAC (bare or separated), else shape-kept."""
        if _BARE_MAC_RE.fullmatch(_MAC_SEP_RE.sub("", value)):
            return self.mac(value)
        return self.fake_like(value, "INSTALLATION_ID")

    def register_mac(self, value: str) -> None:
        """Replace this real MAC wherever it appears (any case, with or without separators)."""
        digits = _MAC_SEP_RE.sub("", value)
        if _BARE_MAC_RE.fullmatch(digits) is None:
            return
        pairs = [re.escape(digits[i : i + 2]) for i in range(0, 12, 2)]
        pattern = re.compile(
            r"(?<![0-9A-Fa-f])" + r"[:-]?".join(pairs) + r"(?![0-9A-Fa-f])", re.IGNORECASE
        )
        if pattern.pattern not in {p.pattern for p in self._mac_literals}:
            self._mac_literals.append(pattern)

    def register_literal(self, value: str, replacement: str) -> None:
        """Replace ``value`` with ``replacement`` wherever it appears in a string value.

        Whole-token match, any case.  Ignored when too short or ambiguous
        (fewer than 3 characters; a number with fewer than 8 digits and fewer
        than 3 decimals, which could be any reading), when it is a redaction
        marker, or when the replacement would itself match (replacements must
        be idempotent).
        """
        token = value.strip()
        if len(token) < _MIN_LITERAL or is_redacted(token) or token == replacement:
            return
        number = _NUMBER_RE.fullmatch(token)
        if number is not None:
            decimals = len(number.group(1) or "")
            if decimals < _MIN_LITERAL_DECIMALS and len(token) < _MIN_NUMERIC_LITERAL:
                return
        if not any(ch.isalnum() for ch in token):
            return
        pattern = re.compile(
            r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", re.IGNORECASE
        )
        if pattern.search(replacement) is not None:
            return
        if any(existing.pattern == pattern.pattern for existing, _ in self._literals):
            return
        self._literals.append((pattern, replacement))
        self._literals.sort(key=lambda item: len(item[0].pattern), reverse=True)

    def register_ssid(self, value: str) -> None:
        """Replace this real SSID wherever it appears (any case) with ``ssid``."""
        if len(value.strip()) < _MIN_SSID_LITERAL or is_redacted(value):
            return
        self.register_literal(value, "ssid")

    def _mac_in_text(self, match: re.Match[str]) -> str:
        found = match.group(0)
        if _MAC_SEP_RE.sub("", found).lower().startswith(_FAKE_MAC_PREFIX):
            return found  # already one of our fakes
        return self.mac(found)

    @staticmethod
    def _email_in_text(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if found.lower().endswith("@example.invalid") else _FAKE_EMAIL

    def text(self, value: str) -> str:
        """Free-text replacement.

        JSON-string values lose their location data; then IPv4/IPv6 addresses,
        MAC addresses, e-mail addresses, LAN host names and every registered
        literal (names, locality, users, SSID...) are replaced.
        """
        if value.lstrip()[:1] in ("{", "["):
            return self._json_text(value, weather=False)
        return self._plain_text(value)

    def _plain_text(self, value: str) -> str:
        """:meth:`text` without the JSON-string step."""
        out = self.text_ips(value)
        out = _IPV6_RE.sub(self._ipv6_in_text, out)
        for pattern in self._mac_literals:
            out = pattern.sub(self._mac_in_text, out)
        out = _MAC_TEXT_RE.sub(self._mac_in_text, out)
        out = _EMAIL_RE.sub(self._email_in_text, out)
        out = _LAN_HOST_RE.sub(lambda m: self.host(m.group(0)), out)
        for pattern, replacement in self._literals:
            out = pattern.sub(_constant(replacement), out)
        return out

    def prepare_users(self, names: Iterable[str]) -> None:
        """Assign ``user1..n`` to the non-admin names in salted-hash order."""
        pending = sorted(
            {n for n in names if n and n != "admin" and n not in self._users},
            key=lambda n: self._stream("user", n, 8),
        )
        for name in pending:
            self._users[name] = f"user{len(self._users) + 1}"

    def user(self, name: str) -> str:
        if name == "admin" or not name:
            return name
        if name not in self._users:
            self.prepare_users([name])
        return self._users[name]

    def host(self, name: str) -> str:
        if name not in self._hosts:
            self._hosts[name] = f"host-{self._stream('host', name.lower(), 3).hex()}.example"
        return self._hosts[name]

    # -- JSON-string values (weather payloads) --------------------------------

    def _json_text(self, value: str, *, weather: bool) -> str:
        """Scrub location data from a JSON-string value, keeping its formatting style."""
        try:
            data = json.loads(value)
        except ValueError:
            if weather:
                return _PSEUDONYMISED
            return self._plain_text(value)
        if not isinstance(data, (dict, list)):
            return self._plain_text(value)
        scrubbed = self.scrub_json(data, weather=weather)
        if scrubbed == data:
            return value
        body = value.strip()
        lead = value[: len(value) - len(value.lstrip())]
        trail = value[len(value.rstrip()) :]
        styles: tuple[tuple[tuple[str, str] | None, bool], ...] = (
            ((",", ":"), False),
            ((",", ":"), True),
            (None, False),
            (None, True),
        )
        for separators, ascii_only in styles:
            if json.dumps(data, separators=separators, ensure_ascii=ascii_only) == body:
                dumped = json.dumps(scrubbed, separators=separators, ensure_ascii=ascii_only)
                return lead + dumped + trail
        return lead + json.dumps(scrubbed, separators=(",", ":"), ensure_ascii=False) + trail

    def scrub_json(
        self, obj: Any, *, weather: bool, parent: str = "", in_place: bool = False
    ) -> Any:
        """Replace location data in a parsed JSON payload; other values kept.

        Everywhere: ``lat``/``lon`` -> fixed fakes, ``coord``-like -> fake
        position, ``city``/``place``/``zip``... -> ``"City"`` (whole block),
        ``country`` -> ``"XX"``, ``sunrise``/``sunset`` -> ``0``.  With
        ``weather=True`` (``METEO_DATA``) also ``name``/``state``/``local_names``
        -> ``"City"``, ``timezone``/``population`` -> ``0`` and every ``id``
        except ``weather[].id`` (a condition code) -> ``0``.  Inside a place
        block every string becomes ``"City"`` and every number ``0``.
        """
        if isinstance(obj, Mapping):
            return {
                key: self._scrub_json_field(
                    key.lower() if isinstance(key, str) else "",
                    value,
                    weather=weather,
                    parent=parent,
                    in_place=in_place,
                )
                for key, value in obj.items()
            }
        if isinstance(obj, list):
            return [
                self.scrub_json(item, weather=weather, parent=parent, in_place=in_place)
                for item in obj
            ]
        if in_place:
            return _zero_like(obj, "City")
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def _scrub_json_field(
        self, name: str, value: Any, *, weather: bool, parent: str, in_place: bool
    ) -> Any:
        if name in _LAT_KEYS:
            return _fake_coordinate(value, _FAKE_LAT)
        if name in _LON_KEYS:
            return _fake_coordinate(value, _FAKE_LON)
        if name in _POSITION_KEYS:
            return self._fake_position_value(value)
        if name in _COUNTRY_KEYS:
            return _zero_like(value, "XX")
        if name in _SUN_KEYS:
            return _zero_like(value, "0")
        if in_place or name in _PLACE_KEYS or (weather and name in _WEATHER_PLACE_KEYS):
            if isinstance(value, (Mapping, list)):
                return self.scrub_json(value, weather=weather, parent=name, in_place=True)
            return _zero_like(value, "City")
        if weather and (
            name in _WEATHER_ZERO_KEYS or (name == "id" and parent != _WEATHER_CODE_PARENT)
        ):
            return _zero_like(value, "0")
        return self.scrub_json(value, weather=weather, parent=name)

    def _fake_position_value(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return self.scrub_json(value, weather=True, in_place=True)
        if isinstance(value, list):
            fakes = (_FAKE_LAT, _FAKE_LON)
            return [
                _fake_coordinate(item, fakes[min(i, 1)])
                if isinstance(item, (int, float, str))
                else self.scrub_json(item, weather=True, in_place=True)
                for i, item in enumerate(value)
            ]
        if isinstance(value, str):
            return fake_position(value) if value and not is_redacted(value) else value
        return _fake_coordinate(value, 0.0)

    def _weather_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._json_text(value, weather=True)
        if isinstance(value, (Mapping, list)):
            return self.scrub_json(value, weather=True)
        return value

    # -- generic --------------------------------------------------------------

    def generic(self, obj: Any) -> Any:
        """Replace personal data in every string (keys untouched; some keys select a rule)."""
        if isinstance(obj, Mapping):
            return {k: self._generic_value(k, v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.generic(item) for item in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj

    def _generic_value(self, key: object, value: Any) -> Any:
        if not isinstance(key, str):
            return self.generic(value)
        if _VERSION_KEY_RE.match(key):
            return value
        if (
            _TZ_KEY_RE.fullmatch(key)
            and isinstance(value, str)
            and _TZ_VALUE_RE.fullmatch(value) is not None
        ):
            return value  # an IANA zone name ("Europe/Rome") is needed by consumers
        lowered = key.lower()
        if lowered in _LAT_KEYS:
            return _fake_coordinate(value, _FAKE_LAT)
        if lowered in _LON_KEYS:
            return _fake_coordinate(value, _FAKE_LON)
        if lowered in _POSITION_KEYS:
            return self._fake_position_value(value)
        if value is None or value == "" or is_redacted(value):
            return value
        if _SERIAL_KEY_RE.fullmatch(key) and isinstance(value, (str, int)):
            return self.serial_value(key, value)
        if isinstance(value, str):
            if _MAC_KEY_RE.search(key):
                return self.mac(value)
            if _SSID_KEY_RE.search(key):
                return "ssid"
            if _EMAIL_KEY_RE.search(key) and "@" in value:
                return _FAKE_EMAIL
            if lowered in _USER_KEYS:
                return self.user(value)
            if _METEO_LOCATION_RE.search(key) and any(ch.isalpha() for ch in value):
                return "City"
        return self.generic(value)

    def serial_value(self, key: str, value: str | int) -> Any:
        """Pseudonym of a serial/installation id held under dict key ``key`` (type kept)."""
        if isinstance(value, bool):
            return value
        text = str(value)
        upper = key.upper()
        fake = (
            self.installation_id(text)
            if upper.startswith("INSTALL")
            else self.fake_like(text, upper if upper in ("MATRICOLA", "CUSTOM_ID") else key)
        )
        if isinstance(value, int):
            try:
                return int(fake)
            except ValueError:
                return fake
        return fake

    # -- records --------------------------------------------------------------

    def record(self, row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        g, u, s, k = (norm(row.get(f)) for f in ("Gruppo", "Unita", "SubUni", "Key"))
        new_key = k
        if g == "UTEN":
            new_key = self.user(k)
            if "Key" in out:
                out["Key"] = new_key
        if "Valore" in out:
            out["Valore"] = self._record_value(g, u, k, out["Valore"])
        for name, value in list(out.items()):
            if name not in ("Gruppo", "Unita", "SubUni", "Key", "Valore", "path"):
                out[name] = self.generic(value)
        if "path" in out and new_key != k:
            out["path"] = make_path(g, u, s, new_key)
        return out

    def personal_text(self, kind: str, g: str, u: str, k: str, text: str) -> str:
        """Pseudonym for the string form of a personal record value of ``kind``."""
        if kind == "name":
            return f"{_NAME_KEYS.get(g, g.title())} {u}"
        if kind == "city":
            return "City"
        if kind == "position":
            return fake_position(text)
        if kind == "coord":
            return "0.0"
        if kind == "serial":
            return self.fake_like(text, k)
        if kind == "installation":
            return self.installation_id(text)
        if kind == "mac":
            return self.mac(text)
        return "ssid"

    def _record_value(self, g: str, u: str, k: str, value: Any) -> Any:
        kind = personal_kind(g, k)
        if kind is None:
            if isinstance(value, str):
                return value if _VERSION_KEY_RE.match(k) else self.text(value)
            return self.generic(value)
        if value is None or value == "" or is_redacted(value):
            return value
        if kind == "weather":
            return self._weather_value(value)
        if isinstance(value, (Mapping, list, tuple)):
            return _PSEUDONYMISED
        text = value if isinstance(value, str) else str(value)
        fake = self.personal_text(kind, g, u, k, text)
        if isinstance(value, (str, bool)):
            return fake
        if isinstance(value, int):
            try:
                return int(fake)
            except ValueError:
                return fake
        if isinstance(value, float):
            try:
                return float(fake)
            except ValueError:
                return fake
        return fake

    def records(self, rows: Any) -> Any:
        if not isinstance(rows, list):
            return self.generic(rows)
        return [self.record(r) if isinstance(r, Mapping) else self.generic(r) for r in rows]

    # -- dict payloads ----------------------------------------------------------

    def config(self, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return self.generic(data)
        out: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str) and not (isinstance(key, str) and _VERSION_KEY_RE.match(key)):
                if _HOSTNAME_RE.fullmatch(value):
                    out[key] = self.host(value)
                    continue
                if isinstance(key, str) and _METEO_LOCATION_RE.search(key) and value:
                    out[key] = "City"
                    continue
            out[key] = self._generic_value(key, value)
        return out

    def me(self, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return self.generic(data)
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key == "username" and isinstance(value, str):
                out[key] = self.user(value)
            elif key == "email" and isinstance(value, str) and value:
                out[key] = _FAKE_EMAIL
            elif key in _ME_NAME_FIELDS and isinstance(value, str) and value:
                out[key] = _ME_NAME_FIELDS[key]
            elif key == "id" and isinstance(value, (int, str)) and not isinstance(value, bool):
                out[key] = 1 if isinstance(value, int) else ("1" if value else value)
            else:
                out[key] = self.generic(value)
        return out

    def domotica(self, obj: Any, label: str = "Item", key: str | None = None) -> Any:
        """Pseudonymise a domotica payload (every user-chosen string is replaced)."""
        if isinstance(obj, Mapping):
            out: dict[str, Any] = {}
            ident = obj.get("id")
            suffix = f" {ident}" if isinstance(ident, (str, int)) and ident != "" else ""
            for field_name, value in obj.items():
                name = field_name if isinstance(field_name, str) else None
                if name in _DOMOTICA_VALUE_DICTS:
                    out[field_name] = self.generic(value)
                elif isinstance(value, str):
                    out[field_name] = self._domotica_text(name, value, label, suffix)
                else:
                    child = _DOMOTICA_LABELS.get(name, label) if name is not None else label
                    out[field_name] = self.domotica(value, child, name)
            return out
        if isinstance(obj, list):
            return [self.domotica(item, label, key) for item in obj]
        if isinstance(obj, str):
            return self._domotica_text(key, obj, label, "")
        return obj

    def _domotica_text(self, key: str | None, value: str, label: str, suffix: str) -> str:
        if not value or is_redacted(value):
            return value
        if key in _DOMOTICA_KEEP_KEYS:
            return self.text(value)
        if key in _DOMOTICA_ID_KEYS and value.isdigit():
            return value
        if key == "name":
            return f"{label}{suffix}"
        if key == "description":
            return f"{label} description{suffix}"
        level = _KNX_LEVEL_RE.fullmatch(key) if key is not None else None
        if level is not None:
            return f"{label} level{level.group(1)}{suffix}"
        return f"{label} {key}{suffix}" if key else label

    def ws_frame(self, frame: Any) -> Any:
        if not isinstance(frame, Mapping):
            return self.generic(frame)
        if "domotica" in frame and "domain" not in frame:
            out = dict(frame)
            out["domotica"] = self.domotica(frame["domotica"], "Object")
            return out
        if frame.get("domain") == "termo":
            path = frame.get("path")
            parts = split_path(path)
            out = dict(frame)
            if parts is not None:
                g, u, s, k = parts
                row = self.record(
                    {
                        "Gruppo": g,
                        "Unita": u,
                        "SubUni": s,
                        "Key": k,
                        "Valore": frame.get("value"),
                        "path": frame.get("path"),
                    }
                )
                if "value" in frame:
                    out["value"] = row["Valore"]
                out["path"] = row["path"]
            else:
                if isinstance(path, str):
                    # An unparseable UTEN path still carries a user name: drop it.
                    head = path.split(".", 1)[0]
                    out["path"] = "UTEN.<redacted>" if head == "UTEN" else self.text(path)
                if "value" in frame:
                    out["value"] = self.generic(frame["value"])
            return out
        return self.generic(frame)

    def jsonl_line(self, line: Any) -> Any:
        if isinstance(line, Mapping) and "frame" in line:
            out = dict(line)
            out["frame"] = self.ws_frame(line["frame"])
            return out
        return self.generic(line)

    def meta(self, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return self.generic(data)
        out = dict(self.generic(data))
        host = data.get("host")
        if isinstance(host, str) and host:
            out["host"] = self.host(host) if not _IPV4_RE.fullmatch(host) else self.ip(host)
        return out


# ---------------------------------------------------------------------------
# Whole-capture pass: collect the real personal values first
# ---------------------------------------------------------------------------


def _record_rows(content: Any) -> list[Any]:
    return content if isinstance(content, list) else []


def _iter_record_values(capture: Mapping[str, Any]) -> Iterator[tuple[str, str, str, Any]]:
    """``(Gruppo, Unita, Key, value)`` of every record and parseable WS termo frame."""
    for content in capture.values():
        for row in _record_rows(content):
            if not isinstance(row, Mapping):
                continue
            if "Gruppo" in row:
                yield (
                    norm(row.get("Gruppo")),
                    norm(row.get("Unita")),
                    norm(row.get("Key")),
                    row.get("Valore"),
                )
            frame = row.get("frame")
            if isinstance(frame, Mapping) and frame.get("domain") == "termo":
                parts = split_path(frame.get("path"))
                if parts is not None:
                    yield parts[0], parts[1], parts[3], frame.get("value")


def _collect_users(capture: Mapping[str, Any]) -> set[str]:
    names: set[str] = set()
    for name, content in capture.items():
        for row in _record_rows(content):
            if isinstance(row, Mapping) and norm(row.get("Gruppo")) == "UTEN":
                names.add(norm(row.get("Key")))
            frame = row.get("frame") if isinstance(row, Mapping) else None
            if isinstance(frame, Mapping):
                parts = split_path(frame.get("path"))
                if parts is not None and parts[0] == "UTEN":
                    names.add(parts[3])
        if name == "me.json" and isinstance(content, Mapping):
            username = content.get("username")
            if isinstance(username, str):
                names.add(username)
    return names


def _json_location_literals(value: Any, parent: str = "") -> Iterator[tuple[str, str]]:
    """Real place names and coordinates inside a weather payload, with their pseudonyms."""
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            value = json.loads(value)
        except ValueError:
            return
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = key.lower() if isinstance(key, str) else ""
            if name in _LAT_KEYS or name in _LON_KEYS:
                if isinstance(item, (int, float, str)) and not isinstance(item, bool):
                    fake = _FAKE_LAT if name in _LAT_KEYS else _FAKE_LON
                    yield str(item), str(_fake_coordinate(item, fake))
            elif isinstance(item, str) and (
                name in _PLACE_KEYS
                or name in _WEATHER_PLACE_KEYS
                or (parent in _PLACE_KEYS and name not in _COUNTRY_KEYS)
            ):
                yield item, "City"
            else:
                yield from _json_location_literals(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _json_location_literals(item, parent)


def _register_literals(pseudo: Pseudonymiser, capture: Mapping[str, Any]) -> None:
    """Register every real personal value so that free-text echoes are replaced too."""
    macs: set[str] = set()
    literals: dict[str, str] = {}

    def add(value: object, replacement: str) -> None:
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            if text and not is_redacted(text):
                literals.setdefault(text, replacement)

    for gruppo, unita, key, value in _iter_record_values(capture):
        if value is None or value == "" or isinstance(value, bool) or is_redacted(value):
            continue
        kind = personal_kind(gruppo, key)
        if kind is None:
            continue
        if kind == "weather":
            for real, fake in _json_location_literals(value):
                add(real, fake)
            continue
        if isinstance(value, (Mapping, list, tuple)):
            continue
        text = str(value)
        if kind == "mac":
            macs.add(text)
            continue
        if kind == "installation" and _BARE_MAC_RE.fullmatch(_MAC_SEP_RE.sub("", text)):
            macs.add(text)
            continue
        fake = pseudo.personal_text(kind, gruppo, unita, key, text)
        add(text, fake)
        if kind == "position":
            real_parts = _POSITION_SPLIT_RE.split(text.strip())
            fake_parts = _POSITION_SPLIT_RE.split(fake)
            if len(real_parts) == len(fake_parts) == 3:
                add(real_parts[0], fake_parts[0])
                add(real_parts[2], fake_parts[2])

    config = capture.get("config.json")
    if isinstance(config, Mapping):
        for key, value in config.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                continue
            if _SERIAL_KEY_RE.fullmatch(key):
                if key.upper().startswith("INSTALL") and _BARE_MAC_RE.fullmatch(
                    _MAC_SEP_RE.sub("", value)
                ):
                    macs.add(value)
                else:
                    add(value, str(pseudo.serial_value(key, value)))
            elif _MAC_KEY_RE.search(key):
                macs.add(value)
            elif _SSID_KEY_RE.search(key):
                add(value, "ssid")
            elif _METEO_LOCATION_RE.search(key) and any(ch.isalpha() for ch in value):
                add(value, "City")
            elif _HOSTNAME_RE.fullmatch(value) and not _VERSION_KEY_RE.match(key):
                add(value, pseudo.host(value))

    me = capture.get("me.json")
    if isinstance(me, Mapping):
        for key, fake in (("email", _FAKE_EMAIL), *_ME_NAME_FIELDS.items()):
            add(me.get(key), fake)

    for name, content in capture.items():
        if (name == "meta.json" or name.endswith("_meta.json")) and isinstance(content, Mapping):
            host = content.get("host")
            if isinstance(host, str) and "." in host and not _IPV4_RE.fullmatch(host):
                add(host, pseudo.host(host))

    for mac in sorted(macs):
        pseudo.register_mac(mac)
    for user in sorted(_collect_users(capture)):
        pseudo.register_literal(user, pseudo.user(user))
    for real in sorted(literals, key=lambda t: (-len(t), t)):
        pseudo.register_literal(real, literals[real])


def pseudonymise_fixture(capture: Mapping[str, Any], salt: str) -> dict[str, Any]:
    """Pseudonymise a whole capture ``{file name: parsed content}``.

    ``*.jsonl`` entries are lists of parsed lines.  Dispatch is by file name:
    record files (``interface*.json``, ``overrides.json``), ``config.json``,
    ``me.json``, ``domotica_<r>.json``, ``ws.jsonl``, ``meta.json``; every other
    file gets the generic replacement.  Real personal values found anywhere in
    the capture are registered first, so that their echoes in other files and
    free text are replaced consistently.
    """
    pseudo = Pseudonymiser(salt)
    pseudo.prepare_users(_collect_users(capture))
    _register_literals(pseudo, capture)
    out: dict[str, Any] = {}
    for name in sorted(capture):
        content = capture[name]
        if fnmatch.fnmatch(name, "interface*.json") or name == "overrides.json":
            out[name] = pseudo.records(content)
        elif name == "config.json":
            out[name] = pseudo.config(content)
        elif name == "me.json":
            out[name] = pseudo.me(content)
        elif name.startswith("domotica_") and name.endswith(".json"):
            resource = name[len("domotica_") : -len(".json")]
            out[name] = pseudo.domotica(content, _DOMOTICA_LABELS.get(resource, "Item"))
        elif name == "meta.json" or name.endswith("_meta.json"):
            out[name] = pseudo.meta(content)
        elif name.endswith(".jsonl") and isinstance(content, list):
            out[name] = [pseudo.jsonl_line(line) for line in content]
        else:
            out[name] = pseudo.generic(content)
    return out
