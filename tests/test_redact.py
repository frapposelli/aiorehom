"""Redaction of records, dict payloads, WebSocket frames and HAR files."""

from __future__ import annotations

import base64
import copy
import json
from typing import Any

import pytest

from aiorehom.inventory import build_inventory
from aiorehom.redact import (
    SECRET_GROUPS,
    SECRET_PAIRS,
    is_redacted,
    is_secret_key,
    is_secret_record,
    marker_presence,
    redact_capture_file,
    redact_dict,
    redact_har,
    redact_records,
    redact_ws_frame,
    redacted_marker,
)

from .conftest import CONFIG, INTERFACE, SECRET_VALUES, rec


def test_marker() -> None:
    assert redacted_marker("abc") == "<redacted len=3>"
    assert redacted_marker("") == "<redacted len=0>"
    assert redacted_marker(12345) == "<redacted int len=5>"
    assert redacted_marker(-1.5) == "<redacted float len=4>"
    assert redacted_marker(True) == "<redacted bool>"
    assert redacted_marker(None) == "<redacted null>"
    assert redacted_marker(["a"]) == "<redacted list>"
    assert redacted_marker({"a": 1}) == "<redacted object>"
    for marker in (
        "<redacted len=12>",
        "<redacted int len=5>",
        "<redacted float len=4>",
        "<redacted null>",
        "<redacted bool>",
        "<redacted list>",
        "<redacted object>",
    ):
        assert is_redacted(marker), marker
    assert not is_redacted("<redacted len=x>")
    assert not is_redacted("<redacted>")
    assert not is_redacted(5)
    assert marker_presence("<redacted len=0>") == "empty"
    assert marker_presence("<redacted len=7>") == "7 chars"
    assert marker_presence("<redacted null>") == "null"
    assert marker_presence("<redacted int len=3>") == "int"
    assert marker_presence("<redacted object>") == "object"
    assert marker_presence("plain") is None
    assert marker_presence(None) is None


def test_null_and_non_string_secrets_keep_presence_and_type() -> None:
    """Regression: a null secret used to become '<redacted len=4>' (len('None'))."""
    rows = [
        {"Gruppo": "CONFIG", "Key": "REMOTE_TOKEN", "Valore": None},
        {"Gruppo": "WIFI", "Key": "PASSWORD", "Valore": ["a"]},
        {"Gruppo": "CONFIG", "Key": "METEO_KEY", "Valore": ""},
        {"Gruppo": "UTEN", "Key": "admin", "Valore": 1234},
    ]
    redacted, count = redact_records(rows)
    assert [r["Valore"] for r in redacted] == [
        "<redacted null>",
        "<redacted list>",
        "<redacted len=0>",
        "<redacted int len=4>",
    ]
    assert count == 4
    assert redact_dict({"METEO_KEY": None})[0] == {"METEO_KEY": "<redacted null>"}
    inventory = build_inventory({"interface.json": rows})
    by_name = {s["name"]: s for s in inventory["secrets"]}
    assert "value" not in by_name["CONFIG.REMOTE_TOKEN"]  # presence only, never a length
    assert by_name["CONFIG.REMOTE_TOKEN"]["presence"] == "null"
    assert by_name["CONFIG.METEO_KEY"]["presence"] == "empty"
    assert by_name["WIFI.PASSWORD"]["presence"] == "list"
    assert by_name["UTEN.admin"]["presence"] == "int"


@pytest.mark.parametrize(
    ("key", "secret"),
    [
        ("PASSWORD", True),
        ("PwdWiFi", True),
        ("REMOTE_TOKEN", True),
        ("METEO_KEY", True),
        ("my_secret", True),
        ("apikey", True),
        ("API_KEY", True),
        ("x-csrftoken", True),
        ("Passwd", True),
        ("ALLARM_METEO_KEY", False),
        ("allarm_meteo_key", True),  # exemption is exact
        ("NOME", False),
        ("STAGIONE", False),
        ("KEYWORD", False),
        (None, False),
    ],
)
def test_catch_all(key: Any, secret: bool) -> None:
    assert is_secret_key(key) is secret


