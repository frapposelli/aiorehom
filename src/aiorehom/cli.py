"""``rehom-probe``: probe for the Rehom RadiaxWeb local API.

Every subcommand is read-only except ``write-test --execute``, which sends one
supervised write, verifies it and reverts it.

No subcommand can issue a request outside the transport allowlist, and there
is no raw-request command.  Output files are written with mode 0o600 and
directories with mode 0o700.

Default locations are derived at run time, never hard-coded: captures go to
``$REHOM_PROBE_CAPTURE_ROOT``, else ``./captures``.  The optional record
catalogue is ``--catalog``, else ``$REHOM_PROBE_CATALOG``.  The write-test
journal is ``--journal``, else ``$REHOM_WRITE_JOURNAL``, else the per-user
``$XDG_STATE_HOME/aiorehom/write-tests/journal.jsonl`` (absolute, so every run
sees the same journal whatever the working directory).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import os
import secrets
import signal
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any, Final

from . import __version__
from ._files import (
    JsonlWriter,
    dump_json,
    ensure_private_dir,
    read_json,
    utc_now_iso,
    utc_stamp,
    write_json,
    write_private_text,
)
from .client import ClientOptions, RehomClient
from .clock import VirtualClock
from .credentials import DEFAULT_SERVICE, read_keychain_credentials
from .enums import UpdateReason
from .exceptions import ForbiddenRequestError, RehomError
from .fixtures import sanitize_capture
from .inventory import (
    build_inventory,
    diff_captures,
    format_diff,
    format_inventory,
    load_capture,
    load_volatile_patterns,
    mask_diff,
    stores_not_compared,
)
from .models import StateUpdate, SyncStats
from .probe import ALL_STEPS, ProbeSession
from .redact import redact_har
from .replay import (
    ABSENT,
    ReplayDevice,
    ReplayDriver,
    ReplayError,
    ScaledClock,
    diff_flat,
    flatten_state,
    format_value,
    mask_flat,
    masked_records,
    update_alarm_ids,
)
from .transport import ReadOnlyTransport
from .websocket import listen_only
from .writetest import (
    OPERATIONS,
    VMC_MODE_DWELL_S,
    WriteTestError,
    build_operation,
    check_arguments,
    close_entry,
    open_entries,
    run_write_test,
)

DEFAULT_HOST: Final = "rehomserver.local"
DEFAULT_PORT: Final = 8000
CAPTURE_ROOT_ENV: Final = "REHOM_PROBE_CAPTURE_ROOT"
CATALOG_ENV: Final = "REHOM_PROBE_CATALOG"
WRITE_JOURNAL_ENV: Final = "REHOM_WRITE_JOURNAL"
WRITE_TEST_MAX_DWELL_S: Final = 3600.0
SESSION_MIN_INTERVAL_S: Final = 1.0
WATCH_MAX_MINUTES: Final = 120
LATENCY_MAX_SECONDS: Final = 600
WATCH_ALIVE_PERIOD_S: Final = 15.0
LATENCY_PERIOD_S: Final = 2.0
REPLAY_MAX_SPEED: Final = 1000.0
#: Host name handed to the replayed client (never resolved: the transport is injected).
REPLAY_HOST: Final = "replay.invalid"
#: Text output lists at most this many model changes per update.
REPLAY_TEXT_MAX_CHANGES: Final = 12


# ---------------------------------------------------------------------------
# Default locations (computed at run time)
# ---------------------------------------------------------------------------


def default_capture_root() -> Path:
    """``$REHOM_PROBE_CAPTURE_ROOT``, else ``./captures``."""
    env = os.environ.get(CAPTURE_ROOT_ENV)
    return Path(env) if env else Path.cwd() / "captures"


def default_catalog() -> Path | None:
    """``$REHOM_PROBE_CATALOG``, else ``None`` (coverage is skipped)."""
    env = os.environ.get(CATALOG_ENV)
    return Path(env) if env else None


def default_write_journal() -> Path:
    """``$REHOM_WRITE_JOURNAL``, else ``$XDG_STATE_HOME/aiorehom/write-tests/journal.jsonl``.

    ``XDG_STATE_HOME`` defaults to ``~/.local/state`` (a relative value is
    ignored, as the XDG spec requires).  The result is absolute; a relative
    ``$REHOM_WRITE_JOURNAL`` is refused, since it would make the open-entry
    interlock depend on the working directory.
    """
    env = os.environ.get(WRITE_JOURNAL_ENV)
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            raise ValueError(f"${WRITE_JOURNAL_ENV} must be an absolute path")
        return path.resolve()
    state_home = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    if not state_home.is_absolute():
        state_home = Path.home() / ".local" / "state"
    return (state_home / "aiorehom" / "write-tests" / "journal.jsonl").resolve()


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _bounded(
    kind: Callable[[str], float], low: float, high: float, name: str
) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name} must be a number") from None
        if not low < value <= high:
            raise argparse.ArgumentTypeError(f"{name} must be > {low:g} and <= {high:g}")
        return value

    return parse


def _parse_steps(text: str) -> list[int]:
    steps: list[int] = []
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                raise argparse.ArgumentTypeError(f"invalid step range {part!r}") from None
            steps.extend(range(start, end + 1))
        else:
            try:
                steps.append(int(part))
            except ValueError:
                raise argparse.ArgumentTypeError(f"invalid step {part!r}") from None
    if not steps or any(s not in ALL_STEPS for s in steps):
        raise argparse.ArgumentTypeError(f"steps must be numbers between 1 and {ALL_STEPS[-1]}")
    return sorted(set(steps))


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


def _dwell(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--dwell must be a number of seconds") from None
    if not math.isfinite(value) or not 1 <= value <= WRITE_TEST_MAX_DWELL_S:
        raise argparse.ArgumentTypeError(
            f"--dwell must be between 1 and {WRITE_TEST_MAX_DWELL_S:g} seconds"
        )
    return value


def _hms(text: str) -> dt_time:
    try:
        value = dt_time.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--until must be HH:MM:SS (UTC)") from None
    if value.tzinfo is not None:
        raise argparse.ArgumentTypeError("--until takes a UTC time without an offset")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rehom-probe",
        description=(
            "Probe for the Rehom RadiaxWeb local API. Only allowlisted requests, one "
            "optional login and a listen-only WebSocket; read-only except "
            "'write-test --execute'; everything saved is redacted."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"controller host (default {DEFAULT_HOST})"
    )
    parser.add_argument("--port", type=_port, default=DEFAULT_PORT, help="API port (default 8000)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            f"output directory (default ${CAPTURE_ROOT_ENV}/<UTC timestamp>/, else "
            "./captures/<UTC timestamp>/)"
        ),
    )
    parser.add_argument("--username", default=None, help="override the Keychain account name")
    parser.add_argument(
        "--service", default=DEFAULT_SERVICE, help=f"Keychain service (default {DEFAULT_SERVICE})"
    )
    parser.add_argument(
        "--no-login", action="store_true", help="never log in; skip steps that need a token"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    session = sub.add_parser("session", help=f"run the read-only capture steps 1-{ALL_STEPS[-1]}")
    session.add_argument(
        "--steps",
        type=_parse_steps,
        default=list(ALL_STEPS),
        help=f"comma-separated steps or ranges, e.g. 1,3-5 (default 1-{ALL_STEPS[-1]})",
    )

    watch = sub.add_parser("watch", help="listen-only WebSocket capture to ws.jsonl")
    watch.add_argument(
        "--minutes",
        type=_bounded(float, 0, WATCH_MAX_MINUTES, "--minutes"),
        default=30.0,
        help=f"capture duration (default 30, max {WATCH_MAX_MINUTES})",
    )
    watch.add_argument(
        "--with-latency",
        action="store_true",
        help="also poll /api/alive/ every 15 s and log latency to alive_latency.jsonl",
    )

    latency = sub.add_parser("latency", help="poll /api/alive/ every 2 s and log latency")
    latency.add_argument(
        "--seconds",
        type=_bounded(float, 0, LATENCY_MAX_SECONDS, "--seconds"),
        default=120.0,
        help=f"duration (default 120, max {LATENCY_MAX_SECONDS})",
    )

    inventory = sub.add_parser("inventory", help="summarise a capture directory (offline)")
    inventory.add_argument("capture_dir", type=Path)
    inventory.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help=f"record catalogue (default ${CATALOG_ENV}; without one, coverage is skipped)",
    )
    names = inventory.add_mutually_exclusive_group()
    names.add_argument(
        "--hide-names",
        dest="hide_names",
        action="store_true",
        default=True,
        help="do not print zone/VMC names (the default; applies to --json too)",
    )
    names.add_argument(
        "--show-names",
        dest="hide_names",
        action="store_false",
        help="print zone/VMC names (personal data: keep it out of chat transcripts)",
    )
    inventory.add_argument("--json", action="store_true", help="print JSON instead of text")

    diff = sub.add_parser("diff", help="record-level diff of two capture directories (offline)")
    diff.add_argument("dir_a", type=Path)
    diff.add_argument("dir_b", type=Path)
    diff.add_argument(
        "--ignore-volatile",
        type=Path,
        default=None,
        help="file of fnmatch patterns on paths (or store:path) to ignore",
    )
    diff.add_argument(
        "--show-personal",
        action="store_true",
        help=(
            "print personal values (names, locality, SSID, MAC, serials, IPs) instead of "
            "<personal>/<ip>/<mac> (keep it out of chat transcripts); user names stay <user>"
        ),
    )
    diff.add_argument("--json", action="store_true", help="print JSON instead of text")

    har = sub.add_parser("redact-har", help="sanitise a HAR file (offline)")
    har.add_argument("in_har", type=Path)
    har.add_argument("out_har", type=Path)
    har.add_argument(
        "--controller-host",
        action="append",
        default=None,
        help="controller host name(s) in the HAR (default: inferred from API paths)",
    )

    fixtures = sub.add_parser("sanitize-fixtures", help="redact + pseudonymise a capture (offline)")
    fixtures.add_argument("capture_dir", type=Path)
    fixtures.add_argument("out_dir", type=Path)
    salt = fixtures.add_mutually_exclusive_group()
    salt.add_argument(
        "--salt",
        default=None,
        help="pseudonymisation salt (default: $REHOM_FIXTURE_SALT); required for reproducible "
        "fixtures",
    )
    salt.add_argument(
        "--random-salt",
        action="store_true",
        help="use a random one-off salt (not stored): the fixtures cannot be regenerated",
    )

    wt = sub.add_parser(
        "write-test",
        help="supervised write test: one write, verified, then reverted (dry run by default)",
    )
    wt.add_argument("--op", choices=sorted(OPERATIONS), help="the operation to test")
    wt.add_argument("--target", default=None, help="zone or VMC id (e.g. 001)")
    wt.add_argument("--value", default=None, help="the forward value (see --list)")
    wt.add_argument("--execute", action="store_true", help="really send the writes")
    wt.add_argument(
        "--dwell",
        type=_dwell,
        default=None,
        help=(
            f"override the dwell, 1..{WRITE_TEST_MAX_DWELL_S:g} s "
            f"(vmc-mode: at least {VMC_MODE_DWELL_S:g} s)"
        ),
    )
    wt.add_argument(
        "--journal",
        type=Path,
        default=None,
        help=(
            f"write-ahead journal (default ${WRITE_JOURNAL_ENV}, else "
            "$XDG_STATE_HOME/aiorehom/write-tests/journal.jsonl, XDG_STATE_HOME defaulting "
            "to ~/.local/state)"
        ),
    )
    wt.add_argument("--list", action="store_true", help="list operations and exit")
    wt.add_argument("--status", action="store_true", help="show open journal entries and exit")
    wt.add_argument(
        "--close", metavar="ID", default=None, help="close an entry after restoring it by hand"
    )

    replay = sub.add_parser(
        "replay",
        help="replay a capture through the client, offline, and print model-level changes",
        description=(
            "Replay FIXTURE_DIR (interface.json, overrides.json, plant_conf.json, config.json, "
            "ws.jsonl and optionally alive.json) through the real client with a fake transport "
            "and WebSocket. Never opens a socket; --host/--port/--username are ignored."
        ),
    )
    replay.add_argument("fixture_dir", type=Path)
    mode = replay.add_mutually_exclusive_group()
    mode.add_argument(
        "--instant",
        action="store_true",
        help="virtual clock: the whole capture replays in seconds (the default)",
    )
    mode.add_argument(
        "--speed",
        type=_bounded(float, 0, REPLAY_MAX_SPEED, "--speed"),
        default=None,
        metavar="X",
        help=f"real time sped up X times (0 < X <= {REPLAY_MAX_SPEED:g})",
    )
    replay.add_argument("--json", action="store_true", help="one JSON object per update")
    replay.add_argument(
        "--show-names",
        action="store_true",
        help="print zone/VMC names (personal data: keep it out of chat transcripts)",
    )
    replay.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="GLOB",
        help="drop model paths matching GLOB (fnmatch, e.g. 'zones.*.temperature'); repeatable",
    )
    replay.add_argument(
        "--alarm-debounce",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="alarm debounce window (>= 30, default 60)",
    )
    replay.add_argument(
        "--until",
        type=_hms,
        default=None,
        metavar="HH:MM:SS",
        help="stop at this UTC time of the capture (default: its last line)",
    )
    return parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _echo(message: str) -> None:
    print(message, flush=True)


def _err(message: str) -> None:
    print(f"rehom-probe: {message}", file=sys.stderr, flush=True)


def _out_dir(args: argparse.Namespace) -> Path:
    out: Path = args.out if args.out is not None else default_capture_root() / utc_stamp()
    return ensure_private_dir(out.expanduser())


@contextlib.contextmanager
def _sigint_sets(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    installed = False
    with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
        loop.add_signal_handler(signal.SIGINT, stop.set)
        installed = True
    try:
        yield
    finally:
        if installed:
            loop.remove_signal_handler(signal.SIGINT)


def _latency_summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "count": len(ordered),
        "min_ms": ordered[0],
        "median_ms": statistics.median(ordered),
        "p95_ms": p95,
        "max_ms": ordered[-1],
    }


async def _timed_alive(transport: ReadOnlyTransport) -> dict[str, Any]:
    error: str | None = None
    try:
        await transport.get_alive()
    except ForbiddenRequestError:
        raise
    except RehomError as err:
        error = type(err).__name__
    last = transport.last_request or {}
    return {
        "ok": error is None and bool(last.get("ok")),
        "status": last.get("status"),
        "latency_ms": last.get("latency_ms"),
        "error": error or last.get("error"),
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def cmd_session(args: argparse.Namespace) -> int:
    out = _out_dir(args)
    _echo(f"rehom-probe session -> {out}")
    async with ReadOnlyTransport(
        args.host,
        args.port,
        min_interval=SESSION_MIN_INTERVAL_S,
        timeout=10,
        allow_login=not args.no_login,
    ) as transport:
        session = ProbeSession(
            transport,
            out,
            login=not args.no_login,
            username=args.username,
            service=args.service,
            echo=_echo,
        )
        meta = await session.run(args.steps)
    failed = [n for n, s in meta["steps"].items() if s["status"] == "error"]
    login = meta["login"]
    if login["state"] == "failed":
        _err(f"login failed: {login['error']}")
    _echo(
        f"done: {meta['requests']} request(s), {meta['redaction_total']} value(s) redacted, "
        f"files in {out}"
    )
    return 1 if failed or login["state"] == "failed" else 0


async def cmd_watch(args: argparse.Namespace) -> int:
    out = _out_dir(args)
    stop = asyncio.Event()
    started = utc_now_iso()
    latencies: list[float] = []
    _echo(f"rehom-probe watch ({args.minutes:g} min, listen-only) -> {out / 'ws.jsonl'}")
    with JsonlWriter(out / "ws.jsonl") as ws_log, _sigint_sets(stop):

        def on_frame(frame: Any) -> None:
            ws_log.write_timestamped("frame", frame)

        def on_event(event: dict[str, Any]) -> None:
            ws_log.write_timestamped("event", event)
            _echo(f"ws: {json.dumps(event)}")

        async def poll_alive(transport: ReadOnlyTransport, writer: JsonlWriter) -> None:
            while not stop.is_set():
                sample = await _timed_alive(transport)
                writer.write_timestamped("alive", sample)
                if sample["ok"] and sample["latency_ms"] is not None:
                    latencies.append(float(sample["latency_ms"]))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), WATCH_ALIVE_PERIOD_S)

        poller: asyncio.Task[None] | None = None
        transport: ReadOnlyTransport | None = None
        alive_log: JsonlWriter | None = None
        try:
            if args.with_latency:
                transport = ReadOnlyTransport(
                    args.host, args.port, min_interval=SESSION_MIN_INTERVAL_S, timeout=5
                )
                alive_log = JsonlWriter(out / "alive_latency.jsonl")
                poller = asyncio.create_task(poll_alive(transport, alive_log))
            # With --with-latency the WS handshake takes its turn in the transport's
            # queue: it never overlaps an /api/alive/ poll and is paced like one.
            stats = await listen_only(
                args.host,
                args.port,
                on_frame,
                stop,
                args.minutes,
                on_event=on_event,
                gate=transport.pacing_slot if transport is not None else None,
            )
        finally:
            stop.set()
            if poller is not None:
                await poller
            if transport is not None:
                await transport.close()
            if alive_log is not None:
                alive_log.close()
    meta = {
        "tool": "rehom-probe watch",
        "aiorehom_version": __version__,
        "host": args.host,
        "port": args.port,
        "started_utc": started,
        "finished_utc": utc_now_iso(),
        "minutes": args.minutes,
        "ws": stats.to_dict(),
        "alive_latency": _latency_summary(latencies) if args.with_latency else None,
    }
    write_json(out / "watch_meta.json", meta)
    _echo(
        f"done: {stats.frames} frame(s), {stats.connects} connect(s), "
        f"{stats.redacted_values} value(s) redacted, stop={stats.stop_reason}"
    )
    return 0


async def cmd_latency(args: argparse.Namespace) -> int:
    out = _out_dir(args)
    stop = asyncio.Event()
    started = utc_now_iso()
    latencies: list[float] = []
    samples = 0
    _echo(f"rehom-probe latency ({args.seconds:g} s, /api/alive/ every 2 s) -> {out}")
    deadline = time.monotonic() + args.seconds
    with JsonlWriter(out / "latency.jsonl") as log, _sigint_sets(stop):
        async with ReadOnlyTransport(
            args.host, args.port, min_interval=LATENCY_PERIOD_S, timeout=5
        ) as transport:
            while not stop.is_set() and time.monotonic() < deadline:
                sample = await _timed_alive(transport)
                samples += 1
                log.write_timestamped("alive", sample)
                if sample["ok"] and sample["latency_ms"] is not None:
                    latencies.append(float(sample["latency_ms"]))
    summary = _latency_summary(latencies)
    meta = {
        "tool": "rehom-probe latency",
        "aiorehom_version": __version__,
        "host": args.host,
        "port": args.port,
        "started_utc": started,
        "finished_utc": utc_now_iso(),
        "seconds": args.seconds,
        "samples": samples,
        "ok": len(latencies),
        "summary": summary,
    }
    write_json(out / "latency_meta.json", meta)
    _echo(f"done: {samples} sample(s), {json.dumps(summary)}")
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    capture_dir: Path = args.capture_dir
    if not capture_dir.is_dir():
        _err(f"not a directory: {capture_dir}")
        return 2
    catalog: Any = None
    catalog_path: Path | None = args.catalog if args.catalog is not None else default_catalog()
    if catalog_path is not None and catalog_path.is_file():
        catalog = read_json(catalog_path)
    elif catalog_path is not None:
        _err(f"catalogue not found, coverage skipped: {catalog_path}")
    else:
        _err(f"no catalogue (pass --catalog or set ${CATALOG_ENV}); coverage skipped")
    inventory = build_inventory(load_capture(capture_dir), catalog, hide_names=args.hide_names)
    if args.json:
        sys.stdout.write(dump_json(inventory))
    else:
        sys.stdout.write(format_inventory(inventory, hide_names=args.hide_names))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    for directory in (args.dir_a, args.dir_b):
        if not directory.is_dir():
            _err(f"not a directory: {directory}")
            return 2
    patterns = load_volatile_patterns(args.ignore_volatile) if args.ignore_volatile else []
    result = mask_diff(
        diff_captures(args.dir_a, args.dir_b, patterns), personal=not args.show_personal
    )
    not_compared = stores_not_compared(args.dir_a, args.dir_b)
    if args.json:
        payload: dict[str, Any] = {name: d.to_dict() for name, d in result.items()}
        payload["not_compared"] = not_compared
        sys.stdout.write(dump_json(payload))
    else:
        sys.stdout.write(format_diff(result, not_compared))
    return 0


def cmd_redact_har(args: argparse.Namespace) -> int:
    in_path: Path = args.in_har
    out_path: Path = args.out_har
    if in_path.resolve() == out_path.resolve():
        _err("refusing to overwrite the input HAR; choose another output path")
        return 2
    try:
        har = read_json(in_path)
    except (OSError, ValueError) as err:
        _err(f"cannot read HAR: {type(err).__name__}")
        return 2
    if not isinstance(har, dict):
        _err("not a HAR document (top level must be an object)")
        return 2
    cleaned = redact_har(har, args.controller_host)
    ensure_private_dir(out_path.parent.resolve())
    write_json(out_path, cleaned)
    counts = cleaned.get("log", {}).get("_aiorehom_redaction", {})
    _echo(f"wrote {out_path}: {json.dumps(counts)}")
    return 0


def cmd_sanitize_fixtures(args: argparse.Namespace) -> int:
    if not args.capture_dir.is_dir():
        _err(f"not a directory: {args.capture_dir}")
        return 2
    salt: str | None
    if args.random_salt:
        salt = secrets.token_hex(16)
        _echo("note: random one-off salt used (not stored); these fixtures cannot be regenerated")
    else:
        salt = args.salt or os.environ.get("REHOM_FIXTURE_SALT")
        if not salt:
            _err(
                "a salt is required for reproducible fixtures: pass --salt or set "
                "$REHOM_FIXTURE_SALT (or --random-salt for one-off fixtures)"
            )
            return 2
    try:
        summary = sanitize_capture(args.capture_dir, args.out_dir, salt)
    except ValueError as err:
        _err(str(err))
        return 2
    write_private_text(args.out_dir / "SANITISED.txt", _sanitised_note(summary))
    _echo(
        f"wrote {len(summary['files'])} file(s) to {args.out_dir}; "
        f"skipped: {', '.join(summary['skipped']) or '-'}"
    )
    return 0


def _sanitised_note(summary: dict[str, Any]) -> str:
    return (
        "Fixtures produced by `rehom-probe sanitize-fixtures` (redaction + pseudonymisation).\n"
        "Review before publishing.\n\n" + dump_json(summary)
    )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _iso_ms(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s"


def _text(value: Any) -> str:
    return "<absent>" if value is ABSENT else str(value)


class _ReplayReport:
    """Turns published updates into model-level change lines (masked)."""

    def __init__(self, *, as_json: bool, show_names: bool, ignore: Sequence[str]) -> None:
        self._json = as_json
        self._show_names = show_names
        self._ignore = list(ignore)
        self.counts: Counter[UpdateReason] = Counter()
        self.alarms_raised = 0
        self.alarms_debounced = 0
        self._last_state: object = None
        self._last_flat: dict[str, Any] = {}

    def _flat(self, update: StateUpdate) -> tuple[dict[str, Any], dict[str, Any]]:
        if update.previous is None:
            old: dict[str, Any] = {}
        elif update.previous is self._last_state:
            old = self._last_flat
        else:
            old = mask_flat(flatten_state(update.previous), show_names=self._show_names)
        new = mask_flat(flatten_state(update.state), show_names=self._show_names)
        self._last_state, self._last_flat = update.state, new
        return old, new

    def on_update(self, update: StateUpdate) -> None:
        self.counts[update.reason] += 1
        raised, debounced = update_alarm_ids(update)
        self.alarms_raised += len(raised)
        self.alarms_debounced += len(debounced)
        old, new = self._flat(update)
        changes = diff_flat(old, new, ignore=self._ignore)
        if self._json:
            record = {
                "t": _iso_ms(update.at),
                "reason": update.reason.value,
                "changes": [
                    {"path": path, "old": format_value(before), "new": format_value(after)}
                    for path, before, after in changes
                ],
                "records": masked_records(update.changed),
                "removed": masked_records(update.removed),
            }
            sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
            return
        head = f"{_iso_ms(update.at)} {update.reason.value:<8}"
        if update.reason is UpdateReason.SYNC:
            state = update.state
            plant = state.plant
            _echo(
                f"{head} zones={','.join(state.zones) or '-'} vmcs={','.join(state.vmcs) or '-'} "
                f"fancoils={','.join(state.fancoils) or '-'} "
                f"preset={plant.preset.value if plant.preset is not None else '-'} "
                f"season={plant.season.value if plant.season is not None else '-'} "
                f"setpoint_mismatch={plant.setpoint_mismatch} alarms={len(state.alarms)}"
            )
            return
        if not changes:
            return
        shown = [
            f"{path}: {_text(before)} -> {_text(after)}"
            for path, before, after in changes[:REPLAY_TEXT_MAX_CHANGES]
        ]
        if len(changes) > REPLAY_TEXT_MAX_CHANGES:
            shown.append(f"... (+{len(changes) - REPLAY_TEXT_MAX_CHANGES} more)")
        _echo(f"{head} {'; '.join(shown)}")

    def summary(self, stats: SyncStats, *, start: datetime, end: datetime, dropped: int) -> None:
        seconds = (end - start).total_seconds()
        if self._json:
            payload: dict[str, Any] = dict(asdict(stats))
            payload.update(
                {
                    "updates": {reason.value: self.counts[reason] for reason in UpdateReason},
                    "alarms_raised": self.alarms_raised,
                    "alarms_debounced": self.alarms_debounced,
                    "frames_dropped": dropped,
                    "start": _iso_ms(start),
                    "end": _iso_ms(end),
                    "virtual_seconds": round(seconds, 3),
                }
            )
            sys.stdout.write(json.dumps({"summary": payload}) + "\n")
            return
        counts = self.counts
        updates = [
            f"sync {counts[UpdateReason.SYNC]}",
            f"frames {counts[UpdateReason.FRAMES]}",
            f"resync {counts[UpdateReason.RESYNC]}",
            f"clock {counts[UpdateReason.CLOCK]}",
        ]
        updates.extend(
            f"{reason.value} {counts[reason]}"
            for reason in (UpdateReason.FALLBACK, UpdateReason.CONFIG)
            if counts[reason]
        )
        _echo(
            f"replayed {stats.frames_received} frames ({stats.noop_updates} no-op, "
            f"{stats.removes_coalesced} coalesced removes, {stats.forecast_frames} forecast) "
            f"over {_duration(seconds)} virtual; updates: {', '.join(updates)}; "
            f"alarms raised {self.alarms_raised} (debounced {self.alarms_debounced})"
        )


def _replay_end(device: ReplayDevice, until: dt_time | None) -> datetime:
    if until is None:
        return device.end
    end = datetime.combine(device.start.date(), until, tzinfo=UTC)
    if end < device.start:
        end += timedelta(days=1)
    if end > device.end:
        raise ReplayError(f"--until is after the end of the capture ({_iso_ms(device.end)})")
    return end


async def cmd_replay(args: argparse.Namespace) -> int:
    try:
        device = ReplayDevice.from_dir(args.fixture_dir)
        end = _replay_end(device, args.until)
    except ReplayError as err:
        _err(str(err))
        return 2
    options = ClientOptions(alarm_debounce=args.alarm_debounce)  # ValueError -> exit 2
    clock: VirtualClock | ScaledClock = (
        VirtualClock(device.start) if args.speed is None else ScaledClock(device.start, args.speed)
    )
    report = _ReplayReport(as_json=args.json, show_names=args.show_names, ignore=args.ignore)
    client = RehomClient(
        REPLAY_HOST,
        transport=device.transport(clock),
        ws_connector=device.ws_connector(clock),
        clock=clock,
        options=options,
    )
    client.subscribe(report.on_update)
    driver = ReplayDriver(device, clock)
    connect = asyncio.create_task(client.connect())
    try:
        drive = asyncio.create_task(driver.run_until(end))
        await asyncio.wait({connect, drive}, return_when=asyncio.FIRST_COMPLETED)
        if connect.done() and connect.exception() is not None:
            drive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drive
            await connect  # raises: RehomError -> exit 1, ForbiddenRequestError -> exit 3
        await drive
        if not connect.done() and isinstance(clock, ScaledClock):
            # real time: processing costs are sped up too; the sync always finishes
            await connect
        if not connect.done():  # a virtual clock no longer moves: it never would
            connect.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await connect
            _err("the replay ended before the first sync completed (try a later --until)")
            return 1
        await connect
    except ForbiddenRequestError:
        raise
    except RehomError as err:
        _err(f"replay failed: {type(err).__name__}: {err}")
        return 1
    finally:
        await client.close()
    report.summary(client.stats, start=device.start, end=end, dropped=device.frames_dropped)
    return 0


# ---------------------------------------------------------------------------
# write-test
# ---------------------------------------------------------------------------


def _journal_path(args: argparse.Namespace) -> Path:
    if args.journal is not None:
        return Path(args.journal).expanduser().resolve()
    return default_write_journal()


def _wall_now() -> datetime:
    """The write-test command's clock (a module function so tests can replace it)."""
    return datetime.now(UTC)


