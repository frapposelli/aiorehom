"""Cloud weather: ``METEO`` rows and ``METEO_DATA`` forecast items.

``METEO.TEMPERATURE`` is a 9-value three-hourly *forecast*, not an outdoor
reading.  ``METEO.POSIZIONE`` (personal: latitude/longitude) is never read.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any, Final

from ..values import parse_float, parse_int, split_csv
from .parse import parse_device_timestamp
from .types import ForecastItem, WeatherDayInfo, WeatherInfo

__all__ = ["parse_forecast", "parse_weather"]

_DAY_RE: Final = re.compile(r"[0-9]+")


def _pair(raw: str | None) -> tuple[str | None, str | None]:
    items = split_csv(raw)
    first = items[0] if items else None
    second = items[1] if len(items) > 1 else None
    return first, second


def _day(index: int, rows: Mapping[str, str | None]) -> WeatherDayInfo:
    condition_type, icon_code = _pair(rows.get("PREVISIONE"))
    temp_min, temp_max = _pair(rows.get("RANGE_TEMP"))
    wind_speed, wind_bearing = _pair(rows.get("VENTO"))
    return WeatherDayInfo(
        index=index,
        condition_type=parse_int(condition_type),
        icon_code=parse_int(icon_code),
        temp_min=parse_float(temp_min),
        temp_max=parse_float(temp_max),
        pressure=parse_float(rows.get("PRESSIONE")),
        wind_speed=parse_float(wind_speed),
        wind_bearing=parse_float(wind_bearing),
    )


def parse_weather(
    scalars: Mapping[str, str | None], days: Mapping[str, Mapping[str, str | None]]
) -> WeatherInfo | None:
    """Decode the ``METEO`` rows.

    ``scalars`` holds the rows with ``Unita ""`` (Key -> Valore); ``days`` is
    keyed by ``Unita`` (``"0"``..``"5"``, day 0 = today local).  Both empty ->
    ``None``.  Days are sorted by their integer ``Unita``; other keys are
    skipped.
    """
    if not scalars and not days:
        return None
    weather_key = scalars.get("ALLARM_METEO_KEY")
    day_items = sorted(
        (int(unita), unita, rows) for unita, rows in days.items() if _DAY_RE.fullmatch(unita)
    )
    return WeatherInfo(
        updated_local=parse_device_timestamp(scalars.get("DATA")),
        forecast_temperatures=tuple(parse_float(x) for x in split_csv(scalars.get("TEMPERATURE"))),
        humidity=parse_float(scalars.get("UMIDITA")),
        days=tuple(_day(index, rows) for index, _, rows in day_items),
        service_ok=None if weather_key is None else parse_int(weather_key) == 0,
    )


def _get(obj: object, *path: str | int) -> object:
    for part in path:
        if isinstance(part, int):
            if not isinstance(obj, list) or len(obj) <= part:
                return None
            obj = obj[part]
        else:
            if not isinstance(obj, dict):
                return None
            obj = obj.get(part)
    return obj


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _item(key: int, raw: str) -> ForecastItem | None:
    try:
        data: Any = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    dt = _integer(data.get("dt"))
    icon = _get(data, "weather", 0, "icon")
    return ForecastItem(
        epoch=key if dt is None else dt,
        temperature=_number(_get(data, "main", "temp")),
        feels_like=_number(_get(data, "main", "feels_like")),
        humidity=_number(_get(data, "main", "humidity")),
        dew_point=_number(_get(data, "main", "dew_point")),
        pressure=_number(_get(data, "main", "pressure")),
        wind_speed=_number(_get(data, "wind", "speed")),
        wind_bearing=_number(_get(data, "wind", "deg")),
        wind_gust=_number(_get(data, "wind", "gust")),
        pop=_number(data.get("pop")),
        clouds=_number(_get(data, "clouds", "all")),
        condition_id=_integer(_get(data, "weather", 0, "id")),
        icon=icon if isinstance(icon, str) else None,
    )


def parse_forecast(items: Mapping[int, str]) -> tuple[ForecastItem, ...]:
    """Decode ``METEO_DATA`` items (``dt`` epoch -> raw JSON string), sorted by epoch.

    Invalid JSON and non-objects are skipped; missing or ill-typed fields
    (including non-finite numbers) become ``None``.  ``epoch`` is the item's
    integer ``dt``, else the map key.
    """
    decoded = [
        (item.epoch, key, item)
        for key, raw in items.items()
        if (item := _item(key, raw)) is not None
    ]
    decoded.sort(key=lambda entry: (entry[0], entry[1]))
    return tuple(item for _, _, item in decoded)