@pytest.mark.parametrize(
    ("gruppo", "key", "secret"),
    [
        ("UTEN", "alice", True),
        ("UTEN", "admin", True),
        ("UTEN", "", True),
        ("WIFI", "PASSWORD", True),
        ("WIFI", "SSID", False),
        ("WEBSERVER", "PwdWiFi", True),
        ("WEBSERVER", "MacAddress", False),
        ("CONFIG", "METEO_KEY", True),
        ("CONFIG", "REMOTE_TOKEN", True),
        ("METEO", "ALLARM_METEO_KEY", False),
        ("ZONA", "NOME", False),
        ("NEWGROUP", "ADMIN_PASSWORD", True),
        (None, None, False),
    ],
)
def test_secret_records(gruppo: Any, key: Any, secret: bool) -> None:
    assert is_secret_record(gruppo, key) is secret


def test_redact_interface_records() -> None:
    original = copy.deepcopy(INTERFACE)
    redacted, count = redact_records(INTERFACE)
    assert original == INTERFACE  # input not mutated
    text = json.dumps(redacted)
    for secret in SECRET_VALUES[:6]:
        assert secret not in text
    assert count == 6  # 2 UTEN + WIFI.PASSWORD + PwdWiFi + REMOTE_TOKEN + CONFIG.METEO_KEY
    by_key = {(r["Gruppo"], r["Key"]): r for r in redacted}
    assert by_key[("UTEN", "alice")]["Valore"] == "<redacted len=14>"
    assert by_key[("UTEN", "alice")]["path"] == "UTEN...alice"
    assert by_key[("METEO", "ALLARM_METEO_KEY")]["Valore"] == "0"
    assert "Soggiorno di Alice" in text
    assert by_key[("WIFI", "SSID")]["Valore"] == "TestNet"
    # idempotent: redacting again changes nothing and counts nothing
    again, count2 = redact_records(redacted)
    assert again == redacted
    assert count2 == 0


def test_redact_records_edge_shapes() -> None:
    rows: list[Any] = [
        {"Key": "PASSWORD", "Valore": "x"},  # no Gruppo: still a record
        {"Gruppo": "UTEN", "Key": "bob", "Valore": 5, "Extra": "leak"},
        {"Gruppo": "ZONA", "Key": "NOME", "Valore": "ok", "token": "t"},
        "a string",
        [{"Gruppo": "UTEN", "Key": "x", "Valore": "nested"}],
    ]
    redacted, count = redact_records(rows)
    assert redacted[0]["Valore"] == "<redacted len=1>"
    assert redacted[1]["Valore"] == "<redacted int len=1>"
    assert redacted[1]["Extra"] == "<redacted len=4>"
    assert redacted[2] == {
        "Gruppo": "ZONA",
        "Key": "NOME",
        "Valore": "ok",
        "token": "<redacted len=1>",
    }
    assert redacted[3] == "a string"
    assert redacted[4][0]["Valore"] == "<redacted len=6>"
    assert count == 5
    # a non-list payload (e.g. an error object) falls back to dict rules
    obj, n = redact_records({"detail": "x", "token": "abc"})
    assert obj == {"detail": "x", "token": "<redacted len=3>"}
    assert n == 1


def test_redact_config_dict() -> None:
    redacted, count = redact_dict(CONFIG)
    assert redacted["METEO_KEY"] == "<redacted len=21>"
    assert redacted["LOCAL_TIME"] == CONFIG["LOCAL_TIME"]
    assert count == 1


def test_redact_dict_recursive_rules() -> None:
    payload = {
        "ALLARM_METEO_KEY": "1",
        "username": "alice",
        "Password": "p",
        "TOKEN": "t",
        "auth_token": {"deep": "structure"},
        "list": [{"api_key": "k"}, {"apiKey": "k2"}, {"secret_sauce": 1}],
        "records": [{"Gruppo": "WIFI", "Key": "PASSWORD", "Valore": "w"}],
        "pairs": [{"key": "METEO_KEY", "value": "mk"}, {"key": "stagione", "value": "0"}],
        "tuple": ("a", {"pwd": "x"}),
        3: "int key",
    }
    redacted, count = redact_dict(payload)
    assert redacted["ALLARM_METEO_KEY"] == "1"
    assert redacted["username"] == "alice"
    assert redacted["Password"] == "<redacted len=1>"
    assert redacted["TOKEN"] == "<redacted len=1>"
    assert redacted["auth_token"] == redacted_marker({"deep": "structure"}) == "<redacted object>"
    assert redacted["list"] == [
        {"api_key": "<redacted len=1>"},
        {"apiKey": "<redacted len=2>"},
        {"secret_sauce": "<redacted int len=1>"},
    ]
    assert redacted["records"][0]["Valore"] == "<redacted len=1>"
    assert redacted["pairs"] == [
        {"key": "METEO_KEY", "value": "<redacted len=2>"},
        {"key": "stagione", "value": "0"},
    ]
    assert redacted["tuple"] == ["a", {"pwd": "<redacted len=1>"}]
    assert redacted[3] == "int key"
    assert count == 9
    assert payload["Password"] == "p"


