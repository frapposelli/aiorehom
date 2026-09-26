"""``aiorehom.logic.vmc``: modes, availability, fan and free cooling."""

from __future__ import annotations

import pytest

from aiorehom.enums import Availability, FanKind, FanSpeed, VmcMode, VmcState
from aiorehom.logic import (
    VMC_MODE_AVAILABILITY_KEYS,
    FanState,
    FreeCoolingState,
    VmcOperation,
    mode_availability,
    selectable_modes,
    vmc_fan,
    vmc_free_cooling,
    vmc_operation,
)

A = Availability
#: The fixture's global DEUM rows (Unita "").
FIXTURE_GLOBALS: dict[str, str | None] = {
    "ABILITA_STOP": "2",
    "DEUM_ARIA_NEUTRA": "2",
    "DEUM_INT_FREDDO": "2",
    "ABILITA_INTEGR_FREDDO": "2",
    "ABILITA_INTEGR_CALDO": "0",
    "ABILITA_STAND_BY": "0",
    "ABILITA_RINN_RAP": "2",
    "ABILITA_RISC_RAP": "0",
    "ABILITA_VENTILA": "2",
    "STEP": "0",
}


def test_mode_keys_cover_every_mode() -> None:
    assert list(VMC_MODE_AVAILABILITY_KEYS) == list(VmcMode)


def test_fixture_selectable_modes() -> None:
    avail = mode_availability(FIXTURE_GLOBALS)
    assert selectable_modes(avail) == (
        VmcMode.STOP,
        VmcMode.DEHUMIDIFY,
        VmcMode.DEHUMIDIFY_COOL,
        VmcMode.COOL,
        VmcMode.RAPID_RENEWAL,
        VmcMode.VENTILATE,
    )
    assert avail[VmcMode.HEAT] is A.HIDDEN


def test_missing_row_hides_only_that_mode() -> None:
    """The web UI nulls every mode when one global row is missing; the library does not."""
    globals_ = dict(FIXTURE_GLOBALS)
    del globals_["ABILITA_VENTILA"]
    avail = mode_availability(globals_)
    assert avail[VmcMode.VENTILATE] is A.HIDDEN
    assert avail[VmcMode.STOP] is A.WRITABLE
    assert mode_availability({}) == dict.fromkeys(VmcMode, A.HIDDEN)


def test_availability_levels() -> None:
    avail = mode_availability(
        {"ABILITA_STOP": "3", "DEUM_ARIA_NEUTRA": "abc", "ABILITA_VENTILA": "1"}
    )
    assert avail[VmcMode.STOP] is A.READ_ONLY
    assert avail[VmcMode.DEHUMIDIFY] is A.HIDDEN
    assert avail[VmcMode.VENTILATE] is A.READ_ONLY
    assert selectable_modes(avail) == ()
    assert selectable_modes({}) == ()


@pytest.mark.parametrize(
    ("mode", "forced", "state", "expected"),
    [
        (VmcMode.DEHUMIDIFY, None, VmcState.RUNNING, VmcOperation(VmcMode.DEHUMIDIFY, True, False)),
        (VmcMode.DEHUMIDIFY, None, VmcState.IDLE, VmcOperation(VmcMode.DEHUMIDIFY, False, False)),
        (VmcMode.STOP, None, VmcState.ERROR, VmcOperation(VmcMode.STOP, False, True)),
        (
            VmcMode.DEHUMIDIFY,
            VmcMode.COOL,
            VmcState.FORCED,
            VmcOperation(VmcMode.COOL, True, False),
        ),
        (
            VmcMode.DEHUMIDIFY,
            VmcMode.STOP,
            VmcState.FORCED,
            VmcOperation(VmcMode.STOP, False, False),
        ),
        # FORCED without the optional ST_MODE_FORZATO row: the effective mode falls
        # back to ST_MODE and running follows it
        (
            VmcMode.DEHUMIDIFY,
            None,
            VmcState.FORCED,
            VmcOperation(VmcMode.DEHUMIDIFY, True, False),
        ),
        (VmcMode.STOP, None, VmcState.FORCED, VmcOperation(VmcMode.STOP, False, False)),
        (None, None, VmcState.FORCED, VmcOperation(None, None, False)),
        (
            VmcMode.DEHUMIDIFY,
            VmcMode.COOL,
            VmcState.RUNNING,
            VmcOperation(VmcMode.DEHUMIDIFY, True, False),
        ),
        (VmcMode.DEHUMIDIFY, VmcMode.COOL, None, VmcOperation(VmcMode.DEHUMIDIFY, None, None)),
        (None, None, None, VmcOperation(None, None, None)),
    ],
)
def test_vmc_operation(
    mode: VmcMode | None, forced: VmcMode | None, state: VmcState | None, expected: VmcOperation
) -> None:
    assert vmc_operation(mode=mode, forced_mode=forced, state=state) == expected


def _fan(**raw: str | None) -> FanState:
    args: dict[str, str | None] = {
        "step_raw": "0",
        "value_raw": "1",
        "control_raw": "2",
        "step_min_raw": "0",
        "step_max_raw": "100",
        "step_no_val_raw": "0",
    }
    args.update(raw)
    return vmc_fan(**args)


def test_fan_fixture() -> None:
    assert _fan() == FanState(
        kind=FanKind.DISCRETE,
        control=A.WRITABLE,
        speed=FanSpeed.MIN,
        value=1,
        step_min=0,
        step_max=100,
        show_labels=False,
    )


@pytest.mark.parametrize(
    ("step", "kind"),
    [
        (None, FanKind.DISCRETE),
        ("0", FanKind.DISCRETE),
        ("4,a,b,c,d", FanKind.DISCRETE),
        ("-1", FanKind.CONTINUOUS),
        ("-1.0", FanKind.CONTINUOUS),
    ],
)
def test_fan_kind(step: str | None, kind: FanKind) -> None:
    assert _fan(step_raw=step).kind is kind


def test_continuous_fan_has_no_speed() -> None:
    fan = _fan(step_raw="-1", value_raw="55")
    assert fan.speed is None
    assert fan.value == 55


def test_fan_control_missing_is_read_only() -> None:
    assert _fan(control_raw=None).control is A.READ_ONLY
    assert _fan(control_raw="0").control is A.HIDDEN


def test_fan_defaults() -> None:
    fan = _fan(value_raw=None, step_min_raw=None, step_max_raw="x", step_no_val_raw=None)
    assert fan.value is None
    assert fan.speed is None
    assert (fan.step_min, fan.step_max) == (0, 100)
    assert fan.show_labels is True
    assert _fan(step_max_raw="0").step_max == 0  # "0" is a value, as in the UI
    assert _fan(step_no_val_raw="1").show_labels is True
    assert _fan(value_raw="9").speed is None


@pytest.mark.parametrize(
    ("level", "on", "error", "expected"),
    [
        (None, "0", "0", FreeCoolingState(A.HIDDEN, False, False)),
        ("2", "1", "1", FreeCoolingState(A.WRITABLE, True, True)),
        ("2", "1", "0", FreeCoolingState(A.WRITABLE, True, False)),
        ("1", "1", None, FreeCoolingState(A.READ_ONLY, True, False)),
        ("2", "0", "1", FreeCoolingState(A.WRITABLE, False, False)),
        ("0", None, "1", FreeCoolingState(A.HIDDEN, None, None)),
    ],
)
def test_free_cooling(
    level: str | None, on: str | None, error: str | None, expected: FreeCoolingState
) -> None:
    assert vmc_free_cooling(level_raw=level, on_raw=on, error_raw=error) == expected
