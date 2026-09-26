"""VMC / dehumidifier: modes, availability, fan and free cooling."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from ..enums import Availability, FanKind, VmcMode, VmcState
from ..values import parse_bool01, parse_int, values_equal
from .parse import parse_availability, parse_fan_speed
from .types import FanState, FreeCoolingState, VmcOperation

__all__ = [
    "VMC_MODE_AVAILABILITY_KEYS",
    "mode_availability",
    "selectable_modes",
    "vmc_fan",
    "vmc_free_cooling",
    "vmc_operation",
]

#: Global ``DEUM`` row (Unita ``""``) holding each mode's availability level.
VMC_MODE_AVAILABILITY_KEYS: Final[Mapping[VmcMode, str]] = {
    VmcMode.STOP: "ABILITA_STOP",
    VmcMode.DEHUMIDIFY: "DEUM_ARIA_NEUTRA",
    VmcMode.DEHUMIDIFY_COOL: "DEUM_INT_FREDDO",
    VmcMode.COOL: "ABILITA_INTEGR_FREDDO",
    VmcMode.HEAT: "ABILITA_INTEGR_CALDO",
    VmcMode.STANDBY: "ABILITA_STAND_BY",
    VmcMode.RAPID_RENEWAL: "ABILITA_RINN_RAP",
    VmcMode.RAPID_HEAT: "ABILITA_RISC_RAP",
    VmcMode.VENTILATE: "ABILITA_VENTILA",
}

_STEP_MIN_DEFAULT: Final = 0
_STEP_MAX_DEFAULT: Final = 100


def mode_availability(globals_: Mapping[str, str | None]) -> dict[VmcMode, Availability]:
    """Availability of all 9 modes; a missing row is HIDDEN for that mode only."""
    return {
        mode: parse_availability(globals_.get(key), missing=Availability.HIDDEN)
        for mode, key in VMC_MODE_AVAILABILITY_KEYS.items()
    }


def selectable_modes(avail: Mapping[VmcMode, Availability]) -> tuple[VmcMode, ...]:
    """The WRITABLE modes, in enum order."""
    return tuple(mode for mode in VmcMode if avail.get(mode) is Availability.WRITABLE)


def vmc_operation(
    *, mode: VmcMode | None, forced_mode: VmcMode | None, state: VmcState | None
) -> VmcOperation:
    """Effective mode, running and error flags.

    * The effective mode is ``forced_mode`` while FORCED, else ``mode``.  A
      FORCED unit without an ``ST_MODE_FORZATO`` row (it is optional and absent
      on the reference plant) falls back to ``mode``.
    * ``running``: ``None`` if the state is unknown; RUNNING is running; FORCED
      is running when the effective mode is known and not STOP (``None`` when it
      is unknown).  Deviation: the UI reports a FORCED unit with no forced-mode
      row as running even when ``ST_MODE`` is STOP.
    * ``error``: the state is ERROR (``None`` if the state is unknown).
    """
    forced = state is VmcState.FORCED
    effective = forced_mode if forced and forced_mode is not None else mode
    if state is None:
        return VmcOperation(effective_mode=effective, running=None, error=None)
    running: bool | None
    if forced:
        running = None if effective is None else effective is not VmcMode.STOP
    else:
        running = state is VmcState.RUNNING
    return VmcOperation(effective_mode=effective, running=running, error=state is VmcState.ERROR)


def vmc_fan(
    *,
    step_raw: str | None,
    value_raw: str | None,
    control_raw: str | None,
    step_min_raw: str | None,
    step_max_raw: str | None,
    step_no_val_raw: str | None,
) -> FanState:
    """Decode the fan.

    Global ``STEP == -1`` means continuous, anything else (a missing row too)
    discrete.  A missing ``ABILITA_VENTOLA`` is READ_ONLY.  Missing
    or unparseable bounds default to 0..100 (``"0"`` stays 0, as in the UI).
    """
    kind = FanKind.CONTINUOUS if values_equal(step_raw, "-1") else FanKind.DISCRETE
    step_min = parse_int(step_min_raw)
    step_max = parse_int(step_max_raw)
    return FanState(
        kind=kind,
        control=parse_availability(control_raw, missing=Availability.READ_ONLY),
        speed=parse_fan_speed(value_raw) if kind is FanKind.DISCRETE else None,
        value=parse_int(value_raw),
        step_min=_STEP_MIN_DEFAULT if step_min is None else step_min,
        step_max=_STEP_MAX_DEFAULT if step_max is None else step_max,
        show_labels=parse_int(step_no_val_raw) != 0,
    )


def vmc_free_cooling(
    *, level_raw: str | None, on_raw: str | None, error_raw: str | None
) -> FreeCoolingState:
    """Free cooling: global level (missing = HIDDEN), on flag, and error while on."""
    on = parse_bool01(on_raw)
    error = None if on is None else (on and parse_bool01(error_raw) is True)
    return FreeCoolingState(
        level=parse_availability(level_raw, missing=Availability.HIDDEN), on=on, error=error
    )
