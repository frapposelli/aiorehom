"""StoreSet: frame application, snapshot replace and change accounting (design 4.2-4.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiorehom.exceptions import RehomResponseError
from aiorehom.state import (
    ChangeSet,
    ForecastCache,
    Snapshot,
    StoreSet,
    config_diff,
    parse_config,
    parse_plant_conf,
    parse_record_rows,
)

from .conftest import rec
from .sync_fakes import bus, termo

T0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=UTC)
M0 = 1000.0


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def snapshot(
    interface: list[dict[str, Any]],
    overrides: list[dict[str, Any]] | None = None,
    *,
    plant_conf: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> Snapshot:
    return Snapshot(
        interface=tuple(parse_record_rows(interface, path="/api/interface/")),
        overrides=tuple(parse_record_rows(overrides or [], path="/api/overrides/", overrides=True)),
        plant_conf=parse_plant_conf(plant_conf or {}),
        config=parse_config(config or {}),
        config_received_at=T0,
    )


def loaded(*rows: dict[str, Any], overrides: list[dict[str, Any]] | None = None) -> StoreSet:
    stores = StoreSet()
    stores.replace(snapshot(list(rows), overrides), T0)
    return stores


def apply(stores: StoreSet, frame: Any, seconds: float) -> ChangeSet:
    return stores.apply_frame(frame, at(seconds), M0 + seconds)


# ---------------------------------------------------------------------------
# keys and values
# ---------------------------------------------------------------------------


def test_raw_string_keys_are_never_integer_normalised() -> None:
    stores = loaded(
        rec("DEUM", "1", "", "ST_MODE", "0"),
        rec("DEUM", "001", "", "ST_MODE", "1"),
        rec("DEUM", "000", "", "ST_MODE", "2"),
        rec("PROG", "002", 0, "PROG_SETT_INVERNO", "1"),
    )
    assert stores.value("DEUM", "1", "", "ST_MODE") == "0"
    assert stores.value("DEUM", "001", "", "ST_MODE") == "1"
    assert stores.value("DEUM", "000", "", "ST_MODE") == "2"
    assert stores.value("PROG", "002", "0", "PROG_SETT_INVERNO") == "1"
    changes = apply(stores, termo("DEUM.1..ST_MODE", "5"), 1)
    assert changes.changed == {"DEUM.1..ST_MODE"}
    assert stores.value("DEUM", "001", "", "ST_MODE") == "1"
    assert [r.unita for r in stores.rows("DEUM")] == ["1", "001", "000"]


def test_noop_update_keeps_changed_at_and_the_stored_spelling() -> None:
    stores = loaded(rec("REHOM", "", "", "TEMP_COM", "24"))
    before = stores.row("REHOM", "", "", "TEMP_COM")
    assert before is not None
    assert before.changed_at == T0
    assert before.frame_at is None
    changes = apply(stores, termo("REHOM...TEMP_COM", "24.0"), 5)
    assert changes.empty()
    after = stores.row("REHOM", "", "", "TEMP_COM")
    assert after is not None
    assert after.value == "24"  # a suppressed frame must not move the built state
    assert after.changed_at == T0  # not a change
    assert after.frame_at == at(5)
    assert stores.counters.noop_updates == 1
    assert stores.counters.changed_updates == 0


def test_semantic_change_updates_changed_at() -> None:
    stores = loaded(rec("REHOM", "", "", "TEMP_COM", "24"))
    changes = apply(stores, termo("REHOM...TEMP_COM", "24.1"), 3)
    assert changes.changed == {"REHOM...TEMP_COM"}
    assert changes.removed == set()
    row = stores.row("REHOM", "", "", "TEMP_COM")
    assert row is not None
    assert (row.value, row.changed_at, row.frame_at) == ("24.1", at(3), at(3))
    assert row.path == "REHOM...TEMP_COM"
    assert stores.counters.changed_updates == 1


def test_new_row_from_frame_is_added_at_the_end() -> None:
    stores = loaded(rec("ZONA", "001", "", "NOME", "Zona 001"))
    changes = apply(stores, termo("ZONA.001..TEMP_AMBIENTE", 21.5), 2)
    assert changes.changed == {"ZONA.001..TEMP_AMBIENTE"}
    assert stores.value("ZONA", "001", "", "TEMP_AMBIENTE") == "21.5"  # norm() -> str
    assert [r.key for r in stores.rows("ZONA", "001")] == ["NOME", "TEMP_AMBIENTE"]
    assert [r.key for r in stores.rows_with_key("TEMP_AMBIENTE")] == ["TEMP_AMBIENTE"]


# ---------------------------------------------------------------------------
# removes and coalescing
# ---------------------------------------------------------------------------


def test_remove_then_update_within_window_is_a_replace() -> None:
    stores = loaded(rec("METEO", "0", "", "VENTO", "3.5,180"))
    assert apply(stores, termo("METEO.0..VENTO", kind="remove"), 10).empty()
    assert stores.pending_removes == 1
    assert stores.value("METEO", "0", "", "VENTO") == "3.5,180"  # still visible
    assert stores.next_expiry() == M0 + 11
    changes = apply(stores, termo("METEO.0..VENTO", "3.5,180"), 10.9)
    assert changes.empty()
    assert stores.pending_removes == 0
    assert stores.counters.removes_coalesced == 1
    assert stores.counters.noop_updates == 1
    assert stores.counters.removes_applied == 0


def test_remove_then_changed_update_within_window_is_a_change_not_a_removal() -> None:
    stores = loaded(rec("METEO", "0", "", "VENTO", "3.5,180"))
    apply(stores, termo("METEO.0..VENTO", kind="remove"), 10)
    changes = apply(stores, termo("METEO.0..VENTO", "4,200"), 10.5)
    assert changes.changed == {"METEO.0..VENTO"}
    assert changes.removed == set()


def test_remove_then_update_after_window_is_remove_then_add() -> None:
    stores = loaded(rec("METEO", "0", "", "VENTO", "3.5,180"), rec("METEO", "", "", "DATA", "x"))
    apply(stores, termo("METEO.0..VENTO", kind="remove"), 10)
    # the next frame (any path) expires it first (step 0)
    changes = apply(stores, termo("METEO...DATA", "y"), 11.0)
    assert changes.changed == {"METEO.0..VENTO", "METEO...DATA"}
    assert changes.removed == {"METEO.0..VENTO"}
    assert stores.value("METEO", "0", "", "VENTO") is None
    assert stores.counters.removes_applied == 1
    changes = apply(stores, termo("METEO.0..VENTO", "3.5,180"), 11.5)
    assert changes.changed == {"METEO.0..VENTO"}
    assert changes.removed == set()
    assert stores.counters.removes_coalesced == 0


def test_expire_on_its_own() -> None:
    stores = loaded(rec("PROG", "001", "3", "PROG_SETT_INVERNO", "1"))
    apply(stores, termo("PROG.001.3.PROG_SETT_INVERNO", kind="remove"), 0)
    assert stores.expire(M0 + 0.99).empty()
    changes = stores.expire(M0 + 1.0)
    assert changes.removed == {"PROG.001.3.PROG_SETT_INVERNO"}
    assert changes.changed == {"PROG.001.3.PROG_SETT_INVERNO"}
    assert stores.next_expiry() is None
    assert list(stores.rows("PROG")) == []


def test_remove_of_unknown_or_already_pending_path_is_a_noop() -> None:
    stores = loaded(rec("ZONA", "001", "", "NOME", "Zona 001"))
    assert apply(stores, termo("ZONA.009..NOME", kind="remove"), 1).empty()
    assert apply(stores, termo("ZONA.001..NOME", kind="remove"), 2).empty()
    assert apply(stores, termo("ZONA.001..NOME", kind="remove"), 2.5).empty()
    assert stores.next_expiry() == M0 + 3  # the second remove did not extend it
    assert stores.counters.frames_ignored == 2


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------


OVR_ROW = {
    "Gruppo": "PROG",  # REST says PROG: re-grouped
    "Unita": "001",
    "SubUni": "1",
    "Key": "PROG_GIORNO_ESTATE",
    "Valore": ",".join(["3"] * 48),
    "Impostazione": "2026-09-25 12:10:00",
    "Scadenza": "2026-09-25 12:44:59",
}


def test_rest_override_rows_are_regrouped_and_keep_timestamps() -> None:
    lock = {"Gruppo": "REHOM", "Unita": "", "SubUni": "", "Key": "WEBSERVER", "Valore": "1"}
    stores = loaded(overrides=[OVR_ROW, lock])
    rows = list(stores.override_rows())
    assert [(r.gruppo, r.key) for r in rows] == [
        ("PROG_OVERRIDE", "PROG_GIORNO_ESTATE"),
        ("REHOM", "WEBSERVER"),
    ]
    assert rows[0].impostazione == "2026-09-25 12:10:00"
    assert rows[0].scadenza == "2026-09-25 12:44:59"
    assert rows[1].impostazione is None
    # overrides are not interface rows
    assert stores.value("PROG_OVERRIDE", "001", "1", "PROG_GIORNO_ESTATE") is None


def test_ws_override_routing_and_timestamp_only_change() -> None:
    stores = loaded(overrides=[OVR_ROW])
    frame = termo(
        "PROG.001.1.PROG_GIORNO_ESTATE",  # path prefix PROG, gruppo PROG_OVERRIDE
        OVR_ROW["Valore"],
        gruppo="PROG_OVERRIDE",
        issuedAt="2026-09-25 12:10:00",
        expiresAt="2026-09-25 13:44:59",
    )
    changes = apply(stores, frame, 1)
    assert changes.changed == {"PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"}  # unlike the web UI
    (row,) = stores.override_rows()
    assert row.scadenza == "2026-09-25 13:44:59"
    assert stores.value("PROG", "001", "1", "PROG_GIORNO_ESTATE") is None
    # identical frame again: no-op
    assert apply(stores, frame, 2).empty()
    # without gruppo, the path prefix routes it
    frame2 = {
        "domain": "termo",
        "type": "update",
        "path": "PROG_OVERRIDE.002.1.PROG_GIORNO_ESTATE",
        "value": "1",
    }
    assert apply(stores, frame2, 3).changed == {"PROG_OVERRIDE.002.1.PROG_GIORNO_ESTATE"}
    assert [r.unita for r in stores.override_rows()] == ["001", "002"]
    # removing an override is coalesced like any record
    apply(stores, termo("PROG.002.1.PROG_GIORNO_ESTATE", kind="remove", gruppo="PROG_OVERRIDE"), 4)
    changes = stores.expire(M0 + 5)
    assert changes.removed == {"PROG_OVERRIDE.002.1.PROG_GIORNO_ESTATE"}


def test_override_frame_without_timestamps_keeps_the_stored_ones() -> None:
    stores = loaded(overrides=[OVR_ROW])
    frame = termo("PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE", ",".join(["2"] * 48))
    assert apply(stores, frame, 1).changed == {"PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"}
    (row,) = stores.override_rows()
    assert row.impostazione == OVR_ROW["Impostazione"]
    new = termo(
        "PROG_OVERRIDE.003.1.PROG_GIORNO_ESTATE",
        "1",
        issuedAt=None,
        expiresAt="2026-09-25 13:00:00",
    )
    apply(stores, new, 2)
    rows = {r.unita: r for r in stores.override_rows()}
    assert rows["003"].impostazione is None
    assert rows["003"].scadenza == "2026-09-25 13:00:00"


# ---------------------------------------------------------------------------
# METEO_DATA, bus, ignored frames, redaction
# ---------------------------------------------------------------------------


def _meteo_data(epoch: int, **extra: Any) -> dict[str, Any]:
    return termo(f"METEO_DATA...dt{epoch}", f'{{"dt": {epoch}}}', **extra)


def test_meteo_data_is_never_a_record_and_bursts_replace_items() -> None:
    stores = loaded(rec("METEO", "", "", "DATA", "x"))
    assert stores.forecast is None
    changes = apply(stores, _meteo_data(100), 0)
    assert changes.forecast_changed
    assert changes.changed == set()
    apply(stores, _meteo_data(200), 1)
    forecast = stores.forecast
    assert forecast is not None
    assert forecast.received_at == at(0)
    assert dict(forecast.items) == {100: '{"dt": 100}', 200: '{"dt": 200}'}
    assert stores.forecast is forecast  # cached until the next frame
    assert list(stores.rows("METEO_DATA")) == []
    # 60 s later is still the same burst; more than 60 s starts a new one
    apply(stores, _meteo_data(300), 61)
    assert set(stores.forecast.items) == {100, 200, 300}  # type: ignore[union-attr]
    apply(stores, _meteo_data(400), 121.5)
    forecast = stores.forecast
    assert forecast is not None
    assert forecast.received_at == at(121.5)
    assert dict(forecast.items) == {400: '{"dt": 400}'}
    # gruppo METEO_DATA with another path prefix is still forecast; a bad key is ignored
    apply(stores, termo("X...dt500", "{}", gruppo="METEO_DATA"), 122)
    assert 500 in stores.forecast.items  # type: ignore[union-attr]
    assert not apply(stores, termo("METEO_DATA...POSIZIONE", "1,2"), 123).forecast_changed
    removed = apply(stores, termo("METEO_DATA...dt400", kind="remove"), 124)
    assert removed.forecast_changed
    assert 400 not in stores.forecast.items  # type: ignore[union-attr]
    assert not apply(stores, termo("METEO_DATA...zz", kind="remove"), 125).forecast_changed
    assert stores.counters.forecast_frames == 6
    assert stores.counters.frames_ignored == 2


def test_forecast_cache_directly() -> None:
    cache = ForecastCache(burst_gap=10)
    assert not cache.apply("x1", "{}", T0, 0)
    assert cache.snapshot() is None
    assert cache.apply("dt1", "a", T0, 0)
    assert cache.remove("dt1", at(1), 1)
    snap = cache.snapshot()
    assert snap is not None
    assert dict(snap.items) == {}
    with pytest.raises(TypeError):
        snap.items[1] = "x"  # type: ignore[index]


def test_bus_frames_update_the_plant_conf() -> None:
    stores = StoreSet()
    stores.replace(snapshot([], plant_conf={"stagione": "1", "CONFIGURA_ON": 0}), T0)
    assert dict(stores.plant_conf) == {"stagione": "1", "CONFIGURA_ON": "0"}
    assert apply(stores, bus("stagione", "0"), 1).conf_changed == {"stagione"}
    assert apply(stores, bus("stagione", "0.0"), 2).empty()
    assert stores.plant_conf["stagione"] == "0"  # raw spelling kept on a semantic no-op
    assert apply(stores, bus("new_key", "x"), 3).conf_changed == {"new_key"}
    assert apply(stores, bus("new_key", kind="remove"), 4).conf_changed == {"new_key"}
    assert apply(stores, bus("new_key", kind="remove"), 5).empty()
    assert apply(stores, bus("x", kind="flush"), 6).empty()
    assert apply(stores, {"domain": "bus", "type": "update", "value": "1"}, 7).empty()
    counters = stores.counters
    assert (counters.changed_updates, counters.noop_updates, counters.removes_applied) == (2, 1, 1)
    assert counters.frames_ignored == 3


def test_ris_bus_frame_only_records_commissioning_time() -> None:
    stores = loaded()
    changes = apply(stores, bus("RIS_CONFIGURA", "1"), 7)
    assert changes.touched
    assert not changes.reportable()
    assert not changes.empty()
    assert stores.last_commissioning_at == at(7)
    assert "RIS_CONFIGURA" not in stores.plant_conf
    assert stores.counters.frames_ignored == 1


@pytest.mark.parametrize(
    "frame",
    [
        {"domotica": {"objects": [{"id": 1, "state": {"state": "1"}}]}},
        ["not", "a", "mapping"],
        "text",
        None,
        {"domain": "other", "type": "update"},
        {"domain": "termo", "type": "update", "path": "BAD.PATH", "value": "1"},
        {"domain": "termo", "type": "update", "path": 12, "value": "1"},
        {"domain": "termo", "type": "refresh", "path": "ZONA.001..NOME", "value": "1"},
        {"_non_json": True, "_raw_len": 3},
    ],
)
def test_ignored_frames(frame: Any) -> None:
    stores = loaded(rec("ZONA", "001", "", "NOME", "Zona 001"))
    assert apply(stores, frame, 1).empty()
    assert stores.counters.frames_ignored == 1
    assert stores.value("ZONA", "001", "", "NOME") == "Zona 001"


def test_unredacted_secret_frames_are_stored_redacted() -> None:
    stores = loaded()
    apply(stores, termo("X...API_TOKEN", "secret-value-000001"), 1)
    apply(stores, termo("UTEN...first.last", "value-of-sixteen"), 2)  # a dotted Key
    apply(stores, termo("X...DEVICE_SECRET", "value-x-123"), 3)
    values = [r.value for r in stores.interface_rows()]
    assert values == ["<redacted len=19>", "<redacted len=16>", "<redacted len=11>"]
    assert stores.value("UTEN", "", "", "first.last") == "<redacted len=16>"
    apply(stores, bus("api_key", "k-123"), 4)
    assert stores.plant_conf["api_key"] == "<redacted len=5>"


def test_rest_snapshot_is_redacted() -> None:
    stores = loaded(
        rec("UTEN", "", "", "someone", "pw-123"), rec("CONFIG", "", "", "REMOTE_TOKEN", "tok")
    )
    assert stores.value("UTEN", "", "", "someone") == "<redacted len=6>"
    assert stores.value("CONFIG", "", "", "REMOTE_TOKEN") == "<redacted len=3>"
    config = parse_config({"METEO_KEY": "abc", "TIMEZONE": "Europe/Rome"})
    assert config == {"METEO_KEY": "<redacted len=3>", "TIMEZONE": "Europe/Rome"}


# ---------------------------------------------------------------------------
# replace()
# ---------------------------------------------------------------------------


def test_replace_diff_semantics() -> None:
    stores = StoreSet()
    first = snapshot(
        [
            rec("REHOM", "", "", "TEMP_COM", "24"),
            rec("REHOM", "", "", "MODO", "1"),
            rec("ZONA", "001", "", "NOME", "Zona 001"),
        ],
        [OVR_ROW],
        plant_conf={"stagione": "1", "gone": "x"},
        config={"LOCAL_TIME": "2026-09-25 12:00:00", "VERSION": "1", "LIST": [1, 2]},
    )
    initial = stores.replace(first, T0)
    assert len(initial.changed) == 4
    assert initial.conf_changed == {"stagione", "gone"}
    assert initial.config_changed == {"VERSION", "LIST"}
    assert stores.synced_at == T0
    apply(stores, _meteo_data(100), 1)
    apply(stores, termo("ZONA.001..NOME", kind="remove"), 2)  # pending: cleared by replace
    second = snapshot(
        [
            rec("REHOM", "", "", "TEMP_COM", "24.0"),  # same value, other spelling
            rec("REHOM", "", "", "MODO", "2"),  # changed
            rec("ZONA", "001", "", "NOME", "Zona 001"),
            rec("ZONA", "002", "", "NOME", "Zona 002"),  # added
        ],
        [],  # override removed
        plant_conf={"stagione": "1.0", "new": "y"},
        config={"LOCAL_TIME": "2026-09-25 12:05:00", "VERSION": "1", "LIST": [1, 2]},
    )
    later = at(300)
    diff = stores.replace(second, later)
    assert diff.changed == {
        "REHOM...MODO",
        "ZONA.002..NOME",
        "PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE",
    }
    assert diff.removed == {"PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"}
    assert diff.conf_changed == {"gone", "new"}
    assert diff.config_changed == set()  # LOCAL_TIME ignored
    assert stores.config["LOCAL_TIME"] == "2026-09-25 12:05:00"
    assert stores.pending_removes == 0
    unchanged = stores.row("REHOM", "", "", "TEMP_COM")
    changed = stores.row("REHOM", "", "", "MODO")
    added = stores.row("ZONA", "002", "", "NOME")
    assert unchanged is not None and changed is not None and added is not None
    assert (unchanged.value, unchanged.changed_at) == ("24", T0)  # stored spelling kept
    assert changed.changed_at == later
    assert added.changed_at == later
    assert added.frame_at is None
    assert stores.plant_conf["stagione"] == "1"  # semantic no-op keeps the raw spelling
    assert stores.forecast is not None  # WS-only: survives
    assert stores.synced_at == later
    assert [r.key for r in stores.interface_rows()] == ["TEMP_COM", "MODO", "NOME", "NOME"]


def test_replace_keeps_frame_at_and_handles_duplicates() -> None:
    stores = loaded(rec("PROC", "", "", "WATCHDOG_MASTER", "1"), rec("ZONA", "001", "", "A", "1"))
    apply(stores, termo("PROC...WATCHDOG_MASTER", "1"), 5)
    apply(stores, termo("ZONA.001..A", "2"), 6)
    diff = stores.replace(
        snapshot(
            [
                rec("PROC", "", "", "WATCHDOG_MASTER", "1"),
                rec("ZONA", "001", "", "A", "1"),
                rec("ZONA", "001", "", "B", "x"),
                rec("ZONA", "001", "", "A", "3"),  # duplicate identity: last wins
            ]
        ),
        at(10),
    )
    assert diff.changed == {"ZONA.001..A", "ZONA.001..B"}
    heartbeat = stores.row("PROC", "", "", "WATCHDOG_MASTER")
    row_a = stores.row("ZONA", "001", "", "A")
    assert heartbeat is not None and row_a is not None
    assert heartbeat.frame_at == at(5)
    assert (row_a.value, row_a.changed_at, row_a.frame_at) == ("3", at(10), at(6))
    assert [r.key for r in stores.rows("ZONA", "001")] == ["A", "B"]


def test_replace_config_and_config_diff() -> None:
    stores = loaded()
    stores.replace_config({"VERSION": "1", "LOCAL_TIME": "a", "N": 1})
    changes = stores.replace_config({"VERSION": "1.0", "LOCAL_TIME": "b", "N": 1.0, "X": "1"})
    assert changes.config_changed == {"N", "X"}  # 1 vs 1.0 (non-str): type differs
    assert config_diff({"A": None}, {"A": None, "B": None}) == {"B"}
    assert config_diff({"A": "x"}, {}) == {"A"}


def test_change_set_merge_nets_removed_only() -> None:
    first = ChangeSet(changed={"a", "b"}, removed={"a"}, conf_changed={"k"})
    second = ChangeSet(changed={"a", "c"}, removed={"c"}, forecast_changed=True, touched=True)
    first.merge(second)
    assert first.changed == {"a", "b", "c"}
    assert first.removed == {"c"}  # "a" came back
    assert first.conf_changed == {"k"}
    assert first.forecast_changed
    assert first.touched
    assert ChangeSet().empty()
    assert ChangeSet(config_changed={"x"}).reportable()


# ---------------------------------------------------------------------------
# validation, views and misc
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("func", "payload", "path"),
    [
        (lambda p: parse_record_rows(p, path="/api/interface/"), {"a": 1}, "/api/interface/"),
        (
            lambda p: parse_record_rows(p, path="/api/overrides/", overrides=True),
            "x",
            "/api/overrides/",
        ),
        (parse_plant_conf, [1], "/api/plant/conf/"),
        (parse_config, None, "/api/config/"),
    ],
)
def test_snapshot_payload_shape_is_validated(func: Any, payload: Any, path: str) -> None:
    with pytest.raises(RehomResponseError, match=f"GET {path}: unexpected payload"):
        func(payload)


def test_non_mapping_items_are_skipped() -> None:
    rows = parse_record_rows([1, "x", rec("A", "", "", "K", None)], path="/api/interface/")
    assert [(r.key, r.value) for r in rows] == [(("A", "", "", "K"), "")]


def test_views_and_indexes() -> None:
    stores = loaded(
        rec("ZONA", "001", "", "NOME", "a"),
        rec("ZONA", "002", "", "NOME", "b"),
        rec("REHOM", "", "", "WEBSERVER", "1"),
        rec("WEBSERVER", "", "", "WEBSERVER_OLD", "1"),
        rec("ZONA", "001", "", "WEBSERVER", "0"),
    )
    assert [r.unita for r in stores.rows("ZONA")] == ["001", "002", "001"]
    assert [r.key for r in stores.rows("ZONA", "001")] == ["NOME", "WEBSERVER"]
    assert [r.gruppo for r in stores.rows_with_key("WEBSERVER")] == ["REHOM", "ZONA"]
    assert list(stores.rows("NOPE")) == []
    assert list(stores.rows_with_key("NOPE")) == []
    assert stores.row("ZONA", "009", "", "NOME") is None
    apply(stores, termo("ZONA.002..NOME", kind="remove"), 0)
    stores.expire(M0 + 5)
    assert [r.unita for r in stores.rows("ZONA")] == ["001", "001"]
    assert list(stores.rows("ZONA", "002")) == []
    apply(stores, termo("ZONA.001..WEBSERVER", kind="remove"), 10)
    stores.expire(M0 + 20)
    assert [r.gruppo for r in stores.rows_with_key("WEBSERVER")] == ["REHOM"]
    assert stores.interface_size == 3


def test_alive_live_since_and_constructor() -> None:
    stores = StoreSet()
    assert not stores.has_snapshot
    with pytest.raises(RuntimeError):
        _ = stores.synced_at
    assert stores.set_alive({"version": "1"})
    assert not stores.set_alive({"version": "1"})
    assert stores.set_alive("garbage")
    assert dict(stores.alive) == {}
    assert stores.set_live_since(T0).touched
    assert not stores.set_live_since(T0).touched
    assert stores.live_since == T0
    stores.live_since = None
    assert stores.live_since is None
    with pytest.raises(TypeError):
        stores.config["x"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="remove_coalesce_window"):
        StoreSet(remove_coalesce_window=0)
    with pytest.raises(ValueError, match="forecast_burst_gap"):
        StoreSet(forecast_burst_gap=0)


def test_dump_records() -> None:
    stores = loaded(rec("ZONA", "001", "", "NOME", "a"), overrides=[OVR_ROW])
    assert stores.dump_records() == [
        {
            "Gruppo": "ZONA",
            "Unita": "001",
            "SubUni": "",
            "Key": "NOME",
            "Valore": "a",
            "path": "ZONA.001..NOME",
        }
    ]
    (row,) = stores.dump_records(overrides=True)
    assert row["Gruppo"] == "PROG_OVERRIDE"
    assert row["path"] == "PROG_OVERRIDE.001.1.PROG_GIORNO_ESTATE"
    assert row["Impostazione"] == OVR_ROW["Impostazione"]
    assert row["Scadenza"] == OVR_ROW["Scadenza"]