@pytest.mark.parametrize(
    ("frame", "redacted_value"),
    [
        ({"domain": "termo", "type": "update", "path": "X...NEW_PASSWORD", "value": "k"}, True),
        ({"domain": "termo", "type": "update", "path": "X...API_TOKEN", "value": "k"}, True),
        (
            {"domain": "termo", "type": "update", "path": "METEO...ALLARM_METEO_KEY", "value": "1"},
            False,
        ),
        (
            {"domain": "termo", "type": "update", "path": "ZONA.001..TEMP_AMBIENTE", "value": "21"},
            False,
        ),
        # odd path shapes
        ({"domain": "termo", "type": "update", "path": "X...a.secret", "value": "k"}, True),
        ({"domain": "termo", "type": "update", "path": "X..API_TOKEN", "value": "k"}, True),
        (
            {"domain": "termo", "type": "update", "path": "METEO..ALLARM_METEO_KEY", "value": "1"},
            False,
        ),
        ({"domain": "termo", "type": "update", "path": "ZONA.001.TEMP", "value": "21"}, False),
        ({"domain": "termo", "type": "update", "path": "", "value": "x"}, False),
        ({"domain": "termo", "type": "update", "value": "x"}, True),
        ({"domain": "termo", "type": "update", "path": 5, "value": "x"}, True),
        (
            {
                "domain": "termo",
                "type": "update",
                "gruppo": "X",
                "path": "Y...API_TOKEN",
                "value": "x",
            },
            True,
        ),
        # bus frames
        ({"domain": "bus", "type": "update", "key": "stagione", "value": "1"}, False),
        ({"domain": "bus", "type": "update", "key": "METEO_KEY", "value": "k"}, True),
        ({"domain": "bus", "type": "update", "key": "wifi_password", "value": "k"}, True),
        ({"domain": "bus", "type": "update", "value": "k"}, True),
    ],
)
def test_ws_frames(frame: dict[str, Any], redacted_value: bool) -> None:
    original = copy.deepcopy(frame)
    out, count = redact_ws_frame(frame)
    assert frame == original
    assert is_redacted(out["value"]) is redacted_value
    assert count == (1 if redacted_value else 0)
    for key in ("domain", "type", "path"):
        if key in frame:
            assert out[key] == frame[key]


#: The configured secret groups and (group, key) pairs, addressed as termo frames.
_SECRET_RECORDS = sorted([*((group, "someone") for group in SECRET_GROUPS), *SECRET_PAIRS])


@pytest.mark.parametrize(("gruppo", "key"), _SECRET_RECORDS)
def test_ws_frames_follow_the_record_rules(gruppo: str, key: str) -> None:
    """A termo frame is redacted whenever its record would be, whatever the path shape."""
    assert is_secret_record(gruppo, key)
    frames = [
        {"path": f"{gruppo}...{key}"},
        {"path": f"{gruppo}..{key}"},
        {"path": f"{gruppo}...{key}.x"},
        {"gruppo": gruppo, "path": f"{gruppo}...{key}"},
    ]
    if gruppo in SECRET_GROUPS:
        frames.append({"gruppo": gruppo, "path": "A.B.C.D"})
    for fields in frames:
        frame = {"domain": "termo", "type": "update", **fields, "value": "v"}
        out, count = redact_ws_frame(frame)
        assert out["value"] == "<redacted len=1>", frame
        assert count == 1


