"""Presence and online vectors.

``REHOM.PRESENZA_*`` is a CSV of ``0``/``1`` per unit slot; ``STATO_*`` is the
matching online vector.  A missing or short ``STATO_*`` means *unknown*, never
offline (the web UI reports the first unit as "not responding" in that case).
"""

from __future__ import annotations

import re
from typing import Final

from ..values import parse_int, split_csv
from .types import UnitPresence

__all__ = [
    "WIDTH_ACTUATORS",
    "WIDTH_FANCOILS",
    "WIDTH_VMCS",
    "WIDTH_ZONES",
    "present_units",
    "unit_id",
    "unit_index",
]

#: ``PRESENZA_SONDE`` / ``STATO_SONDE``.
WIDTH_ZONES: Final = 24
#: ``PRESENZA_AT9091`` / ``STATO_AT9091``.
WIDTH_FANCOILS: Final = 24
#: ``PRESENZA_DEUM`` / ``STATO_DEUM``.
WIDTH_VMCS: Final = 3
#: ``PRESENZA_ATT`` / ``STATO_ATT``.
WIDTH_ACTUATORS: Final = 3

_CANONICAL_UNIT_RE: Final = re.compile(r"[0-9]{3}")


def unit_id(index: int) -> str:
    """Canonical unit id of a 0-based vector index: ``0`` -> ``"001"``."""
    return f"{index + 1:03d}"


def unit_index(unit: str, *, width: int) -> int | None:
    """0-based vector index of a canonical unit id, else ``None``.

    Only ``^[0-9]{3}$`` ids with ``1 <= n <= width`` map to an index, so stray
    rows such as ``"1"``, ``"000"`` or ``"025"`` never map to a unit.
    """
    if _CANONICAL_UNIT_RE.fullmatch(unit) is None:
        return None
    number = int(unit)
    if not 1 <= number <= width:
        return None
    return number - 1


def present_units(
    presence_csv: str | None, status_csv: str | None, *, width: int
) -> dict[str, UnitPresence]:
    """Installed units, in ascending unit order.

    Only the first ``width`` items of the presence vector are used.  A unit is
    present when its item is numerically 1.  ``online`` is the ``STATO`` item at
    the same index: 1 -> True, 0 -> False, missing/short/garbage -> ``None``.
    """
    presence = split_csv(presence_csv)[:width]
    status = split_csv(status_csv)
    out: dict[str, UnitPresence] = {}
    for index, item in enumerate(presence):
        if parse_int(item) != 1:
            continue
        state = parse_int(status[index]) if index < len(status) else None
        online = True if state == 1 else False if state == 0 else None
        out[unit_id(index)] = UnitPresence(unit=unit_id(index), index=index, online=online)
    return out
