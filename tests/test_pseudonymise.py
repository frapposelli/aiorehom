"""Fixture pseudonymisation (deterministic given a salt)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from aiorehom import cli
from aiorehom.pseudonymise import Pseudonymiser, is_personal_field, personal_kind
from aiorehom.redact import pseudonymise_fixture, redact_records

from .conftest import CONFIG, INTERFACE, ME, rec


def _capture() -> dict[str, Any]:
    interface, _ = redact_records(INTERFACE)
    return {
        "interface.json": interface,
        "overrides.json": [],
        "interface_filtered_zona_002.json": [r for r in interface if r["Unita"] == "002"],
        "config.json": {
            **CONFIG,
            "METEO_KEY": "<redacted len=21>",
            "SERVER": "rehom.lan",
            "LOCALITA": "Testville",
            "__DEUM_HELP": ["D1_1.png"],
        },
        "me.json": dict(ME, last_name="Rossi"),
        "domotica_rooms.json": [{"id": 3, "name": "Cucina di Alice", "objects_ids": [1]}],
        "domotica_object.json": [
            {
                "id": 1,
                "name": "Luce",
                "room_data": {"id": 3, "name": "Cucina di Alice"},
                "members": [{"description": "Relay casa", "knx_address": "1/1/1"}],
            }
        ],
        "ws.jsonl": [
            {
                "t": "x",
                "mono": 1,
                "frame": {
                    "domain": "termo",
                    "type": "update",
                    "path": "UTEN...alice",
                    "value": "<redacted len=3>",
                },
            },
            {
                "t": "x",
                "mono": 2,
                "frame": {
                    "domain": "termo",
                    "type": "update",
                    "path": "ZONA.002..NOME",
                    "value": "Soggiorno di Alice",
                },
            },
            {
                "t": "x",
                "mono": 3,
                "frame": {
                    "domain": "termo",
                    "type": "update",
                    "path": "ETH..IP",
                    "value": "198.51.100.8",
                },
            },
            {
                "t": "x",
                "mono": 4,
                "frame": {"domain": "bus", "type": "update", "key": "ip", "value": "10.0.0.7"},
            },
            {"t": "x", "mono": 5, "frame": {"domotica": {"objects": [{"id": 1, "name": "Luce"}]}}},
            {"t": "x", "mono": 6, "event": {"event": "connect"}},
        ],
        "meta.json": {"host": "rehomserver.local", "port": 8000},
        "watch_meta.json": {"host": "198.51.100.8"},
        "history_TEMP_AMBIENTE_002.json": [{"Tempo": "2026-09-25 05:00:00", "Valore": 20.5}],
        "requests.jsonl": [{"method": "GET", "path": "/api/alive/"}],
    }


def test_pseudonymise_capture() -> None:
    capture = _capture()
    original = copy.deepcopy(capture)
    out = pseudonymise_fixture(capture, "salt-1")
    assert capture == original
    text = json.dumps(out)
    for personal in (
        "Soggiorno di Alice",
        "Camera",
        "Testville",
        "A1234567",
        "cust-99",
        "B8:27:EB:12:34:56",
        "TestNet",
        "alice",
        "Alice",
        "Rossi",
        "198.51.100.8",
        "198.51.100.1",
        "10.0.0.7",
        "rehomserver.local",
        "rehom.lan",
        "Cucina",
        "Luce",
        "Relay casa",
        "VMC Casa",
    ):
        assert personal not in text, personal
    rows = {(r["Gruppo"], r["Unita"], r["Key"]): r for r in out["interface.json"]}
    assert rows[("ZONA", "002", "NOME")]["Valore"] == "Zona 002"
    assert rows[("ZONA", "003", "NOME")]["Valore"] == "Zona 003"
    assert rows[("DEUM", "001", "NOME")]["Valore"] == "VMC 001"
    assert rows[("REHOM", "", "LOCALITA")]["Valore"] == "City"
    assert rows[("WIFI", "", "SSID")]["Valore"] == "ssid"
    mac = rows[("WEBSERVER", "", "MacAddress")]["Valore"]
    assert re.fullmatch(r"02:00:00:[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}", mac)
    matricola = rows[("ZONA", "002", "MATRICOLA")]["Valore"]
    assert re.fullmatch(r"[A-Z]\d{7}", matricola) and matricola != "A1234567"
    assert re.fullmatch(r"[a-z]{4}-\d{2}", rows[("ZONA", "002", "CUSTOM_ID")]["Valore"])
    assert rows[("ETH", "", "IP")]["Valore"].startswith("192.0.2.")
    assert rows[("ETH", "", "SUBNET")]["Valore"] == "255.255.255.0"
    assert rows[("REHOM", "", "VER_SOFT")]["Valore"] == "3.1.0.12"
    assert ("UTEN", "", "user1") in rows
    assert rows[("UTEN", "", "user1")]["path"] == "UTEN...user1"
    assert ("UTEN", "", "admin") in rows
    assert rows[("UTEN", "", "admin")]["Valore"].startswith("<redacted")
    # consistency across files
    assert out["me.json"]["username"] == "user1"
    assert out["me.json"]["email"] == "user@example.invalid"
    assert out["me.json"]["first_name"] == "Name"
    assert out["me.json"]["last_name"] == "Surname"
    ws = out["ws.jsonl"]
    assert ws[0]["frame"]["path"] == "UTEN...user1"
    assert ws[1]["frame"]["value"] == "Zona 002"
    assert ws[2]["frame"]["value"] == rows[("ETH", "", "IP")]["Valore"]
    assert ws[3]["frame"]["value"].startswith("192.0.2.")
    assert ws[4]["frame"]["domotica"]["objects"][0]["name"] == "Object 1"
    assert ws[5] == capture["ws.jsonl"][5]
    assert out["config.json"]["RASPBERRY_ETH0_IP"] == rows[("ETH", "", "IP")]["Valore"]
    assert out["config.json"]["SERVER"].endswith(".example")
    assert out["config.json"]["LOCALITA"] == "City"
    assert out["config.json"]["__DEUM_HELP"] == ["D1_1.png"]
    assert out["config.json"]["VERSION"] == "1.4.2"
    assert out["domotica_rooms.json"][0]["name"] == "Room 3"
    obj = out["domotica_object.json"][0]
    assert obj["name"] == "Object 1"
    assert obj["room_data"]["name"] == "Room 3"
    assert obj["members"][0]["description"] == "Member description"
    assert obj["members"][0]["knx_address"] == "1/1/1"
    assert out["meta.json"]["host"].endswith(".example")
    assert out["watch_meta.json"]["host"].startswith("192.0.2.")
    assert out["history_TEMP_AMBIENTE_002.json"] == capture["history_TEMP_AMBIENTE_002.json"]
    # deterministic for a salt, different across salts
    assert pseudonymise_fixture(_capture(), "salt-1") == out
    other = pseudonymise_fixture(_capture(), "salt-2")
    other_rows = {(r["Gruppo"], r["Unita"], r["Key"]): r for r in other["interface.json"]}
    assert other_rows[("ZONA", "002", "MATRICOLA")]["Valore"] != matricola


def test_primitives() -> None:
    p = Pseudonymiser("s")
    with pytest.raises(ValueError, match="salt"):
        Pseudonymiser("")
    assert p.mac("b8-27-eb-12-34-56").startswith("02-00-00-")
    assert p.mac("b827eb123456").startswith("020000")
    assert len(p.mac("b827eb123456")) == 12
    assert p.mac("weird") != "weird"
    assert p.text_ips("a 999.1.1.1 b") == "a 999.1.1.1 b"
    assert p.text_ips("127.0.0.1 0.0.0.0 192.0.2.4") == "127.0.0.1 0.0.0.0 192.0.2.4"
    first = p.ip("10.0.0.1")
    assert p.ip("10.0.0.1") == first
    fakes = {p.ip(f"10.0.1.{i}") for i in range(1, 250)}
    assert len(fakes) == 249  # collision-free while space remains
    assert p.ip("10.0.2.1").startswith("192.0.2.")  # space exhausted: still a fake address
    assert p.user("admin") == "admin"
    assert p.user("") == ""
    assert p.user("zed") == "user1"
    assert p.host("Rehom.LAN") == p.host("Rehom.LAN")
    assert p.fake_like("", "x") == ""
    assert p.records({"not": "a list"}) == {"not": "a list"}
    assert p.config(["x"]) == ["x"]
    assert p.me("x") == "x"
    assert p.ws_frame("x") == "x"
    assert p.meta(["10.1.1.1"])[0].startswith("192.0.2.")
    frame = {"domain": "termo", "path": "odd.path", "value": "10.1.1.1"}
    assert p.ws_frame(frame)["value"].startswith("192.0.2.")
    assert p.record({"Gruppo": "METEO", "Key": "CITY", "Valore": "Rome"})["Valore"] == "City"
    assert p.record({"Gruppo": "METEO", "Key": "LAT", "Valore": "42.4"})["Valore"] == "0.0"
    assert p.record({"Gruppo": "ZONA", "Key": "NOME", "Valore": 5})["Valore"] == "Zona "
    assert p.record({"Gruppo": "ZONA", "Key": "NOME", "Valore": None})["Valore"] is None
    assert p.record({"Gruppo": "ZONA", "Key": "NOME", "Valore": ""})["Valore"] == ""
    assert p.record({"Gruppo": "X", "Key": "Y", "Valore": "1", "extra": "10.9.9.9"})[
        "extra"
    ].startswith("192.0.2.")


# ---------------------------------------------------------------------------
# Regressions
# ---------------------------------------------------------------------------


def _knx_object() -> list[dict[str, Any]]:
    """Domotica payload shape with user-chosen ETS labels."""
    return [
        {
            "id": 7,
            "name": "Luce camera Mario",
            "kind": "on-off",
            "subkind": "light",
            "icon": "lamp",
            "room": 3,
            "zone": "002",
            "room_data": {"id": 3, "name": "Camera Mario", "icon": "bed"},
            "members": [
                {
                    "tag": "command",
                    "knx_object": "Canale A Mario",
                    "knx_address": "1/2/3",
                    "knx_level0": "Casa Rossi",
                    "knx_level1": "Camera Mario",
                    "knx_level2": "Luce camera Mario - Cmd",
                    "ets_type": "DPT_Switch",
                    "description": "Attuatore camera Mario",
                    "label": "Mario",
                }
            ],
            "state": {"command": "1"},
            "stable_state": {"state": "1"},
        }
    ]


def test_domotica_knx_levels_and_free_text_are_pseudonymised() -> None:
    """Regression: members[].knx_level0..2 used to be copied into fixtures unchanged."""
    obj = _knx_object()
    out = pseudonymise_fixture(
        {
            "domotica_object.json": obj,
            "ws.jsonl": [{"frame": {"domotica": {"full_objects": obj}}}],
        },
        "salt",
    )
    text = json.dumps(out)
    for personal in ("Mario", "Rossi", "Casa", "Camera", "Luce", "Canale", "Attuatore"):
        assert personal not in text, personal
    for fixture in (
        out["domotica_object.json"][0],
        out["ws.jsonl"][0]["frame"]["domotica"]["full_objects"][0],
    ):
        member = fixture["members"][0]
        assert member["knx_level0"] == "Member level0"
        assert member["knx_level1"] == "Member level1"
        assert member["knx_level2"] == "Member level2"
        assert member["description"] == "Member description"
        assert member["label"] == "Member label"
        assert member["knx_object"] == "Member knx_object"
        # structural fields are kept
        assert (member["tag"], member["knx_address"], member["ets_type"]) == (
            "command",
            "1/2/3",
            "DPT_Switch",
        )
        assert fixture["name"] == "Object 7"
        assert (fixture["kind"], fixture["subkind"], fixture["icon"]) == ("on-off", "light", "lamp")
        assert (fixture["room"], fixture["zone"]) == (3, "002")
        assert fixture["room_data"] == {"id": 3, "name": "Room 3", "icon": "bed"}
        assert fixture["state"] == {"command": "1"}
        assert fixture["stable_state"] == {"state": "1"}


def test_dotted_uten_username_is_pseudonymised_everywhere(tmp_path: Path) -> None:
    """Regression: 'mario.rossi' in a WS path gave 5 parts and was left in ws.jsonl."""
    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "interface.json").write_text(
        json.dumps([rec("UTEN", "", "", "mario.rossi@example.it", "pw")])
    )
    frame = {
        "domain": "termo",
        "type": "update",
        "gruppo": "UTEN",
        "path": "UTEN...mario.rossi@example.it",
        "value": "newpw",
    }
    odd = {"domain": "termo", "type": "update", "gruppo": "UTEN", "path": "UTEN.bob", "value": "x"}
    (capture / "ws.jsonl").write_text(
        json.dumps({"t": "x", "mono": 1, "frame": frame})
        + "\n"
        + json.dumps({"t": "x", "mono": 2, "frame": odd})
        + "\n"
    )
    out = tmp_path / "fx"
    assert cli.main(["sanitize-fixtures", str(capture), str(out), "--salt", "s"]) == 0
    interface = json.loads((out / "interface.json").read_text())
    ws = [json.loads(line) for line in (out / "ws.jsonl").read_text().splitlines()]
    assert interface[0]["Key"] == "user1"
    assert interface[0]["path"] == "UTEN...user1"
    assert ws[0]["frame"]["path"] == "UTEN...user1"
    assert ws[0]["frame"]["value"] == "<redacted len=5>"
    assert ws[1]["frame"]["path"] == "UTEN.<redacted>"
    text = (out / "ws.jsonl").read_text() + (out / "interface.json").read_text()
    assert "mario" not in text and "bob" not in text


def test_numeric_personal_values_are_pseudonymised() -> None:
    """Regression: a numeric MATRICOLA/CUSTOM_ID used to reach the fixture unchanged."""
    capture = {
        "interface.json": [
            rec("ZONA", "001", "", "MATRICOLA", 20231234),
            rec("DEUM", "001", "", "CUSTOM_ID", 998877),
            rec("ZONA", "001", "", "NOME", 42),
            rec("METEO", "", "", "LAT", 42.46),
            rec("REHOM", "", "", "LOCALITA", ["Somewhere"]),
            rec("ZONA", "001", "", "TEMP_AMBIENTE", 21.5),
        ],
        "ws.jsonl": [
            {"frame": {"domain": "termo", "path": "ZONA.002..MATRICOLA", "value": 20231234}},
        ],
    }
    out = pseudonymise_fixture(capture, "salt")
    rows = {(r["Gruppo"], r["Key"]): r["Valore"] for r in out["interface.json"]}
    assert isinstance(rows[("ZONA", "MATRICOLA")], int)
    assert rows[("ZONA", "MATRICOLA")] != 20231234
    assert isinstance(rows[("DEUM", "CUSTOM_ID")], int)
    assert rows[("DEUM", "CUSTOM_ID")] != 998877
    assert rows[("ZONA", "NOME")] == "Zona 001"
    assert rows[("METEO", "LAT")] == 0.0
    assert rows[("REHOM", "LOCALITA")] == "<pseudonymised>"
    assert rows[("ZONA", "TEMP_AMBIENTE")] == 21.5
    ws_value = out["ws.jsonl"][0]["frame"]["value"]
    assert isinstance(ws_value, int) and ws_value != 20231234
    text = json.dumps(out)
    for leaked in ("20231234", "998877", "Somewhere", "42.46"):
        assert leaked not in text, leaked


def test_mac_and_ssid_replaced_in_every_file(tmp_path: Path) -> None:
    """Regression: the MAC/SSID were pseudonymised only in their own interface record."""
    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "interface.json").write_text(
        json.dumps(
            [
                rec("WEBSERVER", "", "", "MacAddress", "B8:27:EB:12:34:56"),
                rec("WIFI", "", "", "SSID", "TestNet"),
            ]
        )
    )
    (capture / "alive.json").write_text(
        json.dumps({"version": "1.4.2", "mac": "B8:27:EB:12:34:56"})
    )
    (capture / "config.json").write_text(
        json.dumps(
            {
                "NOTE": "eth0 b8:27:eb:12:34:56 on testnet",
                "DEVICE_ID": "b827eb123456",
                "RASPBERRY_WLAN_SSID": "Other-SSID",
                "OTHER_MAC": "dc-a6-32-00-11-22",
            }
        )
    )
    (capture / "ws.jsonl").write_text(
        json.dumps({"frame": {"domain": "bus", "key": "k", "value": "B8-27-EB-12-34-56"}}) + "\n"
    )
    out = tmp_path / "fx"
    assert cli.main(["sanitize-fixtures", str(capture), str(out), "--salt", "s"]) == 0
    text = "\n".join(p.read_text() for p in sorted(out.iterdir())).lower()
    for leaked in ("b8:27:eb:12:34:56", "b8-27-eb-12-34-56", "b827eb123456", "testnet"):
        assert leaked not in text, leaked
    for leaked in ("other-ssid", "dc-a6-32-00-11-22"):
        assert leaked not in text, leaked
    interface = json.loads((out / "interface.json").read_text())
    fake_mac = next(r["Valore"] for r in interface if r["Key"] == "MacAddress")
    alive = json.loads((out / "alive.json").read_text())
    assert alive["mac"] == fake_mac  # same fake everywhere
    assert alive["version"] == "1.4.2"
    config = json.loads((out / "config.json").read_text())
    assert config["NOTE"] == f"eth0 {fake_mac.lower()} on ssid"
    assert config["DEVICE_ID"] == fake_mac.replace(":", "").lower()
    assert config["RASPBERRY_WLAN_SSID"] == "ssid"
    ws = json.loads((out / "ws.jsonl").read_text())
    assert ws["frame"]["value"] == fake_mac.replace(":", "-")


def test_pseudonymiser_idempotent_on_own_fakes() -> None:
    p = Pseudonymiser("s")
    fake = p.mac("B8:27:EB:12:34:56")
    assert p.text(f"x {fake} y") == f"x {fake} y"
    assert p.text("192.0.2.7") == "192.0.2.7"
    p.register_ssid("ab")  # too short to replace safely
    assert p.text("ab abc") == "ab abc"


def test_personal_field_predicates() -> None:
    assert personal_kind("ZONA", "NOME") == "name"
    assert personal_kind("METEO", "CITY") == "city"
    assert personal_kind("WIFI", "SSID") == "ssid"
    assert personal_kind("ZONA", "TEMP_AMBIENTE") is None
    assert is_personal_field("CONFIG_API", "RASPBERRY_MAC")
    assert is_personal_field("CONFIG_API", "LOCALITA")
    assert not is_personal_field("REHOM", "MODO")


# ---------------------------------------------------------------------------
# Regressions from the first live capture (all values below are synthetic)
# ---------------------------------------------------------------------------

_POSITION = "12.3456789,-65.4321098"
_OWM_ITEM: dict[str, Any] = {
    "dt": 1790337600,
    "main": {"temp": 22.5, "feels_like": 22.1, "pressure": 1015, "humidity": 55},
    "weather": [{"id": 800, "main": "Clear", "description": "clear sky", "icon": "01d"}],
    "clouds": {"all": 0},
    "wind": {"speed": 1.5, "deg": 120, "gust": 2.1},
    "visibility": 10000,
    "pop": 0,
    "sys": {"pod": "d"},
    "dt_txt": "2026-09-26 12:00:00",
}
_OWM_CITY: dict[str, Any] = {
    "id": 3165524,
    "name": "Testville",
    "coord": {"lat": 12.3456789, "lon": -65.4321098},
    "country": "ZZ",
    "population": 99887,
    "timezone": 7200,
    "sunrise": 1790310000,
    "sunset": 1790353000,
    "local_names": {"en": "Testville", "it": "Testvilla"},
}


def _meteo_frame(key: str, value: Any, mono: float = 1.0) -> dict[str, Any]:
    return {
        "t": "2026-09-25T10:00:00.000Z",
        "mono": mono,
        "frame": {
            "domain": "termo",
            "type": "update",
            "value": value,
            "gruppo": key.split(".", 1)[0],
            "path": key,
        },
    }


def test_meteo_position_and_weather_json_are_pseudonymised() -> None:
    """Regression: METEO.POSIZIONE (GPS) and METEO_DATA JSON reached the fixtures."""
    compact_item = json.dumps(_OWM_ITEM, separators=(",", ":"))
    full = json.dumps({"cod": "200", "list": [_OWM_ITEM], "city": _OWM_CITY})
    capture = {
        "interface.json": [
            rec("METEO", "", "", "POSIZIONE", _POSITION),
            rec("METEO", "0", "", "VENTO", "1.2345678901234567,123.45"),
            rec("METEO_DATA", "", "", "dt1790337600", compact_item),
            rec("METEO_DATA", "", "", "forecast", full),
            rec("METEO_DATA", "", "", "broken", '{"name": "Testville", "coord'),
            rec("METEO_DATA", "", "", "empty", ""),
            rec("REHOM", "", "", "LOCALITA", "Testville"),
        ],
        "ws.jsonl": [
            _meteo_frame("METEO...POSIZIONE", _POSITION),
            _meteo_frame("METEO_DATA...dt1790337600", compact_item, 2.0),
            _meteo_frame("METEO_DATA...city", _OWM_CITY, 3.0),
        ],
    }
    out = pseudonymise_fixture(capture, "salt")
    text = json.dumps(out)
    for leaked in ("12.3456789", "65.4321098", "Testville", "Testvilla", "3165524", "99887"):
        assert leaked not in text, leaked
    for leaked in ("1790310000", "1790353000", '"ZZ"'):
        assert leaked not in text, leaked
    rows = {(r["Gruppo"], r["Key"]): r["Valore"] for r in out["interface.json"]}
    assert rows[("METEO", "POSIZIONE")] == "45.0000000,10.0000000"
    assert rows[("METEO", "VENTO")] == "1.2345678901234567,123.45"  # weather data kept
    assert rows[("REHOM", "LOCALITA")] == "City"
    # a forecast item without location data is kept byte for byte
    assert rows[("METEO_DATA", "dt1790337600")] == compact_item
    forecast = json.loads(rows[("METEO_DATA", "forecast")])
    assert forecast["list"] == [_OWM_ITEM]  # numeric weather fields and condition codes kept
    assert forecast["cod"] == "200"
    assert forecast["city"] == {
        "id": 0,
        "name": "City",
        "coord": {"lat": 45.0, "lon": 10.0},
        "country": "XX",
        "population": 0,
        "timezone": 0,
        "sunrise": 0,
        "sunset": 0,
        "local_names": {"en": "City", "it": "City"},
    }
    assert ", " in rows[("METEO_DATA", "forecast")]  # original (default) JSON style kept
    assert rows[("METEO_DATA", "broken")] == "<pseudonymised>"
    assert rows[("METEO_DATA", "empty")] == ""
    ws = out["ws.jsonl"]
    assert ws[0]["frame"]["value"] == "45.0000000,10.0000000"
    assert ws[0]["frame"]["path"] == "METEO...POSIZIONE"
    assert ws[1]["frame"]["value"] == compact_item
    assert ws[2]["frame"]["value"]["name"] == "City"
    assert ws[2]["frame"]["value"]["coord"] == {"lat": 45.0, "lon": 10.0}


def test_json_string_values_lose_location_data_everywhere() -> None:
    p = Pseudonymiser("s")
    value = json.dumps({"coord": {"lat": 1.2345, "lon": 2.3456}, "name": "Pump", "ip": "10.1.2.3"})
    scrubbed = json.loads(p.text(value))
    assert scrubbed["coord"] == {"lat": 45.0, "lon": 10.0}
    assert scrubbed["name"] == "Pump"  # only weather payloads lose every "name"
    assert scrubbed["ip"].startswith("192.0.2.")
    assert p.text("[1,2,3]") == "[1,2,3]"
    assert p.text("[not json 10.1.2.3").startswith("[not json 192.0.2.")
    assert p.text(' {"city": "Testville"}\n') == ' {"city": "City"}\n'
    position = p.scrub_json({"geo": [12.34567, -1.5], "gps": "1.23456;2.34567"}, weather=False)
    assert position == {"geo": [45.0, 10.0], "gps": "45.00000;10.00000"}
    assert p.scrub_json({"position": None, "coords": 7}, weather=False) == {
        "position": None,
        "coords": 0,
    }


def test_installation_id_me_id_and_config_keys(tmp_path: Path) -> None:
    """Regression: /config/ INSTALLATION_ID (the bare MAC) and /me/ id were kept."""
    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "interface.json").write_text(
        json.dumps([rec("WEBSERVER", "", "", "MacAddress", "B8:27:EB:12:34:56")])
    )
    (capture / "config.json").write_text(
        json.dumps(
            {
                "INSTALLATION_ID": "b827eb123456",
                "TIMEZONE": "Europe/Rome",
                "RASPBERRY_ETH0_IP": "198.51.100.8",
                "RASPBERRY_IP": "dhcp",
                "ADMIN_EMAIL": "mario.rossi@example.org",
                "LATITUDE": "12.3456789",
                "LONGITUDE": -65.4321098,
                "SERIAL": 20231234,
            }
        )
    )
    (capture / "me.json").write_text(
        json.dumps(
            {
                "id": 42,
                "username": "mario.rossi",
                "email": "mario.rossi@example.org",
                "first_name": "Mario",
                "last_name": "Rossi",
                "date_joined": "2026-01-01T00:00:00",
            }
        )
    )
    out = tmp_path / "fx"
    assert cli.main(["sanitize-fixtures", str(capture), str(out), "--salt", "s"]) == 0
    config = json.loads((out / "config.json").read_text())
    interface = json.loads((out / "interface.json").read_text())
    fake_mac = interface[0]["Valore"]
    assert config["INSTALLATION_ID"] == fake_mac.replace(":", "").lower()
    assert config["TIMEZONE"] == "Europe/Rome"
    assert config["RASPBERRY_ETH0_IP"].startswith("192.0.2.")
    assert config["RASPBERRY_IP"] == "dhcp"
    assert config["ADMIN_EMAIL"] == "user@example.invalid"
    assert config["LATITUDE"] == "45.0000000"
    assert config["LONGITUDE"] == 10.0
    assert isinstance(config["SERIAL"], int) and config["SERIAL"] != 20231234
    me = json.loads((out / "me.json").read_text())
    assert me == {
        "id": 1,
        "username": "user1",
        "email": "user@example.invalid",
        "first_name": "Name",
        "last_name": "Surname",
        "date_joined": "2026-01-01T00:00:00",
    }
    p = Pseudonymiser("s")
    assert re.fullmatch(r"[a-z]\d{3}[a-z]{3}\d[a-z]{2}\d{2}", p.installation_id("x123abc4de56"))
    assert p.me({"id": "7"})["id"] == "1"
    assert p.me({"id": True})["id"] is True


def test_free_text_echoes_of_personal_values_are_replaced(tmp_path: Path) -> None:
    """Every real personal value found in the capture is replaced wherever it is echoed."""
    capture = tmp_path / "cap"
    capture.mkdir()
    (capture / "interface.json").write_text(
        json.dumps(
            [
                rec("ZONA", "001", "", "NOME", "Soggiorno di Alice"),
                rec("DEUM", "001", "", "NOME", "VMC Mansarda"),
                rec("ATTUATORE", "002", "", "NOME", "Collettore Nord"),
                rec("REHOM", "", "", "LOCALITA", "Testville"),
                rec("METEO", "", "", "POSIZIONE", _POSITION),
                rec("WIFI", "", "", "SSID", "CasaRossi-5G"),
                rec("ZONA", "001", "", "MATRICOLA", "AB123456"),
                rec("UTEN", "", "", "mario.rossi", "pw"),
                rec("UTEN", "", "", "admin", "pw"),
                rec(
                    "REHOM",
                    "14",
                    "",
                    "PROCESS_STATE",
                    "Soggiorno di Alice ok in testville (12.3456789) user mario.rossi"
                    " host rehomserver.local serial ab123456 wifi casarossi-5g",
                ),
            ]
        )
    )
    (capture / "me.json").write_text(
        json.dumps({"username": "mario.rossi", "email": "m.r@example.org", "first_name": "Mario"})
    )
    (capture / "meta.json").write_text(
        json.dumps(
            {
                "host": "rehomserver.local",
                "detail": ["Mario logged in from rehomserver.local", "mail m.r@example.org"],
            }
        )
    )
    (capture / "requests.jsonl").write_text(
        json.dumps(
            {
                "method": "GET",
                "path": "/api/users/mario.rossi/",
                "query": {"Gruppo": "UTEN", "Key": "mario.rossi"},
            }
        )
        + "\n"
    )
    (capture / "ws.jsonl").write_text(
        json.dumps(_meteo_frame("REHOM.14..PROCESS_STATE", "Collettore Nord: VMC Mansarda on"))
        + "\n"
    )
    out = tmp_path / "fx"
    assert cli.main(["sanitize-fixtures", str(capture), str(out), "--salt", "s"]) == 0
    interface = json.loads((out / "interface.json").read_text())
    state = next(r["Valore"] for r in interface if r["Key"] == "PROCESS_STATE")
    assert state.startswith("Zona 001 ok in City (45.0000000) user user1 host host-")
    assert state.endswith(
        ".example serial "
        + next(r["Valore"] for r in interface if r["Key"] == "MATRICOLA")
        + " wifi ssid"
    )
    names = {(r["Gruppo"], r["Unita"]): r["Valore"] for r in interface if r["Key"] == "NOME"}
    assert names[("ATTUATORE", "002")] == "Attuatore 002"
    ws = json.loads((out / "ws.jsonl").read_text())
    assert ws["frame"]["value"] == "Attuatore 002: VMC 001 on"
    requests = json.loads((out / "requests.jsonl").read_text())
    assert requests["path"] == "/api/users/user1/"
    assert requests["query"] == {"Gruppo": "UTEN", "Key": "user1"}
    meta = json.loads((out / "meta.json").read_text())
    assert meta["detail"] == [f"Name logged in from {meta['host']}", "mail user@example.invalid"]
    blob = "\n".join(p.read_text() for p in sorted(out.iterdir()))
    normalised = re.sub(r"[\s_\-:.,/;]", "", blob.lower())
    for leaked in (
        "Soggiorno di Alice",
        "Mansarda",
        "Collettore",
        "Testville",
        "12.3456789",
        "65.4321098",
        "CasaRossi-5G",
        "AB123456",
        "mario.rossi",
        "m.r@example.org",
        "Mario",
        "rehomserver",
    ):
        assert leaked.lower() not in blob.lower(), leaked
        assert re.sub(r"[\s_\-:.,/;]", "", leaked.lower()) not in normalised, leaked


def test_ipv6_email_and_lan_hosts_in_free_text() -> None:
    p = Pseudonymiser("s")
    out = p.text("eth0 fe80::ba27:ebff:fe12:3456%eth0 up")
    assert out.startswith("eth0 2001:db8::") and out.endswith("%eth0 up")
    assert "ba27" not in out
    full = "2001:0db9:0000:0000:0000:ff00:0042:8329"
    assert p.text(full).startswith("2001:db8::")
    assert p.text(full) == p.text("2001:db9::ff00:42:8329")  # same address, same fake
    for kept in (
        "::1",
        "::",
        "10:41:06",
        "2026-09-25T10:41:06Z",
        "2001:db8::5",
        "1:2:3:4:5",
        "::ffff:127.0.0.1",
        "zz::1",
    ):
        assert p.text(kept) == kept, kept
    assert p.text("contact a.b+c@mail.example.org now") == "contact user@example.invalid now"
    assert p.text("user@example.invalid") == "user@example.invalid"
    assert p.text("at rehom-01.lan:8000/x").startswith("at host-")
    assert p.text("api.openweathermap.org") == "api.openweathermap.org"
    assert p.text("Europe/Rome") == "Europe/Rome"


def test_literal_registration_guards() -> None:
    p = Pseudonymiser("s")
    p.register_literal("101", "999")  # short number: could be any reading
    p.register_literal("45.12", "0.0")  # two decimals: could be any reading
    p.register_literal("Zona", "Zona 001")  # replacement would match again
    p.register_literal("<redacted len=4>", "x")
    p.register_literal("--", "x")
    assert p.text("101 45.12 Zona --") == "101 45.12 Zona --"
    p.register_literal("12.34567", "45.00000")
    p.register_literal("20231234", "11111111")
    p.register_literal("Casa Bianca", "Zona 003")
    p.register_literal("Casa Bianca", "Zona 004")  # first registration wins
    assert p.text("12.34567 20231234 casa bianca 112.34567") == (
        "45.00000 11111111 Zona 003 112.34567"
    )


def test_new_personal_field_predicates() -> None:
    assert personal_kind("METEO", "POSIZIONE") == "position"
    assert personal_kind("METEO_DATA", "dt1790337600") == "weather"
    assert personal_kind("ATTUATORE", "NOME") == "name"
    assert personal_kind("CONFIG", "INSTALLATION_ID") == "installation"
    assert personal_kind("METEO", "VENTO") is None
    for key in ("INSTALLATION_ID", "POSIZIONE", "ADMIN_EMAIL", "GPS_FIX", "LAT", "LONGITUDE"):
        assert is_personal_field("CONFIG", key), key
    assert not is_personal_field("REHOM", "ALLARME_BUS")
    p = Pseudonymiser("s")
    assert p.record({"Gruppo": "METEO", "Key": "POSIZIONE", "Valore": "1.5,2"})["Valore"] == (
        "45.0,10"
    )
    assert p.record({"Gruppo": "METEO", "Key": "POSIZIONE", "Valore": "n/a"})["Valore"] == (
        "45.0,10.0"
    )
    assert p.record({"Gruppo": "METEO_DATA", "Key": "x", "Valore": 5})["Valore"] == 5
    weather = {"Gruppo": "METEO_DATA", "Key": "x", "Valore": {"name": "Testville", "id": 9}}
    assert p.record(weather)["Valore"] == {"name": "City", "id": 0}


def test_generic_dict_keys_and_json_edge_cases() -> None:
    p = Pseudonymiser("s")
    out = p.generic({"username": "mario", "HOME_CITY": "Testville", "note": "mario was here"})
    assert out["username"] == "user1"
    assert out["HOME_CITY"] == "City"
    assert out["note"] == "mario was here"  # only registered literals are replaced in free text
    weather = p.record({"Gruppo": "METEO_DATA", "Key": "x", "Valore": "5"})
    assert weather["Valore"] == "5"  # a JSON scalar has no location data
    odd = '{"city":  "Testville",  "t": 1}'  # non-standard spacing: compact output
    assert p.text(odd) == '{"city":"City","t":1}'
    assert p.scrub_json({"city": {"local_names": ["Testville", 3]}}, weather=False) == {
        "city": {"local_names": ["City", 0]}
    }
    capture = {
        "config.json": {"MATRICOLA": "AB123456", "INSTALLATION_ID": "inst-7788"},
        "alive.json": {"note": "unit ab123456 / inst-7788"},
    }
    fixed = pseudonymise_fixture(capture, "s")
    assert fixed["alive.json"]["note"] == (
        f"unit {fixed['config.json']['MATRICOLA']} / {fixed['config.json']['INSTALLATION_ID']}"
    )
    assert re.fullmatch(r"[A-Z]{2}\d{6}", fixed["config.json"]["MATRICOLA"])
    assert re.fullmatch(r"[a-z]{4}-\d{4}", fixed["config.json"]["INSTALLATION_ID"])
