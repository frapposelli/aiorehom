"""The public surface of ``aiorehom``."""

from __future__ import annotations

import dataclasses
import inspect

import aiorehom
from aiorehom import enums, models


def test_version() -> None:
    assert aiorehom.__version__ == "0.2.0"


def test_every_model_and_enum_is_exported() -> None:
    public = set(aiorehom.__all__)
    model_classes = {
        name
        for name, obj in vars(models).items()
        if inspect.isclass(obj)
        and dataclasses.is_dataclass(obj)
        and obj.__module__ == models.__name__
    }
    enum_classes = {
        name
        for name, obj in vars(enums).items()
        if inspect.isclass(obj) and obj.__module__ == enums.__name__ and not name.startswith("_")
    }
    assert model_classes <= public
    assert enum_classes <= public
    assert {
        "RehomClient",
        "ClientOptions",
        "DeviceClock",
        "RehomNotReadyError",
        "ReadOnlyTransport",
        "RecordStore",
    } <= public
    assert all(hasattr(aiorehom, name) for name in aiorehom.__all__)
