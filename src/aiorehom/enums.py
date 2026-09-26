"""Public enumerations (contract file: members and values are public API).

Unknown wire values parse to ``None`` (see aiorehom.logic), never to a member.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Season(StrEnum):  # REHOM.STAGIONE: "0" winter, "1" summer
    WINTER = "winter"
    SUMMER = "summer"


class MasterMode(IntEnum):  # REHOM.MODO
    OFF = 0
    MANUAL = 1
    AUTO = 2


class MasterSetPoint(IntEnum):  # REHOM.SET_POINT
    UNSET = 0
    ECONOMY = 1
    PRE_COMFORT = 2
    COMFORT = 3


class MasterPreset(StrEnum):  # decoded whole-house state
    OFF = "off"
    ECONOMY = "economy"
    PRE_COMFORT = "pre_comfort"
    COMFORT = "comfort"
    AUTO = "auto"


class Level(IntEnum):  # zone schedule slot level (PROG_GIORNO_*, PROG_OVERRIDE)
    OFF = 0
    ECONOMY = 1
    PRE_COMFORT = 2
    COMFORT = 3


class ZoneSetp(IntEnum):  # ZONA.<z>..SETP_CORRENTE (a level, not a temperature)
    UNSET = 0
    OFF = 1
    ECONOMY = 2
    PRE_COMFORT = 3
    COMFORT = 4
    PROBE_OFF = 5


class ZoneMode(StrEnum):  # effective zone mode
    OFF = "off"
    AUTO = "auto"
    MANUAL = "manual"


class ZonePreset(StrEnum):  # effective zone preset
    NONE = "none"
    ECONOMY = "economy"
    PRE_COMFORT = "pre_comfort"
    COMFORT = "comfort"
    TEMPORARY_COMFORT = "temporary_comfort"


class ControlSource(StrEnum):  # why a zone's level/target is what it is
    SCHEDULE = "schedule"
    ZONE = "zone"
    OVERRIDE = "override"
    HOUSE = "house"
    CRONO = "crono"
    PROBE = "probe"
    EXTERNAL = "external"


class ProgramSource(StrEnum):  # branch of the running-program calculation
    HOUSE_OFF = "house_off"
    PROBE_OFF = "probe_off"
    HOUSE_MANUAL = "house_manual"
    CRONO = "crono"
    SCHEDULE = "schedule"
    ZONE_MANUAL = "zone_manual"


class ScheduleSource(StrEnum):  # where a zone's schedule rows came from
    PROG = "prog"
    ZONA_MIRROR = "zona_mirror"
    NONE = "none"


class HvacAction(StrEnum):
    OFF = "off"
    IDLE = "idle"
    HEATING = "heating"
    COOLING = "cooling"


class LockState(StrEnum):  # Key == "WEBSERVER": 0 crono, 2 serial down, else normal
    NORMAL = "normal"
    CRONO = "crono"
    SERIAL_DOWN = "serial_down"


class VmcMode(IntEnum):  # DEUM.<d>..ST_MODE / ST_MODE_FORZATO
    STOP = 0
    DEHUMIDIFY = 1
    DEHUMIDIFY_COOL = 2
    COOL = 3
    HEAT = 4
    STANDBY = 5
    RAPID_RENEWAL = 6
    RAPID_HEAT = 7
    VENTILATE = 8


class VmcState(IntEnum):  # DEUM.<d>..ST_STATO_DEUM
    IDLE = 0
    RUNNING = 1
    ERROR = 2
    FORCED = 3


class VmcScheduleLevel(IntEnum):  # DEUM PROG_GIORNO slot (inverted vs zones)
    MAX = 0
    MIN = 1
    OFF = 2


class Availability(IntEnum):  # ABILITA_* / DEUM_ARIA_NEUTRA / DEUM_INT_FREDDO levels
    HIDDEN = 0
    READ_ONLY = 1
    WRITABLE = 2


class FanKind(StrEnum):  # global DEUM..STEP: "-1" continuous, else discrete
    DISCRETE = "discrete"
    CONTINUOUS = "continuous"


class FanSpeed(IntEnum):  # discrete COM_VENTILA
    NONE = 0
    MIN = 1
    MED = 2
    MAX = 3
    ATTENUATED = 4


class DeviceKind(StrEnum):
    HUB = "hub"
    PLANT = "plant"
    ZONE = "zone"
    VMC = "vmc"
    ACTUATOR = "actuator"
    FANCOIL = "fancoil"


class AlarmSource(StrEnum):  # what raised an alarm condition
    UNIT_NOT_RESPONDING = "unit_not_responding"
    VMC_FLAG = "vmc_flag"
    FREE_COOLING = "free_cooling"
    GENERIC = "generic"
    PROCESS = "process"
    BUS = "bus"
    WATCHDOG = "watchdog"
    INTERNET = "internet"
    SERIAL_LINE = "serial_line"
    WEATHER_SERVICE = "weather_service"


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"


class UpdateReason(StrEnum):
    SYNC = "sync"
    FRAMES = "frames"
    RESYNC = "resync"
    FALLBACK = "fallback"
    CONFIG = "config"
    CLOCK = "clock"


class HistorySeries(StrEnum):  # GET /api/history/ Key
    CONT_CALORIE = "CONT_CALORIE"
    CONT_FRIGORIE = "CONT_FRIGORIE"
    CONT_ACQUA_CALDA = "CONT_ACQUA_CALDA"
    CONT_ACQUA_FREDDA = "CONT_ACQUA_FREDDA"
    TEMP_ESTERNA = "TEMP_ESTERNA"
    UMIDITA_ESTERNA = "UMIDITA_ESTERNA"
    TEMP_AMBIENTE = "TEMP_AMBIENTE"
    SETP_CORRENTE = "SETP_CORRENTE"
    UMIDITA = "UMIDITA"

    @property
    def per_zone(self) -> bool:
        """True when ``Unita`` is a zone id; otherwise ``Unita`` is ``""``."""
        return self in _PER_ZONE_SERIES


_PER_ZONE_SERIES: frozenset[HistorySeries] = frozenset(
    {HistorySeries.TEMP_AMBIENTE, HistorySeries.SETP_CORRENTE, HistorySeries.UMIDITA}
)
