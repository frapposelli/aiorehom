# Fixture `20260925T102117Z`

**Reviewed by the maintainer before publication (2026-09-26).**

## Provenance

- A read-only `rehom-probe` capture of one Rehom RadiaxWeb controller, taken on 2026-09-25:
  - REST snapshot at 10:21:23Z;
  - a 30-minute receive-only WebSocket session, 10:22:06.806Z to 10:51:58.168Z (563 frames).
- Produced with `rehom-probe sanitize-fixtures` (redaction plus pseudonymisation; see `SANITISED.txt`). Nothing was copied from the raw capture.
- Only the files the replay needs are kept: `alive.json`, `config.json`, `interface.json`, `overrides.json`, `plant_conf.json` and `ws.jsonl`, plus `SANITISED.txt`. `SANITISED.txt` is the sanitiser's own summary and also lists files of the full sanitised capture that are deliberately not included here.

## Sanitisation

Redaction (secrets):
- The sanitiser replaced every secret-like value with a redaction marker.
- The records and fields that held only such markers were then removed, because the replay does not need them. The fixture holds no redaction markers.

Pseudonymisation (personal data):
| Data | Fixture value |
|---|---|
| Zone names | `Zona NNN` |
| VMC names | `VMC NNN` |
| Locality | `City` |
| GPS position | `45.0000000,10.0000000` |
| Weather-service location data | removed, including the ground-level and sea-level pressure of the forecast frames (the other weather values are kept) |
| MAC address | `02:00:00:…` |
| IPv4 addresses | `192.0.2.0/24` |
| Serial numbers and custom ids | salted fakes |
| Controller host name | `host-….example` |

The `mono` field of `ws.jsonl` (the capturing machine's uptime) was rebased to start at `0.0`.

## Verification

- A one-off scan, not stored, compared this directory against the personal values of the raw capture. The raw values were held in memory only, and the scan printed counts only.
  - Field-aligned: 34 personal fields were checked, and 0 are still equal.
  - Whole-token free text (text of 3 or more characters, numbers of 8 or more digits): 19 literals were searched, with 0 hits.
- After that scan, only removals were made: the records and fields that held only redaction markers (`interface.json`, `config.json`), and the `sea_level` and `grnd_level` pressures of the 40 weather-forecast frames of `ws.jsonl` (their difference gave the site's elevation).
- `tests/test_e2e_fixture_hygiene.py` re-checks the redaction and pseudonymisation invariants on every test run.

## Use

- `tests/test_e2e_fixture.py` and `tests/test_replay_synthetic.py` replay this capture through the real `RehomClient`, offline, on a virtual clock (`aiorehom.replay`).
- Human timeline: `uv run rehom-probe replay tests/fixtures/20260925T102117Z`
