# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (0.x releases may change the API).

## [Unreleased]

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

[Unreleased]: https://github.com/frapposelli/aiorehom/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/frapposelli/aiorehom/releases/tag/v0.2.0