def test_ws_termo_override_and_extra_fields() -> None:
    frame = {
        "domain": "termo",
        "type": "update",
        "gruppo": "PROG_OVERRIDE",
        "path": "PROG_OVERRIDE.001.1.PROG_GIORNO_INVERNO",
        "value": "3,3",
        "issuedAt": "2026-09-25 18:10:00",
        "expiresAt": "2026-09-25 20:09:59",
        "extra": {"token": "t"},
    }
    out, count = redact_ws_frame(frame)
    assert out["value"] == "3,3"
    assert out["issuedAt"] == frame["issuedAt"]
    assert out["extra"] == {"token": "<redacted len=1>"}
    assert count == 1
    secret = {"domain": "termo", "path": "X...TOKEN", "value": "v", "other": "o", "issuedAt": "i"}
    out, count = redact_ws_frame(secret)
    assert out["other"] == "<redacted len=1>"
    assert out["issuedAt"] == "i"
    assert count == 2


def test_ws_other_frames() -> None:
    frame = {"domotica": {"objects": [{"id": 1, "state": {"state": "1", "password": "x"}}]}}
    out, count = redact_ws_frame(frame)
    assert out["domotica"]["objects"][0]["state"] == {"state": "1", "password": "<redacted len=1>"}
    assert count == 1
    out, count = redact_ws_frame(["a", {"secret": "b"}])
    assert out == ["a", {"secret": "<redacted len=1>"}]
    out, count = redact_ws_frame("plain")
    assert (out, count) == ("plain", 0)


# ---------------------------------------------------------------------------
# HAR
# ---------------------------------------------------------------------------


def _entry(
    url: str,
    method: str = "GET",
    *,
    request_body: Any = None,
    response_body: Any = None,
    mime: str = "application/json",
    encoding: str | None = None,
    request_mime: str = "application/json",
    ws: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "request": {
            "method": method,
            "url": url,
            "headers": [
                {"name": "Authorization", "value": "Token tok-abcdef0123456789"},
                {"name": "Cookie", "value": "sessionid=abc"},
                {"name": "X-CSRFToken", "value": "csrf"},
                {"name": "Accept", "value": "*/*"},
            ],
            "cookies": [{"name": "sessionid", "value": "abc"}],
            "queryString": [],
        },
        "response": {
            "status": 200,
            "headers": [
                {"name": "Set-Cookie", "value": "sessionid=abc"},
                {"name": "Content-Type", "value": mime},
            ],
            "cookies": [{"name": "sessionid", "value": "abc"}],
            "content": {"mimeType": mime, "size": 0},
        },
    }
    if request_body is not None:
        text = request_body if isinstance(request_body, str) else json.dumps(request_body)
        entry["request"]["postData"] = {"mimeType": request_mime, "text": text}
    if response_body is not None:
        text = response_body if isinstance(response_body, str) else json.dumps(response_body)
        if encoding == "base64":
            text = base64.b64encode(text.encode()).decode()
            entry["response"]["content"]["encoding"] = "base64"
        entry["response"]["content"]["text"] = text
    if ws is not None:
        entry["_webSocketMessages"] = ws
    return entry


CTRL = "http://198.51.100.8:8000"