async def _wall_sleep(seconds: float) -> None:
    """The write-test command's sleep (a module function so tests can replace it)."""
    await asyncio.sleep(seconds)


def _write_test_status(journal: Path) -> int:
    entries = open_entries(journal)
    for entry in entries:
        _echo(
            f"OPEN {entry['id']}: {entry.get('summary')} originals={entry.get('originals')} "
            f"raw={entry.get('before')}"
        )
    _echo(f"{len(entries)} open entr{'y' if len(entries) == 1 else 'ies'} in {journal}")
    return 1 if entries else 0


async def _write_test_session(args: argparse.Namespace, journal: Path) -> int:
    username, password = await read_keychain_credentials(args.service, username=args.username)
    client = RehomClient(
        args.host,
        port=args.port,
        username=username,
        password=password,
        allow_writes=args.execute is True,
    )
    del password
    try:
        await client.connect()
        if args.execute:
            wait = client.options.alarm_debounce + 5
            _echo(f"connected; waiting {wait:g} s so alarms are debounced before pre-flight")
            await _wall_sleep(wait)
        await run_write_test(
            client,
            lambda st: build_operation(st, args.op, args.target, args.value),
            journal=journal,
            execute=args.execute is True,
            echo=_echo,
            sleep=_wall_sleep,
            now=_wall_now,
            dwell_s=args.dwell,
        )
    finally:
        await client.close()
    return 0


