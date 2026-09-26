"""The ``WEBSERVER`` lock.

``Key == "WEBSERVER"`` rows (any Gruppo, either store): ``0`` = crono mode,
``2`` = serial line interrupted, anything else = normal.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ..enums import LockState
from ..values import parse_int
from .types import LockInfo

__all__ = ["lock_state"]

_DEFAULT: Final = "1"


def lock_state(interface_values: Sequence[str], override_values: Sequence[str]) -> LockInfo:
    """Decode the lock from the ``Valore`` of every ``Key == "WEBSERVER"`` row, in store order.

    The last value wins within each store, and an empty store defaults to
    ``"1"``.  Crono if either store says 0; serial down if either says 2
    (serial down takes precedence for ``state``).
    """
    vi = parse_int(interface_values[-1] if interface_values else _DEFAULT)
    vo = parse_int(override_values[-1] if override_values else _DEFAULT)
    is_crono = vi == 0 or vo == 0
    serial_down = vi == 2 or vo == 2
    if serial_down:
        state = LockState.SERIAL_DOWN
    elif is_crono:
        state = LockState.CRONO
    else:
        state = LockState.NORMAL
    return LockInfo(state=state, is_crono=is_crono, serial_down=serial_down)
