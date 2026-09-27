# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x releases may change the API).

## [Unreleased]

## [0.3.0] - 2026-09-27

The write path: opt-in, guarded commands, each sent once and confirmed by the
controller. Clients stay read-only unless they ask for writes. Season changes
and schedule (weekly program) edits are not writable. Temporary comfort is
implemented and tested offline, but has not been verified against a live
controller yet.

### Added

- Opt-in writes: `RehomClient(..., allow_writes=True)`. Without it a client is
  read-only, as before. `allow_writes` must be an actual `bool` (anything else,
  such as `"false"` or `1`, raises `TypeError`); `RehomClient.allow_writes`
  reports it.
- Commands on `RehomClient`:
  - `set_house_preset(preset)`: AUTO, or MANUAL at ECONOMY, PRE_COMFORT or
    COMFORT. A MANUAL level always writes that level's temperature as the
    regulated setpoint too, so it cannot be left stale. OFF is not offered.
  - `set_comfort_temperature(temperature)`: 0.1 °C steps, only while the house
    is MANUAL/COMFORT, within the season's limits (winter 16 to 28.5 °C,
    summer 18 to 35.5 °C).
  - `set_predictive(enabled)`: only while the house is in AUTO.
  - `set_zone_offset(zone_id, offset)`: whole degrees, -3 to +3, in any house
    mode.
  - `set_zone_mode(zone_id, setp)`: back to the schedule (`ZoneSetp.UNSET`) or
    a manual ECONOMY, PRE_COMFORT or COMFORT level. Level changes need the
    house in AUTO and no externally forced setpoint; never while a temporary
    comfort is running. Zones are never switched OFF.
  - `set_temporary_comfort(zone_id, minutes)`: COMFORT from now for 30 to 1440
    minutes in 30-minute steps, ending before midnight; an override still in
    force is extended. Needs the house in AUTO and the zone on its schedule.
    Not yet verified against a live controller.
  - `set_vmc_fan(vmc_id, value)`: a discrete `FanSpeed`, or a continuous value
    within the fan's range (and 0 to 100); not while the current mode fixes
    the fan speed.
  - `set_vmc_mode(vmc_id, mode)`: only modes the installer made selectable.
- Guards: every command is planned against the latest state and refused
  before anything is sent when the master panel is in control (crono mode),
  the controller's bus is down, the controller is read-only, the zone or VMC
  is unknown or offline, a VMC is in error or a forced mode, or a command's
  own precondition fails. The refusal is `RehomWriteRefusedError` with a
  stable, machine-readable `reason` (for example `"house_not_auto"`),
  suitable as a translation key. A read-only client refuses with
  `"writes_disabled"` and an unreachable controller with `"unavailable"`;
  before the first complete sync, or after `close()`, a command raises
  `RehomNotReadyError`.
- Execution: commands run one at a time, in call order. Each write is sent
  once and never retried, then confirmed from the controller's own reports
  (its live echo or, failing that, one fresh snapshot). A command returns
  `False` when the controller already reports the requested values (nothing
  is sent) and `True` once a sent write is confirmed; `client.state` shows it
  when the call returns. `RehomWriteNotConfirmedError` means the write was
  accepted (2xx) but the new values were not reported; any other error from
  the request itself (a timeout in particular) means it may or may not have
  landed.
- `ClientOptions.write_confirm_timeout` (20 s by default, 5 to 120 s) and
  `ClientOptions.write_poll_interval` (0.25 s).
- `aiorehom.writes`: the pure planners behind the commands (`plan_*`
  functions returning a `WritePlan`: the endpoint, the exact records, and the
  values the controller must report back). No I/O.
- The transport's write gate. The two bulk-write endpoints are on the exact
  allowlist but refused before any I/O unless the transport was created with
  `allow_writes=True` (an actual `bool`). `check_write_body()` accepts only
  bodies shaped like the payloads the official web client sends: per-group
  body templates (`WRITE_BODY_TEMPLATES`) over a fixed set of writable keys
  (`WRITABLE_INTERFACE_KEYS`), a value domain for every key, consistent values
  across the records of a body, and ASCII-only strings. The records are then
  rebuilt from the validated plain values, so what is sent is exactly what
  was checked. `ReadOnlyTransport.post_bulk_update()` sends one write and
  never retries it.