def _har() -> dict[str, Any]:
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "test"},
            "entries": [
                _entry(
                    f"{CTRL}/api/get-token/",
                    "POST",
                    request_body={"username": "alice", "password": "s3cret-user-pw"},
                    response_body={"token": "tok-abcdef0123456789"},
                ),
                _entry(f"{CTRL}/api/interface/", response_body=INTERFACE),
                _entry(f"{CTRL}/api/overrides/", response_body=[], encoding="base64"),
                _entry(f"{CTRL}/api/config/", response_body=CONFIG, encoding="base64"),
                _entry(f"{CTRL}/api/plant/conf/", response_body={"stagione": "0"}),
                _entry(
                    f"{CTRL}/api/interface/bulk_update/",
                    "POST",
                    request_body=[rec("UTEN", "", "", "alice", "new-pw-000")],
                    response_body="",
                ),
                _entry(
                    f"{CTRL}/api/plant/conf/",
                    "POST",
                    request_body={"key": "stagione", "value": "1"},
                ),
                _entry(
                    f"{CTRL}/api/domotica/object/7/set_control_values/",
                    "PUT",
                    request_body="switch=1&api_key=zzz",
                    request_mime="application/x-www-form-urlencoded",
                ),
                _entry(f"{CTRL}/api/me/?token=qqq&x=1", response_body={"username": "alice"}),
                _entry(
                    f"{CTRL}/api/alive/", response_body="<html>not json</html>", mime="text/html"
                ),
                _entry(
                    f"{CTRL}/api/dynicons/fan.png",
                    response_body="iVBORw0=",
                    mime="image/png",
                    encoding=None,
                ),
                _entry(
                    f"{CTRL}/www/static/js/main.js",
                    response_body="var a=1;",
                    mime="application/javascript",
                ),
                _entry(f"{CTRL}/api/history/", response_body="garbage", encoding="base64"),
                _entry(
                    "https://sentry.io/api/1/store/",
                    "POST",
                    request_body={"extra": "whole record"},
                    response_body={"id": "x"},
                ),
                _entry(
                    "ws://198.51.100.8:8000/ws/",
                    ws=[
                        {
                            "type": "receive",
                            "opcode": 1,
                            "data": json.dumps(
                                {
                                    "domain": "termo",
                                    "type": "update",
                                    "path": "X...API_TOKEN",
                                    "value": "secret-value-1",
                                }
                            ),
                        },
                        {
                            "type": "receive",
                            "opcode": 1,
                            "data": json.dumps(
                                {
                                    "domain": "termo",
                                    "type": "update",
                                    "path": "ZONA.001..NOME",
                                    "value": "Soggiorno",
                                }
                            ),
                        },
                        {"type": "receive", "opcode": 1, "data": "not json"},
                        {"type": "receive", "opcode": 2, "data": "AAEC"},
                        "junk",
                    ],
                ),
            ],
            "_localStorage": {"rehom_auth": '{"accessToken":"tok-abcdef0123456789"}'},
        },
        "localStorage": {"rehom_auth": "x"},
    }


def test_redact_har_end_to_end() -> None:
    har = _har()
    original = copy.deepcopy(har)
    out = redact_har(har)
    assert har == original  # never mutates
    text = json.dumps(out)
    for secret in (
        *SECRET_VALUES,
        "secret-value-1",
        "new-pw-000",
        "sessionid=abc",
        "csrf",
        "zzz",
        "qqq",
    ):
        assert secret not in text, secret
    assert "localStorage" not in text
    entries = out["log"]["entries"]
    login = entries[0]
    assert "postData" not in login["request"]
    assert "text" not in login["response"]["content"]
    for entry in entries:
        names = {h["name"].lower() for h in entry["request"]["headers"]}
        assert names <= {"accept"}
        assert entry["request"]["cookies"] == []
        assert entry["response"]["cookies"] == []
        assert {h["name"] for h in entry["response"]["headers"]} == {"Content-Type"}
    interface = json.loads(entries[1]["response"]["content"]["text"])
    assert {(r["Gruppo"], r["Key"]) for r in interface} == {
        (r["Gruppo"], r["Key"]) for r in INTERFACE
    }
    config = json.loads(entries[3]["response"]["content"]["text"])
    assert config["METEO_KEY"].startswith("<redacted")
    assert "encoding" not in entries[3]["response"]["content"]
    bulk = json.loads(entries[5]["request"]["postData"]["text"])
    assert bulk[0]["Valore"] == "<redacted len=10>"
    assert json.loads(entries[6]["request"]["postData"]["text"]) == {
        "key": "stagione",
        "value": "1",
    }
    assert "switch=1" in entries[7]["request"]["postData"]["text"]
    assert "qqq" not in entries[8]["request"]["url"]
    assert "x=1" in entries[8]["request"]["url"]
    assert "text" not in entries[9]["response"]["content"]  # non-JSON API body dropped
    assert entries[10]["response"]["content"]["text"] == "iVBORw0="  # image kept
    assert entries[11]["response"]["content"]["text"] == "var a=1;"  # static asset kept
    assert "text" not in entries[12]["response"]["content"]  # undecodable base64 dropped
    sentry = entries[13]
    assert "postData" not in sentry["request"]
    assert "text" not in sentry["response"]["content"]
    ws = entries[14]["_webSocketMessages"]
    assert json.loads(ws[0]["data"])["value"] == "<redacted len=14>"
    assert json.loads(ws[1]["data"])["value"] == "Soggiorno"
    assert ws[2]["data"] == "" and ws[2]["_aiorehom"] == {"_raw_len": 8, "_non_json": True}
    assert ws[3]["data"] == ""
    assert len(ws) == 4
    counts = out["log"]["_aiorehom_redaction"]
    assert counts["login_bodies_dropped"] == 2
    assert counts["storage_blobs_dropped"] == 2
    assert counts["ws_frames"] == 4
    assert counts["ws_values_redacted"] == 1
    assert counts["foreign_bodies_dropped"] == 2
    assert counts["entries"] == 15
    assert counts["headers_removed"] >= 15 * 4


