"""``aiorehom.logic.lock``: the ``WEBSERVER`` lock."""

from __future__ import annotations

import pytest

from aiorehom.enums import LockState
from aiorehom.logic import LockInfo, lock_state


def _values(rows: list[tuple[str, str]]) -> list[str]:
    """What the builder passes: the Valore of rows whose Key is exactly WEBSERVER."""
    return [value for key, value in rows if key == "WEBSERVER"]


@pytest.mark.parametrize(
    ("interface", "overrides", "expected"),
    [
        ([], [], LockInfo(LockState.NORMAL, is_crono=False, serial_down=False)),
        (["1"], [], LockInfo(LockState.NORMAL, is_crono=False, serial_down=False)),
        (["0"], [], LockInfo(LockState.CRONO, is_crono=True, serial_down=False)),
        (["0.0"], [], LockInfo(LockState.CRONO, is_crono=True, serial_down=False)),
        ([], ["0"], LockInfo(LockState.CRONO, is_crono=True, serial_down=False)),
        (["2"], [], LockInfo(LockState.SERIAL_DOWN, is_crono=False, serial_down=True)),
        (["1"], ["2"], LockInfo(LockState.SERIAL_DOWN, is_crono=False, serial_down=True)),
        (["2"], ["0"], LockInfo(LockState.SERIAL_DOWN, is_crono=True, serial_down=True)),
        (["x"], [""], LockInfo(LockState.NORMAL, is_crono=False, serial_down=False)),
        (["3"], [], LockInfo(LockState.NORMAL, is_crono=False, serial_down=False)),
    ],
)
def test_lock_state(interface: list[str], overrides: list[str], expected: LockInfo) -> None:
    assert lock_state(interface, overrides) == expected


def test_last_row_wins_in_each_store() -> None:
    assert lock_state(["1", "0"], []).state is LockState.CRONO
    assert lock_state(["0", "1"], []).state is LockState.NORMAL
    assert lock_state([], ["0", "1"]).state is LockState.NORMAL
    assert lock_state(["1", "1"], ["1", "0"]).state is LockState.CRONO


def test_webserver_old_is_ignored() -> None:
    rows = [("WEBSERVER_OLD", "0"), ("WEBSERVER", "1"), ("WEBSERVER_OLD", "2")]
    assert lock_state(_values(rows), []).state is LockState.NORMAL