async def cmd_write_test(args: argparse.Namespace) -> int:
    """Supervised write test (see :mod:`aiorehom.writetest`).

    Exit codes: 0 done, 1 another error (``--status``: open entries), 2 bad
    arguments, 4 refused or stopped (a stopped test's journal entry stays open).
    """
    if args.list:
        for name, usage in sorted(OPERATIONS.items()):
            _echo(f"{name:13} {usage}")
        return 0
    try:
        journal = _journal_path(args)
        if args.status:
            return _write_test_status(journal)
        if args.close is not None:
            close_entry(journal, args.close, "closed by the owner after manual restore")
            _echo(f"closed {args.close} in {journal}")
            return 0
        # Every argument is checked before the Keychain is read or anything connects.
        if args.op is None or args.value is None:
            _err("--op and --value are required (see --list)")
            return 2
        check_arguments(args.op, args.target, args.value)
        if args.op == "vmc-mode" and args.dwell is not None and args.dwell < VMC_MODE_DWELL_S:
            _err(f"vmc-mode needs a dwell of at least {VMC_MODE_DWELL_S:g} s")
            return 2
        _echo(f"journal: {journal}")
        return await _write_test_session(args, journal)
    except ValueError as err:
        _err(str(err))
        return 2
    except WriteTestError as err:
        _err(str(err))
        return 4
    except ForbiddenRequestError:
        raise
    except RehomError as err:
        _err(f"{type(err).__name__}: {err}")
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


_ASYNC_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], Coroutine[Any, Any, int]]]] = {
    "session": cmd_session,
    "watch": cmd_watch,
    "latency": cmd_latency,
    "replay": cmd_replay,
    "write-test": cmd_write_test,
}
_SYNC_COMMANDS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "inventory": cmd_inventory,
    "diff": cmd_diff,
    "redact-har": cmd_redact_har,
    "sanitize-fixtures": cmd_sanitize_fixtures,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Console entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in _ASYNC_COMMANDS:
            return asyncio.run(_ASYNC_COMMANDS[args.command](args))
        return _SYNC_COMMANDS[args.command](args)
    except ForbiddenRequestError as err:
        _err(f"safety guard: {err}")
        return 3
    except ValueError as err:
        _err(str(err))
        return 2
    except OSError as err:
        _err(f"I/O error: {type(err).__name__}: {err.strerror or err}")
        return 1
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