def test_redact_har_explicit_hosts_and_bad_shapes() -> None:
    har = _har()
    out = redact_har(har, controller_hosts=["other.host"])
    interface = out["log"]["entries"][1]
    assert "text" not in interface["response"]["content"]  # treated as foreign
    ws_entry = out["log"]["entries"][14]
    assert ws_entry["_webSocketMessages"] == []
    assert redact_har({})["log"]["entries"] == []
    assert redact_har({"log": {"entries": "x"}})["log"]["entries"] == []
    weird = {"log": {"entries": [{"request": {"url": "http://[bad", "postData": "str"}}, "x"]}}
    cleaned = redact_har(weird)
    assert "postData" not in cleaned["log"]["entries"][0]["request"]


def test_redact_har_non_json_request_body_dropped() -> None:
    har = {
        "log": {
            "entries": [
                _entry(f"{CTRL}/api/alive/"),
                _entry(
                    f"{CTRL}/api/interface/bulk_update/",
                    "POST",
                    request_body="raw=text",
                    request_mime="text/plain",
                ),
            ]
        }
    }
    out = redact_har(har)
    post = out["log"]["entries"][1]["request"]["postData"]
    assert post["text"] == ""
    assert "dropped" in post["comment"]
    har["log"]["entries"][1]["request"]["postData"]["params"] = [
        {"name": "password", "value": "p"},
        {"name": "a", "value": "b"},
        "junk",
    ]
    out = redact_har(har)
    params = out["log"]["entries"][1]["request"]["postData"]["params"]
    assert params == [
        {"name": "password", "value": "<redacted len=1>"},
        {"name": "a", "value": "b"},
    ]


def test_redact_capture_file_dispatch() -> None:
    lines, n = redact_capture_file(
        "ws.jsonl",
        [
            {"frame": {"domain": "termo", "path": "X...API_TOKEN", "value": "x"}},
            {"event": {"token": "t"}},
        ],
    )
    assert lines[0]["frame"]["value"] == "<redacted len=1>"
    assert lines[1]["event"]["token"] == "<redacted len=1>"
    assert n == 2
    records, n = redact_capture_file("overrides.json", [rec("CONFIG", "", "", "REMOTE_TOKEN", "r")])
    assert records[0]["Valore"] == "<redacted len=1>"
    assert redact_capture_file("config.json", {"METEO_KEY": "k"}) == (
        {"METEO_KEY": "<redacted len=1>"},
        1,
    )


# --- fail-closed record redaction (added after the build verification) -------


def test_record_without_gruppo_but_uten_path_is_redacted() -> None:
    rows = [
        {"Unita": "", "SubUni": "", "Key": "alice", "Valore": "hunter2", "path": "UTEN...alice"}
    ]
    out, count = redact_records(rows)
    assert out[0]["Valore"] == "<redacted len=7>"
    assert count == 1


def test_lowercase_record_fields_are_redacted() -> None:
    rows = [{"gruppo": "UTEN", "unita": "", "subuni": "", "key": "alice", "valore": "hunter2"}]
    out, _ = redact_records(rows)
    assert out[0]["valore"] == "<redacted len=7>"
    assert out[0]["gruppo"] == "UTEN"


def test_record_with_no_group_and_no_path_fails_closed() -> None:
    rows = [{"Key": "whatever", "Valore": "maybe-secret"}]
    out, count = redact_records(rows)
    assert out[0]["Valore"] == "<redacted len=12>"
    assert count == 1


def test_ordinary_record_is_not_redacted() -> None:
    rows = [
        {"Gruppo": "ZONA", "Unita": "001", "SubUni": "", "Key": "TEMP_AMBIENTE", "Valore": "21.4"}
    ]
    out, count = redact_records(rows)
    assert out[0]["Valore"] == "21.4"
    assert count == 0