- Hard limits, enforced by the planners and again by the gate: a body carries
  at most 3 interface records (the largest write the official web client
  sends) or exactly one schedule-override record with a same-day window of at
  most 24 hours. House and zone OFF are never written, and neither are the
  timed rapid VMC modes (rapid renewal, rapid heating), not even when
  selectable.
- `rehom-probe write-test`: supervised write tests. One operation
  (`zone-offset`, `zone-mode`, `vmc-fan`, `vmc-mode`, `predictive`, `house`
  or `comfort-temp`) is written, verified and reverted: pre-flight checks, a
  write-ahead journal entry forced to disk before anything is sent, the
  forward write, a dwell, a comparison of every record and plant
  configuration key with the baseline, the revert (planned from the original
  values), and the same comparison again. Any failure, unexpected change or
  new alarm stops the test without a revert and leaves the journal entry
  open; no test runs until it is closed (`--status`, `--close ID`). It is a
  dry run by default; `--execute` sends the writes.
- `RehomClient.record_values()`: every interface and override record as raw
  values. It includes secrets: never log or persist it unredacted.
- `RehomWriteRefusedError` and `RehomWriteNotConfirmedError`, exported from
  `aiorehom`.

### Changed

- Read-only by default instead of by construction. The WebSocket is still
  receive-only, and the only new non-GET requests are the two gated bulk
  writes.
- A closed `ReadOnlyTransport` refuses every later request with
  `RehomConnectionError`, including one already waiting for its turn, and
  never opens a new session. `RehomClient.close()` refuses a write not yet
  sent and stops waiting for the confirmation of one already sent
  (`RehomWriteNotConfirmedError`).
- History request validation accepts ASCII digits only in times and unit ids.
- `ForbiddenRequestError` for a request that is not allowed now says "not on
  the allowlist" (it said "not on the read-only allowlist").

## [0.2.0] - 2026-09-26

The first public release: the read path.

### Added

- `RehomClient` and `ClientOptions`: a live, immutable model of one controller
  (`RehomState`: plant, zones, VMCs, actuators, fancoils, alarms, health,
  weather) kept up to date from the WebSocket and REST snapshots.
- WebSocket-first sync, batched `StateUpdate` notifications, periodic and
  on-demand resyncs with backoff, a REST fallback when the socket is down, and
  detection of half-open sockets by silence.
- Time-driven rebuilds at `state.next_change_at` (schedule slots, override
  expiry, alarm debounce, heartbeat deadlines) and a device clock with time
  zone and skew handling.
- `aiorehom.logic`: pure derived logic (presence, schedules, running program,
  overrides, zone target and mode, master preset and setpoint mismatch, lock,
  VMC, alarms with two-edge debouncing, heartbeat, weather). The `*_compat`
  functions, behaviour-compatible reimplementations of the web UI's
  calculations, are tested against golden vectors.
- `RehomClient.get_history()`, chunked to 24-hour windows.
- `RehomClient.dump()` diagnostics (secrets redacted) and `RehomState.as_dict()`.
- `aiorehom.replay` and `rehom-probe replay`: offline replay of a capture
  through the real client on a virtual clock.
- A sanitised capture fixture and an end-to-end replay test.

### Changed

- `rehom-probe inventory` and `diff` show secrets by presence only (never a
  length) and mask personal data by default.

## 0.1.0 - 2026-09-25

The read-only probe. Not published on its own; everything in it is part of
0.2.0.

### Added

- `ReadOnlyTransport`: an exact request allowlist, strict path validation,
  strict pacing, no redirects and a single request per call.
- A receive-only WebSocket capture.
- Secret redaction for records, dict payloads, WebSocket frames and HAR files,
  and pseudonymisation of personal data for test fixtures.
- `rehom-probe` with the `session`, `watch`, `latency`, `inventory`, `diff`,
  `redact-har` and `sanitize-fixtures` commands.

[Unreleased]: https://github.com/frapposelli/aiorehom/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/frapposelli/aiorehom/releases/tag/v0.3.0
[0.2.0]: https://github.com/frapposelli/aiorehom/releases/tag/v0.2.0
