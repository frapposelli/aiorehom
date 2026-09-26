"""Read-only capture session (``rehom-probe session``, steps 1-8).

Every response is redacted before it is written; nothing unredacted touches
disk.  All HTTP goes through :class:`~aiorehom.transport.ReadOnlyTransport`.
"""

from __future__ import annotations

import platform
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp

from . import __version__
from . import credentials as _credentials
from ._files import JsonlWriter, utc_now_iso, write_json, write_private_text
from .credentials import DEFAULT_SERVICE
from .exceptions import CredentialsError, ForbiddenRequestError, RehomError
from .redact import redact_dict, redact_records
from .store import RecordStore
from .transport import DOMOTICA_RESOURCES, ReadOnlyTransport

__all__ = [
    "ALL_STEPS",
    "HISTORY_WINDOW",
    "ProbeSession",
    "controller_now",
    "first_zone_id",
    "records_tsv",
]

ALL_STEPS: Final = (1, 2, 3, 4, 5, 6, 7, 8)
AUTH_STEPS: Final = frozenset({2, 3, 4, 5, 6, 7, 8})
STEP_NAMES: Final = {
    1: "alive",
    2: "login + me",
    3: "config",
    4: "interface",
    5: "overrides + plant conf",
    6: "domotica",
    7: "history",
    8: "filtered interface",
}
HISTORY_WINDOW: Final = timedelta(hours=6)
PLANT_SERIES: Final = ("CONT_CALORIE", "CONT_ACQUA_CALDA")
ZONE_SERIES: Final = ("TEMP_AMBIENTE", "SETP_CORRENTE")
OUTDOOR_SERIES: Final = ("TEMP_ESTERNA",)
TSV_COLUMNS: Final = (
    "store",
    "Gruppo",
    "Unita",
    "SubUni",
    "Key",
    "Valore",
    "Stato",
    "Flusso",
    "path",
    "path_received",
)

CredentialsReader = Callable[..., Awaitable[tuple[str, str]]]
_T = TypeVar("_T")


def _tsv_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def records_tsv(stores: Mapping[str, RecordStore]) -> str:
    """Render normalised stores as TSV (one header line, rows sorted by identity)."""
    lines = ["\t".join(TSV_COLUMNS)]
    for store_name in sorted(stores):
        for row in stores[store_name].to_rows():
            cells = [store_name] + [_tsv_cell(row.get(col)) for col in TSV_COLUMNS[1:]]
            lines.append("\t".join(cells))
    return "\n".join(lines) + "\n"


def controller_now(config: object) -> tuple[datetime, str] | None:
    """Controller wall-clock "now" (naive) from ``/api/config/``.

    Uses ``LOCAL_TIME`` (converted to ``TIMEZONE`` if it carries an offset);
    falls back to the host clock in ``TIMEZONE``; ``None`` if neither works.
    """
    if not isinstance(config, Mapping):
        return None
    tz: ZoneInfo | None = None
    tz_name = config.get("TIMEZONE")
    if isinstance(tz_name, str) and tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    local_time = config.get("LOCAL_TIME")
    if isinstance(local_time, str) and local_time.strip():
        text = local_time.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is not None:
                if tz is not None:
                    parsed = parsed.astimezone(tz)
                parsed = parsed.replace(tzinfo=None)
            return parsed.replace(microsecond=0), "LOCAL_TIME"
    if tz is not None:
        return datetime.now(tz).replace(tzinfo=None, microsecond=0), "host clock in TIMEZONE"
    return None


def first_zone_id(rows: Iterable[Mapping[str, Any]]) -> str | None:
    """First present zone id: from ``REHOM.PRESENZA_SONDE``, else the lowest ZONA unit."""
    store = RecordStore(rows)
    presence = store.value("REHOM", "", "", "PRESENZA_SONDE")
    if presence:
        for index, flag in enumerate(presence.split(",")):
            if flag.strip() == "1":
                return f"{index + 1:03d}"
    units = sorted(
        {
            row["Unita"]
            for row in store.to_rows()
            if row["Gruppo"] == "ZONA"
            and row["Unita"] not in ("", "000")
            and row["Unita"].isdigit()
        }
    )
    return units[0] if units else None


@dataclass
class StepResult:
    number: int
    name: str
    status: str = "pending"
    files: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "files": list(self.files),
            "detail": list(self.detail),
        }


