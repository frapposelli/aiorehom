# aiorehom

Unofficial async Python client for the local API of the **Rehom Radiax**
controller ("RadiaxWeb" web server): radiant floor heating and cooling, VMC
units and dehumidifiers. It is the library behind the
[ha-rehom](https://github.com/frapposelli/ha-rehom) Home Assistant integration.

> **Status: alpha.** Version 0.3 reads the plant state (REST snapshots plus
> the controller's WebSocket) and, only when a client is created with
> `allow_writes=True`, sends a small set of guarded commands: house preset,
> comfort temperature, predictive algorithm, zone offset and mode, temporary
> comfort, VMC fan and mode. Season and schedule edits are not writable. The
> API may change before 1.0.

This is an independent interoperability project. It is not affiliated with,
endorsed by or supported by Rehom S.r.l.; see the [disclaimer](#disclaimer).

## Features

- `RehomClient` keeps a live, immutable model of one controller (zones, VMCs,
  actuators, fancoils, alarms, health, weather) and publishes batched
  `StateUpdate` notifications.
- WebSocket-first sync with REST snapshots, periodic resyncs, a REST fallback
  when the socket is down, and time-driven rebuilds at schedule boundaries.
- Read-only by default: an exact request allowlist, one request at a time,
  and a receive-only WebSocket (see [Safety](#safety)).
- Opt-in, guarded writes: typed `set_*` commands, each checked against the
  current state, sent once and confirmed by the controller (see
  [Writing](#writing-opt-in)).
- Fully typed (`py.typed`, `mypy --strict`), asyncio and aiohttp based.
- `rehom-probe`, a command-line probe that captures, inspects and replays
  controller sessions offline, and runs supervised write tests.

Requires Python 3.13 or later. Runtime dependencies: `aiohttp` (3.12 or later)
and `yarl`.

## Installation

```sh
pip install aiorehom
# or
uv add aiorehom
```

## Quick start

```python
import asyncio

from aiorehom import RehomClient, StateUpdate


async def main() -> None:
    async with RehomClient("192.0.2.10", username="homeassistant", password="...") as client:

        def on_update(update: StateUpdate) -> None:
            print(update.reason.value, sorted(update.changed))

        client.subscribe(on_update)
        await client.connect()  # WebSocket first, then the REST snapshot

        state = client.state
        print(state.plant.preset, state.plant.setpoint_mismatch)
        for zone in state.zones.values():
            print(zone.id, zone.name, zone.temperature, zone.target, zone.hvac_action)

        await asyncio.sleep(3600)


asyncio.run(main())
```

Pass `session=` to reuse an existing `aiohttp.ClientSession` (for example Home
Assistant's shared session). A session you pass in is never modified or
closed.

### The model

- `client.state` is an immutable `RehomState` snapshot. Every change is
  published as `StateUpdate(state, previous, reason, changed=<record paths>, ...)`.
  A value that is numerically unchanged (`"24"` vs `"24.0"`) never notifies,
  and neither does a resync that finds no difference.
- The client also rebuilds the state at `state.next_change_at`: schedule slot
  boundaries, override expiry, alarm debounce and heartbeat deadlines.
- `state.alarms_debounced` is debounced on both edges
  (`ClientOptions.alarm_debounce`, 60 s by default).
- `client.connection_state` is `CONNECTED`, `DEGRADED` (WebSocket down, REST
  only), `UNAVAILABLE` or `CLOSED`. The socket is receive-only, so a half-open
  connection is detected by silence (`ClientOptions.ws_idle_timeout`, 180 s)
  and reconnected.
- A failed resync is retried with exponential backoff up to the resync
  interval; `client.stats` exposes failure counters and the last error class.
- `client.get_history(...)` fetches history in chunks of 24 hours or less.
- `client.dump()` returns diagnostics with secrets redacted (personal data such
  as zone names is not masked). `state.as_dict()` is a JSON-ready copy of the
  model.
- There is no re-login: an HTTP 401/403 raises `RehomAuthenticationError`.

| Module | Contents |
|---|---|
| `aiorehom.client` | `RehomClient`, `ClientOptions` |
| `aiorehom.models` | Frozen dataclasses of the state (`RehomState`, `Zone`, `Vmc`, `Alarm`, ...) |
| `aiorehom.enums`, `aiorehom.values` | Public enums; numeric-aware parsing and comparison of raw values |
| `aiorehom.logic` | Pure derived logic: presence, schedules, running program, overrides, zone target and mode, master preset, lock, VMC, alarms, heartbeat, weather |
| `aiorehom.writes` | Pure write planners: guards, the exact records to send and the values to confirm |
| `aiorehom.transport` | `ReadOnlyTransport`, the only code that issues HTTP requests (the allowlist and the write gate) |
| `aiorehom.websocket` | Receive-only WebSocket connections |
| `aiorehom.replay` | Offline replay of a capture through the real client |
| `aiorehom.redact`, `aiorehom.pseudonymise` | Secret redaction; pseudonymisation of personal data for test fixtures |
| `aiorehom.writetest` | Supervised write tests (forward, verify, revert) behind `rehom-probe write-test` |
| `aiorehom.cli` | The `rehom-probe` command |

## Writing (opt-in)

A client is read-only unless it is created with `allow_writes=True` (an
actual `bool`; anything else raises `TypeError`). Commands need a completed
`connect()`:

```python
from aiorehom import RehomClient, RehomWriteNotConfirmedError, RehomWriteRefusedError

async with RehomClient(
    "192.0.2.10", username="homeassistant", password="...", allow_writes=True
) as client:
    await client.connect()
    try:
        sent = await client.set_zone_offset("001", 1)  # zone 001: +1 °C
    except RehomWriteRefusedError as err:
        print("refused:", err.reason)  # e.g. "zone_offline", "offset_out_of_range"
    except RehomWriteNotConfirmedError:
        print("accepted, but the controller did not report it: check before retrying")
    else:
        print("confirmed" if sent else "already set, nothing sent")
```

| Command | Notes |
|---|---|
| `set_house_preset(MasterPreset)` | AUTO, or MANUAL at ECONOMY, PRE_COMFORT or COMFORT (the level's temperature is written as the setpoint too); never OFF |
| `set_comfort_temperature(float)` | 0.1 °C steps, only while the house is MANUAL at COMFORT, within the season's limits |
| `set_predictive(bool)` | House in AUTO |
| `set_zone_offset(zone_id, int)` | Whole degrees, -3 to +3 |
| `set_zone_mode(zone_id, ZoneSetp)` | Back to the schedule (`UNSET`) or a manual level; house in AUTO; never OFF |
| `set_temporary_comfort(zone_id, minutes)` | COMFORT for 30 to 1440 minutes in 30-minute steps, ending before midnight; not yet verified on a live controller |
| `set_vmc_fan(vmc_id, int)` | A discrete `FanSpeed` or a value in the fan's range |
| `set_vmc_mode(vmc_id, VmcMode)` | Only modes the installer made selectable; never the timed rapid modes |

What every command guarantees:

- **Guards first.** The command is planned against the latest state
  (`aiorehom.writes`). If a guard fails (crono mode, bus down, a read-only
  controller, an unknown or offline zone or VMC, or the command's own
  preconditions), it raises `RehomWriteRefusedError` and nothing is sent.
  `err.reason` is a stable code, suitable as a translation key; a read-only
  client refuses with `"writes_disabled"`.
- **One at a time, sent once.** Commands run in call order, one at a time.
  Each is a single request through the transport's write gate, never
  retried.
- **Confirmed.** A command returns only after the controller reports the new
  values (its live echo within `ClientOptions.write_confirm_timeout`, 20 s,
  or failing that one fresh snapshot), and `client.state` already shows them.
  It returns `False`, without sending anything, when the controller already
  reports them.
- **Honest failures.** `RehomWriteNotConfirmedError` means the controller
  accepted the write but did not report the new values. Any other error
  from the request itself (a timeout in particular) means it may or may not
  have landed. Neither is retried for you: read `client.state` first.

Season changes and schedule (weekly program) edits are not writable.

## Safety

By default aiorehom looks without touching anything, and writing is a
deliberate, narrow opt-in. These guarantees are enforced in code and covered
by tests (`tests/test_transport_allowlist.py`, `tests/test_transport_writes.py`,
`tests/test_client_writes.py`, `tests/test_websocket.py`,
`tests/test_e2e_no_writes.py`).

- **Exact allowlist.** Each permitted request has its own typed method on
  `ReadOnlyTransport`; there is no generic request method. The allowlist
  contains `GET`s of read endpoints (with fixed query keys where queries are
  allowed), at most one login per transport, and the two bulk-write
  endpoints behind the write gate. Any other method, path or query raises
  `ForbiddenRequestError` before a socket is opened. Paths are validated
  strictly and compared exactly (no prefix or glob matching, no `..`, `//`,
  percent-encoding, backslashes or non-ASCII characters).
- **Read-only by default.** Unless the transport was created with
  `allow_writes=True` (an actual `bool`), every write is refused before any
  I/O, so nothing that changes the controller's state can be sent.
- **Write gate.** When writes are enabled, every body must match one of a
  few fixed templates, shaped like the payloads the official web client
  sends: a fixed set of writable keys, a value domain for every key (house
  and zone OFF and the timed rapid VMC modes are not in it), at most 3
  records, ASCII only. The records are rebuilt from the validated values, so
  what is sent is exactly what was checked. Within the library only
  `RehomClient` sends writes, one at a time, and nothing is ever retried.
- **Nothing after close.** A closed transport refuses every later request,
  including one already waiting for its turn.
- **One request per call.** Redirects are never followed (any 3xx raises), and
  a per-request middleware refuses any second hop, including aiohttp's
  transparent resend of idempotent requests.
- **Receive-only WebSocket.** The socket is wrapped so that every `send_*`,
  `ping` and `pong` method raises; only the automatic pong to server pings and
  a CLOSE frame at shutdown ever go out.
- **Pacing.** Requests run strictly one at a time, at least 1 s apart. The
  controller is a small single-board computer.
- **Secrets.** The API token is held only in memory and never appears in
  logs, exceptions, `repr()` or files. Error messages never include response
  bodies or headers.
- **Redaction.** Before anything reaches disk or diagnostics, every value that
  looks like a credential (passwords, keys, tokens) is replaced by a marker
  that keeps only its presence and type, for example `<redacted len=8>`.

## Security advice

The controller's local API is plain HTTP and is meant for a trusted LAN.

- Keep the controller on a trusted, preferably isolated, network segment (for
  example an IoT VLAN that only your Home Assistant host can reach).
- Never expose the controller's ports (8000, 1337) to the internet, and do not
  port-forward them.
- Use a dedicated controller account for aiorehom or Home Assistant, with the
  least privileges available, and a unique password.
- Enable writes only where you need them; a read-only client cannot change
  anything on the controller.

## `rehom-probe`

`rehom-probe` is a diagnostic CLI. It uses the same allowlisted transport.
Every command is read-only except `write-test --execute` (see
[Supervised write tests](#supervised-write-tests)).

```
rehom-probe [--host rehomserver.local] [--port 8000] [--out DIR]
            [--username NAME] [--service rehom-api] [--no-login] COMMAND ...
```

| Command | Network | What it does |
|---|---|---|
| `session [--steps 1,3-5]` | allowlisted GETs, at most one login | Runs the capture steps (below) and saves redacted results |
| `watch [--minutes 30] [--with-latency]` | receive-only WebSocket (+ `/api/alive/` every 15 s) | Saves WebSocket frames to `ws.jsonl` (maximum 120 min) |
| `latency [--seconds 120]` | `/api/alive/` every 2 s | Measures baseline latency (maximum 600 s) |
| `inventory CAPTURE_DIR [--catalog FILE] [--show-names] [--json]` | none | Human summary of a capture, with optional coverage against a record catalogue |
| `diff DIR_A DIR_B [--ignore-volatile FILE] [--show-personal] [--json]` | none | Record-level diff of two captures |
| `redact-har IN.har OUT.har [--controller-host H]` | none | Sanitises a browser HAR file |
| `sanitize-fixtures CAPTURE_DIR OUT_DIR (--salt S \| --random-salt)` | none | Redacts again, then pseudonymises, to produce test fixtures |
| `replay FIXTURE_DIR [--instant \| --speed X] [--json] [--show-names] ...` | none | Replays a capture through the real client on a virtual clock and prints the model changes |
| `write-test --op OP [--target ID] --value V [--execute] ...` | read-only client; with `--execute`, two gated writes | Supervised write test: forward, verify, revert (below) |

With `--no-login`, `session` skips every step that needs a login.

Session steps: (1) `/api/alive/`; (2) login and `/api/me/`; (3) `/api/config/`
(never with a query string); (4) `/api/interface/`; (5) `/api/overrides/` and
`/api/plant/conf/`; (6) the four domotica GETs; (7) 6 hours of history for a
few plant and zone series; (8) two filtered interface GETs.

Output hygiene:

- Every response is redacted before it is written. Files are created with
  mode `0600` and directories with mode `0700`. The request log records
  method, path, status, latency and size, never headers or bodies.
- `inventory`, `diff` and `replay` print to the terminal, which may end up in
  a chat transcript or an issue. They re-redact every file they read and mask
  personal data by default (zone and VMC names, locality, GPS position, MAC
  and IP addresses, serial numbers, user names); `--show-names` and
  `--show-personal` opt out.
- `sanitize-fixtures` is deterministic for a salt (`--salt` or
  `$REHOM_FIXTURE_SALT`). Always review fixtures yourself before sharing them.

Default output directory: `$REHOM_PROBE_CAPTURE_ROOT/<UTC timestamp>/`, else
`./captures/<UTC timestamp>/`; `--out` overrides both. The record catalogue for
`inventory` is `--catalog`, else `$REHOM_PROBE_CATALOG`; without one, coverage
is skipped.

### Supervised write tests

`write-test` tries one write on a real controller under supervision: it
writes, verifies and reverts. Operations: `zone-offset`, `zone-mode`,
`vmc-fan`, `vmc-mode` (to `ventilate` or `standby` only), `predictive`,
`house` and `comfort-temp` (a delta such as `0.1`); `--list` shows their
arguments.

```sh
rehom-probe write-test --list                                    # operations and their values
rehom-probe write-test --op zone-offset --target 001 --value 1   # dry run: prints both writes
rehom-probe write-test --op zone-offset --target 001 --value 1 --execute
rehom-probe write-test --status                                  # open journal entries
rehom-probe write-test --close ID                                # after restoring by hand
```

- **Dry run by default.** Without `--execute` it connects read-only, plans
  the forward and the revert write, prints their records and sends nothing.
  Every argument is checked before the Keychain is read or anything
  connects.
- **Pre-flight.** It refuses to start (dry run included) if the master panel
  is in control, the bus is down, the controller is read-only, an installer
  session or recent commissioning activity is seen, an alarm is active, the
  run would come within 2 minutes of a half-hour schedule boundary, the
  journal has an open entry, the operation's own guards fail, or there is
  nothing to test. With `--execute` it first waits out the alarm debounce.
- **Write-ahead journal.** The forward and revert records and the original
  values are appended to the journal and forced to disk before anything is
  sent.
- **Forward, verify, revert.** After the confirmed forward write and a dwell
  (15 s by default; `--dwell` 1 to 3600 s; `vmc-mode` at least 600 s), every
  record and plant configuration key is compared with the baseline. A change
  outside the written paths, or a new alarm, stops the test. Otherwise the
  original values are written back, checked the same way, and the journal
  entry is closed.
- **Stops are loud.** A stopped or interrupted test never reverts on its
  own: it prints what to restore by hand and leaves the journal entry open.
  No later test runs until you restore the state and close the entry with
  `--close ID`.

The journal is `--journal FILE`, else `$REHOM_WRITE_JOURNAL` (which must be
an absolute path), else `$XDG_STATE_HOME/aiorehom/write-tests/journal.jsonl`
(`XDG_STATE_HOME` defaults to `~/.local/state`). The path is always
resolved to an absolute one, so every run sees the same journal whatever the
working directory. Exit codes: 0 done, 1 another error (for `--status`: open
entries), 2 bad arguments, 4 refused or stopped.

### Credentials (macOS)

The probe reads the controller login from the macOS Keychain item `rehom-api`.
Create it in your own terminal, so the password never appears in a shell
history or a transcript:

```sh
security add-generic-password -s rehom-api -a <username> -w
# -w as the last argument makes `security` prompt for the password
```

Use `--username NAME` for a different account and `--service NAME` for a
different Keychain item. On other systems, use the library directly: it takes
the username and password as arguments.

## Development

Everything runs offline: tests use `aioresponses` mocks and aiohttp test
servers bound to `127.0.0.1`, and a guard in `tests/conftest.py` blocks every
non-loopback connection.

```sh
uv sync
uv run pytest -q                 # add --cov=aiorehom for coverage (>= 95 %)
uv run mypy --strict src
uv run ruff check . && uv run ruff format --check .
uv run rehom-probe replay tests/fixtures/20260925T102117Z   # the replay timeline, by eye
```

Test data:

- `tests/fixtures/20260925T102117Z/` is a sanitised (redacted and
  pseudonymised) 30-minute capture of one plant; see its `README.md`.
  `tests/test_e2e_fixture.py` replays it through the real client.
- `tests/golden/*.json` are input/output vectors for the `*_compat` functions,
  which reimplement calculations of the controller's web UI. They were
  generated by running the web UI's JavaScript on seeded inputs. Only the
  generated JSON is stored; no vendor code is included. See
  [`tools/golden/README.md`](https://github.com/frapposelli/aiorehom/blob/main/tools/golden/README.md).

## Disclaimer

aiorehom is an unofficial, independent project written for interoperability.
It is not affiliated with, endorsed by or supported by Rehom S.r.l. "Rehom", "Radiax" and "RadiaxWeb" are trademarks of
their respective owners and are used only to identify the compatible
equipment. Use it at your own risk: the software is provided "as is", without
warranty of any kind.

## License

MIT, see [LICENSE](https://github.com/frapposelli/aiorehom/blob/main/LICENSE).
