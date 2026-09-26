"""Alarm conditions and the debouncing tracker.

Only known-active conditions are alarms: a missing row is *unknown*, never an
alarm (the UI raises the bus/watchdog alarms on missing rows), and defrost
(``COM_SBRINA``) is not an alarm.  Alarm ids are stable strings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Final

from ..enums import AlarmSource, DeviceKind, LockState
from ..redact import marker_presence
from ..values import parse_bool01, parse_int
from .types import AlarmCondition, AlarmInputs, AlarmView, TrackedAlarm

__all__ = [
    "GENERIC_ALARM_KINDS",
    "MIN_ALARM_DEBOUNCE",
    "VMC_ALARM_KEYS",
    "AlarmTracker",
    "compute_alarms",
    "token_configured",
]

#: VMC alarm flag keys and their text, in this order.
VMC_ALARM_KEYS: Final[Mapping[str, str]] = {
    "ALLARM_ESPANSIONE": "expansion probe",
    "ALLARM_PRESS_ALTA_PRESS": "high-pressure switch",
    "ALLARM_PRESSOSTATO_FILTRO": "dirty filter",
    "ALLARM_SBRINAMENTO": "defrost",
    "ALLARM_SONDA_ANTIGELO": "antifreeze probe",
    "ALLARM_SONDA_LIVELLO": "condensate level",
    "ALLARM_SONDA_RICIRCOLO": "recirculation air probe",
    "ALLARM_SONDA_RINNOVO": "fresh-air probe",
}

#: ``GENERIC_ALARM`` Gruppo -> the kind of unit its ``Unita`` refers to.
GENERIC_ALARM_KINDS: Final[Mapping[str, DeviceKind]] = {
    "ZONA": DeviceKind.ZONE,
    "DEUM": DeviceKind.VMC,
    "AT9091": DeviceKind.FANCOIL,
    "ATT": DeviceKind.ACTUATOR,
}

#: Shortest accepted debounce window.
MIN_ALARM_DEBOUNCE: Final = timedelta(seconds=30)

#: Only codes above this raise a GENERIC/PROCESS alarm (code 14 is the CPU text).
_CODE_THRESHOLD: Final = 100
_FREE_COOLING_TEXT: Final = "free-cooling error"


def token_configured(raw: str | None) -> bool:
    """Whether ``CONFIG.REMOTE_TOKEN`` holds a token.

    ``None``/``""`` -> False.  A redaction marker counts as configured unless it
    records an empty string or a null (``<redacted len=0>``, ``<redacted null>``).
    """
    if not raw:
        return False
    return marker_presence(raw) not in ("empty", "null")


def _unit_alarms(inputs: AlarmInputs) -> Iterable[AlarmCondition]:
    for kind, units in inputs.presence.items():
        for unit, presence in units.items():
            if presence.online is False:
                yield AlarmCondition(
                    id=f"{kind}:{unit}:not_responding",
                    source=AlarmSource.UNIT_NOT_RESPONDING,
                    device=kind,
                    unit=unit,
                    code=None,
                    text=None,
                )


def _vmc_alarms(inputs: AlarmInputs) -> Iterable[AlarmCondition]:
    for unit in inputs.presence.get(DeviceKind.VMC, {}):
        flags = inputs.vmc_flags.get(unit, {})
        for key, text in VMC_ALARM_KEYS.items():
            if parse_int(flags.get(key)) == 1:
                yield AlarmCondition(
                    id=f"vmc:{unit}:{key}",
                    source=AlarmSource.VMC_FLAG,
                    device=DeviceKind.VMC,
                    unit=unit,
                    code=None,
                    text=text,
                )
        on_raw, error_raw = inputs.vmc_free_cooling.get(unit, (None, None))
        if parse_bool01(on_raw) is True and parse_bool01(error_raw) is True:
            yield AlarmCondition(
                id=f"vmc:{unit}:free_cooling_error",
                source=AlarmSource.FREE_COOLING,
                device=DeviceKind.VMC,
                unit=unit,
                code=None,
                text=_FREE_COOLING_TEXT,
            )


def _generic_alarms(inputs: AlarmInputs) -> Iterable[AlarmCondition]:
    for row in inputs.generic:
        kind = GENERIC_ALARM_KINDS.get(row.gruppo)
        code = parse_int(row.subuni)
        if kind is None or row.value == "" or code is None or code <= _CODE_THRESHOLD:
            continue
        if row.unita != "" and row.unita not in inputs.presence.get(kind, {}):
            continue
        yield AlarmCondition(
            id=f"generic:{row.gruppo}:{row.unita}:{code}",
            source=AlarmSource.GENERIC,
            device=kind,
            unit=row.unita or None,
            code=code,
            text=row.value,
        )


def _process_alarms(inputs: AlarmInputs) -> Iterable[AlarmCondition]:
    for unita, value in inputs.process:
        code = parse_int(unita)
        if value == "" or code is None or code <= _CODE_THRESHOLD:
            continue
        yield AlarmCondition(
            id=f"plant:process:{code}",
            source=AlarmSource.PROCESS,
            device=DeviceKind.PLANT,
            unit=None,
            code=code,
            text=value,
        )


def _hub(alarm_id: str, source: AlarmSource) -> AlarmCondition:
    return AlarmCondition(
        id=alarm_id, source=source, device=DeviceKind.HUB, unit=None, code=None, text=None
    )


def _hub_alarms(inputs: AlarmInputs) -> Iterable[AlarmCondition]:
    if parse_int(inputs.bus_raw) == 0:  # inverted: 1 = bus OK
        yield _hub("hub:bus", AlarmSource.BUS)
    if parse_int(inputs.watchdog_raw) == 1:
        yield _hub("hub:watchdog", AlarmSource.WATCHDOG)
    if parse_int(inputs.internet_raw) == 0 and token_configured(inputs.remote_token_raw):
        yield _hub("hub:internet", AlarmSource.INTERNET)
    if inputs.lock is LockState.SERIAL_DOWN:
        yield _hub("hub:serial_line", AlarmSource.SERIAL_LINE)
    if inputs.weather_key_raw is not None and parse_int(inputs.weather_key_raw) != 0:
        yield _hub("hub:weather_service", AlarmSource.WEATHER_SERVICE)


def compute_alarms(inputs: AlarmInputs) -> tuple[AlarmCondition, ...]:
    """Every known-active alarm condition, sorted by ``id`` (duplicate ids: the last wins).

    Not alarms: ``COM_SBRINA`` (defrost), the ``ALLARME`` bitmask,
    crono mode, codes <= 100, and any missing row.
    """
    by_id: dict[str, AlarmCondition] = {}
    for source in (_unit_alarms, _vmc_alarms, _generic_alarms, _process_alarms, _hub_alarms):
        for condition in source(inputs):
            by_id[condition.id] = condition
    return tuple(by_id[alarm_id] for alarm_id in sorted(by_id))


class AlarmTracker:
    """Keeps ``first_seen`` per alarm id and derives the debounced view.

    Both edges are debounced by the same window:

    * **raise**: an alarm is debounced once it has been active for the window.
      ``first_seen`` is kept while an id stays active (a change of text or code
      keeps it); an id that ends before it is debounced is forgotten, so a
      flapping alarm starts a fresh streak.
    * **clear**: a debounced alarm that ends stays debounced (as last seen)
      until it has been inactive for the window.  If it comes back within the
      window, the streak continues with its original ``first_seen``, so a
      short dropout of a real alarm never clears and re-raises it.
    """

    def __init__(self, debounce: timedelta) -> None:
        if debounce < MIN_ALARM_DEBOUNCE:
            raise ValueError("alarm debounce must be at least 30 s")
        self._debounce = debounce
        self._first_seen: dict[str, datetime] = {}
        #: Debounced alarms of the last view, by id (as last seen).
        self._debounced: dict[str, TrackedAlarm] = {}
        #: Debounced alarms that ended: id -> (as last seen, release instant).
        self._held: dict[str, tuple[TrackedAlarm, datetime]] = {}

    @property
    def debounce(self) -> timedelta:
        """The debounce window."""
        return self._debounce

    def update(self, active: Iterable[AlarmCondition], now: datetime) -> AlarmView:
        """Record the currently active conditions at ``now`` and return the view.

        ``active`` holds the active conditions only; ``debounced`` also holds the
        ended alarms still held for the clear debounce; ``next_maturity`` is the
        earliest instant this view changes by time alone (a pending alarm
        matures, or a held one is released).
        """
        by_id = {condition.id: condition for condition in active}
        # A debounced alarm that ended is held from now on.
        for alarm_id, entry in self._debounced.items():
            if alarm_id not in by_id and alarm_id not in self._held:
                self._held[alarm_id] = (entry, now + self._debounce)
        # A held alarm that is back continues its streak; an expired hold ends it.
        resumed: dict[str, datetime] = {}
        for alarm_id, (entry, release) in list(self._held.items()):
            if alarm_id in by_id:
                del self._held[alarm_id]
                resumed[alarm_id] = entry.first_seen
            elif now >= release:
                del self._held[alarm_id]
        self._first_seen = {
            alarm_id: self._first_seen.get(alarm_id) or resumed.get(alarm_id) or now
            for alarm_id in sorted(by_id)
        }
        tracked = tuple(
            TrackedAlarm(condition=by_id[alarm_id], first_seen=first_seen)
            for alarm_id, first_seen in self._first_seen.items()
        )
        debounced: dict[str, TrackedAlarm] = {}
        pending: list[datetime] = []
        for entry in tracked:
            maturity = entry.first_seen + self._debounce
            if now >= maturity:
                debounced[entry.condition.id] = entry
            else:
                pending.append(maturity)
        for alarm_id, (entry, release) in self._held.items():
            debounced[alarm_id] = entry
            pending.append(release)
        self._debounced = debounced
        return AlarmView(
            active=tracked,
            debounced=tuple(debounced[alarm_id] for alarm_id in sorted(debounced)),
            next_maturity=min(pending, default=None),
        )

    def reset(self) -> None:
        """Forget every streak and every held alarm."""
        self._first_seen = {}
        self._debounced = {}
        self._held = {}