class ProbeSession:
    """Run the capture steps and write redacted results to ``out_dir``."""

    def __init__(
        self,
        transport: ReadOnlyTransport,
        out_dir: Path,
        *,
        login: bool = True,
        username: str | None = None,
        service: str = DEFAULT_SERVICE,
        credentials_reader: CredentialsReader | None = None,
        echo: Callable[[str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.out_dir = out_dir
        self.login_enabled = login
        self._username = username
        self._service = service
        self._read_credentials = credentials_reader or _credentials.read_keychain_credentials
        self._echo = echo or (lambda _msg: None)
        self._login_state = "not_attempted"
        self._login_error: str | None = None
        self.redaction_counts: dict[str, int] = {}
        self.steps: dict[int, StepResult] = {}
        self.config: Any = None
        self.interface: list[Any] | None = None
        self.overrides: list[Any] | None = None
        self.history_window: dict[str, str] | None = None

    # -- helpers ----------------------------------------------------------------

    def _save(self, name: str, data: Any, kind: str, step: StepResult) -> Any:
        if kind == "records":
            redacted, count = redact_records(data)
        else:
            redacted, count = redact_dict(data)
        write_json(self.out_dir / name, redacted)
        self.redaction_counts[name] = count
        step.files.append(name)
        return redacted

    async def _ensure_login(self) -> bool:
        if self._login_state == "ok":
            return True
        if self._login_state != "not_attempted":
            return False
        if not self.login_enabled:
            self._login_state = "disabled"
            return False
        self._login_state = "failed"
        try:
            username, password = await self._read_credentials(
                self._service, username=self._username
            )
        except CredentialsError as err:
            self._login_error = f"credentials: {err}"
            return False
        try:
            await self.transport.login(username, password)
        except ForbiddenRequestError:
            raise
        except RehomError as err:
            status = getattr(err, "status", None)
            self._login_error = f"login failed: {type(err).__name__}" + (
                f" (HTTP {status})" if status is not None else ""
            )
            return False
        finally:
            del password
        self._login_state = "ok"
        return True

    async def _attempt(
        self, step: StepResult, label: str, call: Callable[[], Awaitable[_T]]
    ) -> tuple[bool, _T | None]:
        """Run one sub-request of a step; a failure marks the step but does not end it.

        Returns ``(True, result)`` or ``(False, None)``.  Allowlist refusals
        still abort the whole session.
        """
        try:
            return True, await call()
        except ForbiddenRequestError:
            raise
        except RehomError as err:
            step.status = "error"
            step.detail.append(f"{label}: {type(err).__name__}: {err}")
            return False, None

    def _skip_reason(self) -> str:
        if self._login_state == "disabled":
            return "skipped: --no-login"
        return f"skipped: not logged in ({self._login_error or 'login failed'})"

    # -- steps --------------------------------------------------------------------

    async def _step1(self, step: StepResult) -> None:
        self._save("alive.json", await self.transport.get_alive(), "dict", step)

    async def _step2(self, step: StepResult) -> None:
        self._save("me.json", await self.transport.get_me(), "dict", step)

    async def _step3(self, step: StepResult) -> None:
        self.config = self._save("config.json", await self.transport.get_config(), "dict", step)

    async def _step4(self, step: StepResult) -> None:
        data = self._save("interface.json", await self.transport.get_interface(), "records", step)
        self.interface = data if isinstance(data, list) else None
        if self.interface is None:
            step.detail.append("interface response is not a list")

    async def _step5(self, step: StepResult) -> None:
        ok, data = await self._attempt(step, "overrides", self.transport.get_overrides)
        if ok:
            saved = self._save("overrides.json", data, "records", step)
            self.overrides = saved if isinstance(saved, list) else None
        ok, data = await self._attempt(step, "plant/conf", self.transport.get_plant_conf)
        if ok:
            self._save("plant_conf.json", data, "dict", step)

    async def _step6(self, step: StepResult) -> None:
        for resource in DOMOTICA_RESOURCES:
            try:
                data = await self.transport.get_domotica(resource)
            except ForbiddenRequestError:
                raise
            except RehomError as err:
                step.detail.append(f"domotica/{resource}: {err}")
                continue
            self._save(f"domotica_{resource}.json", data, "dict", step)

    async def _step7(self, step: StepResult) -> None:
        now = controller_now(self.config)
        if now is None:
            step.status = "skipped"
            step.detail.append("skipped: needs step 3 (/api/config/ LOCAL_TIME or TIMEZONE)")
            return
        lte, source = now
        gte = lte - HISTORY_WINDOW
        self.history_window = {
            "Tempo__gte": gte.strftime("%Y-%m-%d %H:%M:%S"),
            "Tempo__lte": lte.strftime("%Y-%m-%d %H:%M:%S"),
            "source": source,
        }
        zone = first_zone_id(self.interface) if self.interface else None
        series: list[tuple[str, str]] = [(key, "") for key in PLANT_SERIES]
        if zone is not None:
            series += [(key, zone) for key in ZONE_SERIES]
        else:
            step.detail.append("zone series skipped: no zone known (needs step 4)")
        series += [(key, "") for key in OUTDOOR_SERIES]
        for key, unita in series:
            try:
                data = await self.transport.get_history(key, unita, gte, lte)
            except ForbiddenRequestError:
                raise
            except RehomError as err:
                step.detail.append(f"history {key}/{unita or '-'}: {err}")
                continue
            self._save(f"history_{key}_{unita}.json", data, "dict", step)

    async def _step8(self, step: StepResult) -> None:
        zone = first_zone_id(self.interface) if self.interface else None
        if zone is not None:
            ok, data = await self._attempt(
                step,
                f"?Gruppo=ZONA&Unita={zone}",
                lambda: self.transport.get_interface(Gruppo="ZONA", Unita=zone),
            )
            if ok:
                self._save(f"interface_filtered_zona_{zone}.json", data, "records", step)
        else:
            step.detail.append("?Gruppo=ZONA&Unita=... skipped: no zone known (needs step 4)")
        ok, data = await self._attempt(
            step,
            "?Key__in=STAGIONE,MODO",
            lambda: self.transport.get_interface(Key__in=["STAGIONE", "MODO"]),
        )
        if ok:
            self._save("interface_filtered_key_in.json", data, "records", step)

    # -- driver ---------------------------------------------------------------------

    async def run(self, steps: Sequence[int] = ALL_STEPS) -> dict[str, Any]:
        """Run ``steps`` in ascending order; always writes meta/requests/records files."""
        started = utc_now_iso()
        wanted = sorted(set(steps))
        runners = {
            1: self._step1,
            2: self._step2,
            3: self._step3,
            4: self._step4,
            5: self._step5,
            6: self._step6,
            7: self._step7,
            8: self._step8,
        }
        try:
            for number in wanted:
                step = StepResult(number, STEP_NAMES[number])
                self.steps[number] = step
                if number in AUTH_STEPS and not await self._ensure_login():
                    step.status = "skipped"
                    step.detail.append(self._skip_reason())
                    self._echo(f"step {number} ({step.name}): {step.detail[-1]}")
                    continue
                try:
                    await runners[number](step)
                except ForbiddenRequestError:
                    step.status = "error"
                    step.detail.append("aborted: request refused by the allowlist")
                    raise
                except RehomError as err:
                    step.status = "error"
                    step.detail.append(f"{type(err).__name__}: {err}")
                else:
                    if step.status == "pending":
                        step.status = "ok"
                self._echo(
                    f"step {number} ({step.name}): {step.status}"
                    + (f" [{', '.join(step.files)}]" if step.files else "")
                    + ("".join(f"\n    {d}" for d in step.detail) if step.detail else "")
                )
        finally:
            meta = self._write_outputs(started, wanted)
        return meta

    def _write_outputs(self, started: str, wanted: list[int]) -> dict[str, Any]:
        with JsonlWriter(self.out_dir / "requests.jsonl") as log:
            for entry in self.transport.request_log:
                log.write(entry)
        stores: dict[str, RecordStore] = {}
        if self.interface is not None:
            stores["interface"] = RecordStore(r for r in self.interface if isinstance(r, Mapping))
        if self.overrides is not None:
            stores["overrides"] = RecordStore(r for r in self.overrides if isinstance(r, Mapping))
        if stores:
            write_private_text(self.out_dir / "records.tsv", records_tsv(stores))
        meta: dict[str, Any] = {
            "tool": "rehom-probe",
            "aiorehom_version": __version__,
            "python": platform.python_version(),
            "aiohttp": aiohttp.__version__,
            "host": self.transport.host,
            "port": self.transport.port,
            "started_utc": started,
            "finished_utc": utc_now_iso(),
            "steps_requested": wanted,
            "steps": {str(n): s.to_dict() for n, s in sorted(self.steps.items())},
            "login": {
                "enabled": self.login_enabled,
                "state": self._login_state,
                "attempted": self.transport.login_attempted,
                "username_source": None
                if not self.login_enabled
                else ("override" if self._username is not None else "keychain"),
                "error": self._login_error,
            },
            "history_window": self.history_window,
            "records": {
                name: {"rows": len(st), "raw_rows": st.raw_rows, "duplicates": st.duplicates}
                for name, st in sorted(stores.items())
            },
            "requests": len(self.transport.request_log),
            "redaction_counts": dict(sorted(self.redaction_counts.items())),
            "redaction_total": sum(self.redaction_counts.values()),
        }
        write_json(self.out_dir / "meta.json", meta)
        return meta
