# Evaluation: open-source WHOOP companions (2026-11-21)

Repos were cloned and read (READMEs, docs, issue threads, selected code). Nothing was
built or run against a band.

## Verdict

Start with **OpenStrap Edge** (<https://github.com/OpenStrap/edge>).

| | NOOP (`ryanbr/noop`, continuation) | OpenStrap Edge |
|---|---|---|
| Upstream | `NoopApp/noop` deleted; mirrors and forks remain. No commits since 2026-10-25. | Very active (about 2,150 commits since 2026-10-01). |
| WHOOP 5.0/MG | Mostly live HR. README: sleep stages unreliable, SpO₂ unavailable, HRV and skin temp incomplete. | Listed as full support; newer than 4.0 support. |
| MG ECG | No | Raw 100 Hz capture (not a diagnosis). |
| License | PolyForm Noncommercial (source-available) | AGPL-3.0 (edge, protocol, analytics); MIT (backend, research) |
| Data out | Read-only MCP + CLI over SQLite (macOS), Apple Health | HealthKit / Health Connect writes, SQLite export, scheduled backups, Tasker (Android), Shortcuts Sync Data (iOS) |

## Others found

- Goose (`b-nnett/goose`, `tigercraft4/goose`): WHOOP 5.0 client in Rust/Swift, archived 2026-07-23.
- OpenWhoop web app (Web Bluetooth), experimental.
- WHOOP 4.0 only: `christianmeurer/whoop-reader` (Python, MIT), `cs-balazs/gowhoop` (Go), `OpenStrap/research`, `bWanShiTong/reverse-engineering-whoop-post`.

## Firmware lock (main risk)

WHOOP 5.0/MG firmware 50.43.x can withhold history unless the band holds a
WHOOP-signed membership token (about 45 days). Live HR still works over the
standard heart-rate service. Evidence: `OpenStrap/edge#673`, `ryanbr/noop#2890`,
OpenStrap README firmware section (checked 2026-11-09 by the project). Reports
conflict; no downgrade is known.

## How OpenStrap works (short)

The band stores about one record per second (HR, beat intervals, PPG, accelerometer,
skin temp). Edge bonds over BLE, downloads history, decodes it with `OpenStrap/protocol`,
stores it in SQLite on the phone, and computes scores with `OpenStrap/analytics`
(published methods, deterministic). The optional backend is not needed.
