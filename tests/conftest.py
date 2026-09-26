"""Shared fixtures.  Tests are strictly offline.

* Every DNS lookup / TCP connect to anything other than loopback fails.
* The Keychain helper binary is pointed at a path that does not exist, so a
  test that forgot to mock credentials can never reach the real Keychain.
"""

from __future__ import annotations

import copy
import inspect
import socket
from typing import Any

import aiohttp
import aioresponses.core
import pytest


class _NullStreamWriter:
    output_size = 0


class _CompatClientResponse(aiohttp.ClientResponse):
    """aioresponses 0.7.x predates aiohttp 3.14's required ``stream_writer`` argument."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("stream_writer", _NullStreamWriter())
        super().__init__(*args, **kwargs)


if "stream_writer" in inspect.signature(aiohttp.ClientResponse.__init__).parameters:
    aioresponses.core.ClientResponse = _CompatClientResponse  # type: ignore[misc]


TEST_HOST = "rehom.test"
TEST_PORT = 8000
BASE = f"http://{TEST_HOST}:{TEST_PORT}"

_LOOPBACK = {"127.0.0.1", "localhost", "::1", None}


@pytest.fixture(autouse=True)
def _offline_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in _LOOPBACK:
            raise OSError(f"offline test guard: DNS lookup of {host!r} blocked")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if address[0] not in _LOOPBACK:
            raise OSError(f"offline test guard: connection to {address[0]!r} blocked")
        return real_create_connection(address, *args, **kwargs)

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else None
        if isinstance(address, tuple) and host not in _LOOPBACK:
            raise OSError(f"offline test guard: connection to {host!r} blocked")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        _check(address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aiorehom.credentials.SECURITY_BIN", "/nonexistent/aiorehom-test/security")


# ---------------------------------------------------------------------------
# Synthetic payloads with realistic shapes
# ---------------------------------------------------------------------------

SECRET_VALUES = (
    "s3cret-user-pw",
    "adm1n-pw",
    "wifi-pass-123",
    "pwdwifi-456",
    "remote-token-789",
    "meteo-key-in-interface",
    "0123456789abcdefMETEO",
    "tok-abcdef0123456789",
)


def rec(g: str, u: Any, s: Any, k: str, v: Any, **extra: Any) -> dict[str, Any]:
    unita = "" if u is None else str(u)
    subuni = "" if s is None else str(s)
    row = {
        "Gruppo": g,
        "Unita": u,
        "SubUni": s,
        "Key": k,
        "Valore": v,
        "Stato": 1,
        "Flusso": 0,
        "path": f"{g}.{unita}.{subuni}.{k}",
    }
    row.update(extra)
    return row


def _vector(ones: set[int], size: int) -> str:
    return ",".join("1" if i in ones else "0" for i in range(size))


INTERFACE: list[dict[str, Any]] = [
    rec("REHOM", "", "", "STAGIONE", "0"),
    rec("REHOM", "", "", "FORZATURA_STAGIONE", "0"),
    rec("REHOM", "", "", "MODO", "2"),
    rec("REHOM", "", "", "SET_POINT", "0"),
    rec("REHOM", "", "", "TEMP_OFF", "7.0"),
    rec("REHOM", "", "", "TEMP_MAN", "18.0"),
    rec("REHOM", "", "", "TEMP_PRE", "19.5"),
    rec("REHOM", "", "", "TEMP_COM", "21.0"),
    rec("REHOM", "", "", "ALG_ATTIVO", "0"),
    rec("REHOM", "", "", "TERMO_READONLY", "0"),
    rec("REHOM", "", "", "SERVER_ON", "1"),
    rec("REHOM", "", "", "LOCALITA", "Testville"),
    rec("REHOM", "", "", "VER_SOFT", "3.1.0.12"),
    rec("REHOM", "", "", "ABILITA_DOMOTICA", "0"),
    rec("REHOM", "", "", "PRESENZA_SONDE", _vector({1, 2}, 24)),
    rec("REHOM", "", "", "STATO_SONDE", _vector({1}, 24)),
    rec("REHOM", "", "", "PRESENZA_DEUM", "1,0,0"),
    rec("REHOM", "", "", "STATO_DEUM", "1,0,0"),
    rec("REHOM", "", "", "PRESENZA_AT9091", _vector(set(), 24)),
    rec("REHOM", "", "", "STATO_AT9091", _vector(set(), 24)),
    rec("REHOM", "", "", "ALLARME_BUS", "1"),
    rec("REHOM", "101", "", "PROCESS_STATE", ""),
    rec("REHOM", "", "", "WEBSERVER", "1"),
    rec("ZONA", "002", "", "NOME", "Soggiorno di Alice"),
    rec("ZONA", "002", "", "TEMP_AMBIENTE", "21.4"),
    rec("ZONA", "002", "", "UMIDITA", "48"),
    rec("ZONA", "002", "", "ATTIVA", "1"),
    rec("ZONA", "002", "", "SETP_CORRENTE", "0"),
    rec("ZONA", "002", "", "DELTA_SETP_CORRENTE", "0.0"),
    rec("ZONA", "002", "", "MATRICOLA", "A1234567"),
    rec("ZONA", "002", "", "CUSTOM_ID", "cust-99"),
    rec("ZONA", "002", "1", "ICO_IMG", "fan_on"),
    rec("ZONA", "002", "1", "ICO_VAL", "ON"),
    rec("ZONA", "003", "", "NOME", "Camera"),
    rec("ZONA", "003", "", "TEMP_AMBIENTE", "19.9"),
    rec("ZONA", "003", "", "UMIDITA", "255"),
    rec("ZONA", "003", "", "ATTIVA", "0"),
    rec("ZONA", "003", "", "SETP_CORRENTE", "4"),
    rec("ZONA", "000", "2", "ICO_IMG", "sun"),
    rec("PROG", "002", 0, "PROG_SETT_INVERNO", "1"),
    rec("PROG", "002", "1", "PROG_GIORNO_INVERNO", ",".join(["1"] * 48)),
    rec("DEUM", "001", "", "NOME", "VMC Casa"),
    rec("DEUM", "001", "", "ST_MODE", "1"),
    rec("DEUM", "001", "", "COM_VENTILA", "2"),
    rec("DEUM", "001", "", "ST_STATO_DEUM", "1"),
    rec("DEUM", "", "", "STEP", "0"),
    rec("METEO", "", "", "TEMPERATURE", "14.2,13.0"),
    rec("METEO", "", "", "ALLARM_METEO_KEY", "0"),
    rec("PROC", "", "", "WATCHDOG_ALARM", "0"),
    rec("CONFIG", "", "", "REMOTE_TOKEN", "remote-token-789"),
    rec("CONFIG", "", "", "METEO_KEY", "meteo-key-in-interface"),
    rec("WEBSERVER", "", "", "MacAddress", "B8:27:EB:12:34:56"),
    rec("WEBSERVER", "", "", "PwdWiFi", "pwdwifi-456"),
    rec("WIFI", "", "", "SSID", "TestNet"),
    rec("WIFI", "", "", "PASSWORD", "wifi-pass-123"),
    rec("ETH", "", "", "IP", "198.51.100.8"),
    rec("ETH", "", "", "SUBNET", "255.255.255.0"),
    rec("UTEN", "", "", "alice", "s3cret-user-pw"),
    rec("UTEN", "", "", "admin", "adm1n-pw"),
]

OVERRIDES: list[dict[str, Any]] = [
    {
        "Gruppo": "PROG_OVERRIDE",
        "Unita": "002",
        "SubUni": "1",
        "Key": "PROG_GIORNO_INVERNO",
        "Valore": ",".join(["3"] * 48),
        "Impostazione": "2026-09-25 09:00:00",
        "Scadenza": "2026-09-25 11:00:00",
    }
]

CONFIG: dict[str, Any] = {
    "LOCAL_TIME": "2026-09-25 10:15:30",
    "TIMEZONE": "Europe/Rome",
    "METEO_KEY": "0123456789abcdefMETEO",
    "VERSION": "1.4.2",
    "RASPBERRY_ETH0_IP": "198.51.100.8",
    "RASPBERRY_WLAN1_IP": "",
    "RASPBERRY_IP": "dhcp",
    "RASPBERRY_SUBNET": "auto",
    "RASPBERRY_GATEWAY": "198.51.100.1",
    "__DEUM_HELP": ["D1_1.png", "D1_f.png"],
}

PLANT_CONF: dict[str, Any] = {
    "user_message": "",
    "stagione": "0",
    "CONFIGURA_ON": "0",
    "temperatura_comfort_inverno": "21.0",
    "rele_collector1_config3": "1",
    "brand_new_key": "x",
}

ALIVE: dict[str, Any] = {"version": "1.4.2"}
ME: dict[str, Any] = {"username": "alice", "email": "alice@example.org", "first_name": "Alice"}
DOMOTICA: dict[str, Any] = {
    "object": [],
    "rooms": [],
    "scenarios": [],
    "config": {
        "power_detatch_algorithm": "rehom",
        "power_consumption_auto_detach": "0",
        "power_consumption_limit": "3.0",
    },
}
HISTORY: list[dict[str, Any]] = [
    {"Tempo": "2026-09-25 04:30:00", "Valore": 1.5},
    {"Tempo": "2026-09-25 05:00:00", "Valore": 2.0},
]


@pytest.fixture
def interface_rows() -> list[dict[str, Any]]:
    return copy.deepcopy(INTERFACE)


@pytest.fixture
def mini_catalog() -> dict[str, Any]:
    return {
        "interface_records": [
            {"gruppo": "REHOM", "key": "STAGIONE"},
            {"gruppo": "REHOM", "key": "NEVER_THERE"},
            {"gruppo": "UTEN", "key": "{username}"},
            {"gruppo": "{any}", "key": "WEBSERVER"},
            {"gruppo": "PROG", "key": "PROG_GIORNO_{season:INVERNO|ESTATE}"},
        ],
        "plant_conf_keys": [
            {"key": "stagione"},
            {"key": "CONF_ZONE_ON"},
            {"key": "rele_collector1_config{relay:1-10}"},
        ],
        "api_config_fields": [{"key": "LOCAL_TIME"}, {"key": "MISSING_FIELD"}],
    }
