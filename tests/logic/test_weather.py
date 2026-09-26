"""``aiorehom.logic.weather``: METEO rows and METEO_DATA items (synthetic values only)."""

from __future__ import annotations

import json
from datetime import datetime

from aiorehom.logic import ForecastItem, WeatherDayInfo, parse_forecast, parse_weather

SCALARS: dict[str, str | None] = {
    "DATA": "2026-01-02 08:30:00",
    "TEMPERATURE": "1.5,2,3.25,x,4,5,6,7,8",
    "UMIDITA": "61.5",
    "ALLARM_METEO_KEY": "0",
    "POSIZIONE": "45.0000000,10.0000000",
}
DAYS: dict[str, dict[str, str | None]] = {
    "1": {"PREVISIONE": "2,5", "RANGE_TEMP": "0.5,7.25", "PRESSIONE": "1011.5", "VENTO": "3.5,270"},
    "0": {"PREVISIONE": "0,0", "RANGE_TEMP": "1,6", "PRESSIONE": "1013", "VENTO": "2.25,90.5"},
    "10": {"RANGE_TEMP": "3"},
    "x": {"PRESSIONE": "1000"},
    "-1": {"PRESSIONE": "1000"},
}

#: Hand-written item with the shape of an OpenWeatherMap 3-hour forecast entry.
ITEM = {
    "dt": 1767312000,
    "main": {
        "temp": 3.5,
        "feels_like": 1,
        "temp_min": 3.0,
        "temp_max": 3.5,
        "pressure": 1012,
        "humidity": 80,
        "temp_kf": 0.5,
        "dew_point": 0.25,
    },
    "weather": [{"id": 500, "main": "Rain", "description": "light rain", "icon": "10d"}],
    "clouds": {"all": 90},
    "wind": {"speed": 3.1, "deg": 200, "gust": 6.5},
    "visibility": 10000,
    "pop": 0.4,
    "sys": {"pod": "d"},
    "dt_txt": "2026-01-02 00:00:00",
}


def test_parse_weather() -> None:
    info = parse_weather(SCALARS, DAYS)
    assert info is not None
    assert info.updated_local == datetime(2026, 1, 2, 8, 30)
    assert info.forecast_temperatures == (1.5, 2.0, 3.25, None, 4.0, 5.0, 6.0, 7.0, 8.0)
    assert info.humidity == 61.5
    assert info.service_ok is True
    assert [d.index for d in info.days] == [0, 1, 10]
    assert info.days[1] == WeatherDayInfo(
        index=1,
        condition_type=2,
        icon_code=5,
        temp_min=0.5,
        temp_max=7.25,
        pressure=1011.5,
        wind_speed=3.5,
        wind_bearing=270.0,
    )
    assert info.days[2] == WeatherDayInfo(10, None, None, 3.0, None, None, None, None)


def test_posizione_is_never_read() -> None:
    without = {k: v for k, v in SCALARS.items() if k != "POSIZIONE"}
    assert parse_weather(SCALARS, DAYS) == parse_weather(without, DAYS)
    assert "45.0000000" not in repr(parse_weather(SCALARS, DAYS))


def test_weather_empty_and_partial() -> None:
    assert parse_weather({}, {}) is None
    info = parse_weather({"ALLARM_METEO_KEY": "1"}, {})
    assert info is not None
    assert info.service_ok is False
    assert info.updated_local is None
    assert info.forecast_temperatures == ()
    assert info.days == ()
    missing_key = parse_weather({}, {"0": {}})
    assert missing_key is not None
    assert missing_key.service_ok is None
    assert missing_key.days == (WeatherDayInfo(0, None, None, None, None, None, None, None),)


def test_parse_forecast() -> None:
    items = parse_forecast({1767312000: json.dumps(ITEM)})
    assert items == (
        ForecastItem(
            epoch=1767312000,
            temperature=3.5,
            feels_like=1.0,
            humidity=80.0,
            dew_point=0.25,
            pressure=1012.0,
            wind_speed=3.1,
            wind_bearing=200.0,
            wind_gust=6.5,
            pop=0.4,
            clouds=90.0,
            condition_id=500,
            icon="10d",
        ),
    )


def test_forecast_skips_invalid_items_and_sorts() -> None:
    later = {**ITEM, "dt": 1767322800}
    raw = {
        1767322800: json.dumps(later),
        1: "{not json",
        2: "[1, 2]",
        3: "42",
        4: "[" * 100_000 + "]" * 100_000,
        1767312000: json.dumps(ITEM),
    }
    assert [item.epoch for item in parse_forecast(raw)] == [1767312000, 1767322800]


def test_forecast_ill_typed_fields() -> None:
    odd = {
        "dt": True,
        "main": {"temp": "22", "humidity": None, "pressure": False, "feels_like": float("nan")},
        "weather": [],
        "clouds": [90],
        "wind": "calm",
        "pop": 1,
    }
    (item,) = parse_forecast({99: json.dumps(odd)})
    assert item.epoch == 99  # dt is not an int: the map key is used
    assert item.temperature is None
    assert item.humidity is None
    assert item.pressure is None
    assert item.feels_like is None
    assert item.condition_id is None
    assert item.icon is None
    assert item.clouds is None
    assert item.wind_speed is None
    assert item.pop == 1.0
    (bare,) = parse_forecast({5: "{}"})
    assert bare == ForecastItem(5, *([None] * 12))
    weird = {"weather": [{"id": 1.5, "icon": 7}]}
    (item,) = parse_forecast({6: json.dumps(weird)})
    assert (item.condition_id, item.icon) == (None, None)
