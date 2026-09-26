# aiorehom

Unofficial async Python client for the local API of the **Rehom Radiax**
controller ("RadiaxWeb" web server): radiant floor heating and cooling, VMC
units and dehumidifiers. It is the library behind the
[ha-rehom](https://github.com/frapposelli/ha-rehom) Home Assistant integration.

> **Status: alpha, read-only.** Version 0.2 reads the plant state (REST
> snapshots plus the controller's WebSocket). Writing (setpoints, modes,
> schedules) is not implemented yet. The API may change before 1.0.

This is an independent interoperability project. It is not affiliated with,
endorsed by or supported by Rehom S.r.l.; see the [disclaimer](#disclaimer).

## Features

- `RehomClient` keeps a live, immutable model of one controller (zones, VMCs,
  actuators, fancoils, alarms, health, weather) and publishes batched
  `StateUpdate` notifications.
- WebSocket-first sync with REST snapshots, periodic resyncs, a REST fallback
  when the socket is down, and time-driven rebuilds at schedule boundaries.
- Read-only by construction: an exact request allowlist, one request at a
  time, and a receive-only WebSocket (see [Safety](#safety)).
- Fully typed (`py.typed`, `mypy --strict`), asyncio and aiohttp based.
- `rehom-probe`, a read-only command-line probe that captures, inspects and
  replays controller sessions offline.

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
- There is no write API and no re-login in 0.2: an HTTP 401/403 raises
  `RehomAuthenticationError`.

| Module | Contents |
|---|---|
| `aiorehom.client` | `RehomClient`, `ClientOptions` |
| `aiorehom.models` | Frozen dataclasses of the state (`RehomState`, `Zone`, `Vmc`, `Alarm`, ...) |
| `aiorehom.enums`, `aiorehom.values` | Public enums; numeric-aware parsing and comparison of raw values |
| `aiorehom.logic` | Pure derived logic: presence, schedules, running program, overrides, zone target and mode, master preset, lock, VMC, alarms, heartbeat, weather |
| `aiorehom.transport` | `ReadOnlyTransport`, the only code that issues HTTP requests |
| `aiorehom.websocket` | Receive-only WebSocket connections |
| `aiorehom.replay` | Offline replay of a capture through the real client |
| `aiorehom.redact`, `aiorehom.pseudonymise` | Secret redaction; pseudonymisation of personal data for test fixtures |
| `aiorehom.cli` | The `rehom-probe` command |

## Safety

aiorehom is designed to look without touching anything. These guarantees are
enforced in code and covered by tests (`tests/test_transport_allowlist.py`,
`tests/test_websocket.py`, `tests/test_e2e_no_writes.py`).

- **Exact allowlist.** Each permitted request has its own typed method on
  `ReadOnlyTransport`; there is no generic request method. The allowlist
  contains only `GET`s of read endpoints (with fixed query keys where queries
  are allowed) and at most one login per transport. Any other method, path or
  query raises `ForbiddenRequestError` before a socket is opened. Paths are
  validated strictly and compared exactly (no prefix or glob matching, no
  `..`, `//`, percent-encoding, backslashes or non-ASCII characters).
- **No writes.** Nothing that changes the controller's state can be sent.
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

## `rehom-probe`

`rehom-probe` is a read-only diagnostic CLI. It uses the same allowlisted
transport, so it cannot write anything either.

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
