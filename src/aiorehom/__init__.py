"""aiorehom: async client for the Rehom RadiaxWeb local API.

:class:`RehomClient` keeps a live, immutable :class:`RehomState` of one
controller: a WebSocket-first sync (snapshot, then live frames), batched
:class:`StateUpdate` notifications, and time-driven rebuilds at
``state.next_change_at``.  By default it is read-only: only allowlisted GETs, at
most one login, and a receive-only WebSocket.  Writes are opt-in
(``allow_writes=True``): each typed ``set_*`` call is planned against the
current state (:mod:`aiorehom.writes`), sent once through the transport's write
gate, and confirmed from the controller's own reports.

The probe (``rehom-probe``) ships alongside: the allowlisted transport, secret
redaction, a normalised record store, a listen-only WebSocket capture, an
offline replay of captures (``rehom-probe replay``) and supervised write tests
(``rehom-probe write-test``).
"""

from __future__ import annotations

__version__ = "0.3.0"

from .client import ClientOptions, RehomClient
from .clock import DeviceClock
from .enums import (
    AlarmSource,
    Availability,
    ConnectionState,
    ControlSource,
    DeviceKind,
    FanKind,
    FanSpeed,
    HistorySeries,
    HvacAction,
    Level,
    LockState,
    MasterMode,
    MasterPreset,
    MasterSetPoint,
    ProgramSource,
    ScheduleSource,
    Season,
    UpdateReason,
    VmcMode,
    VmcScheduleLevel,
    VmcState,
    ZoneMode,
    ZonePreset,
    ZoneSetp,
)
from .exceptions import (
    CredentialsError,
    ForbiddenRequestError,
    RehomAuthenticationError,
    RehomConnectionError,
    RehomError,
    RehomHttpError,
    RehomNotReadyError,
    RehomRedirectError,
    RehomResponseError,
    RehomTimeoutError,
    RehomWriteNotConfirmedError,
    RehomWriteRefusedError,
)
from .models import (
    Actuator,
    Alarm,
    DayProgram,
    Fancoil,
    Forecast,
    ForecastEntry,
    Health,
    HistorySample,
    Hub,
    Plant,
    Program,
    RehomState,
    SeasonSchedule,
    StateUpdate,
    SyncStats,
    UnitIdentity,
    Vmc,
    VmcFan,
    VmcFreeCooling,
    Weather,
    WeatherDay,
    Zone,
    ZoneOverride,
    ZoneSchedule,
)
from .store import RecordStore, diff, make_path, norm, record_key
from .transport import ReadOnlyTransport

__all__ = [
    "Actuator",
    "Alarm",
    "AlarmSource",
    "Availability",
    "ClientOptions",
    "ConnectionState",
    "ControlSource",
    "CredentialsError",
    "DayProgram",
    "DeviceClock",
    "DeviceKind",
    "FanKind",
    "FanSpeed",
    "Fancoil",
    "ForbiddenRequestError",
    "Forecast",
    "ForecastEntry",
    "Health",
    "HistorySample",
    "HistorySeries",
    "Hub",
    "HvacAction",
    "Level",
    "LockState",
    "MasterMode",
    "MasterPreset",
    "MasterSetPoint",
    "Plant",
    "Program",
    "ProgramSource",
    "ReadOnlyTransport",
    "RecordStore",
    "RehomAuthenticationError",
    "RehomClient",
    "RehomConnectionError",
    "RehomError",
    "RehomHttpError",
    "RehomNotReadyError",
    "RehomRedirectError",
    "RehomResponseError",
    "RehomState",
    "RehomTimeoutError",
    "RehomWriteNotConfirmedError",
    "RehomWriteRefusedError",
    "ScheduleSource",
    "Season",
    "SeasonSchedule",
    "StateUpdate",
    "SyncStats",
    "UnitIdentity",
    "UpdateReason",
    "Vmc",
    "VmcFan",
    "VmcFreeCooling",
    "VmcMode",
    "VmcScheduleLevel",
    "VmcState",
    "Weather",
    "WeatherDay",
    "Zone",
    "ZoneMode",
    "ZoneOverride",
    "ZonePreset",
    "ZoneSchedule",
    "ZoneSetp",
    "__version__",
    "diff",
    "make_path",
    "norm",
    "record_key",
]
