# Plan: personal WHOOP hub for Hermes agents and Home Assistant

Status: draft for owner review, 2026-11-21 (Asia/Ho_Chi_Minh).

## Goal

Replace the WHOOP subscription with an open-source stack. The owner's agents
(Hermes) and Home Assistant should be able to answer questions like
"how did I sleep this week?" and "what workouts did I do?" from the owner's
own WHOOP 5.0 MG data, with no WHOOP cloud involved.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Base app | OpenStrap Edge (AGPL-3.0) | Active, reads WHOOP 5.0/MG history, MG ECG capture. NOOP's upstream was deleted and its 5.0/MG support is mostly live HR. See `docs/EVALUATION.md`. |
| Phone | iPhone, own build signed with the owner's Apple developer account | Lets us add a push to the hub after each sync instead of relying on Apple Health exports or manual backups. |
| Hub | The owner's Mac mini | Always on, already runs Hermes, Home Assistant and Tailscale. |
| Agent | The owner's personal Hermes assistant | A desktop app hosts it as a Hermes profile with its own `mcp_servers` block. It uses whatever model its gateway alias resolves to; no separate model choice. |
| Home Assistant | The instance on the Mac mini (Core 2026.2, native venv, launchd) | Already in use; reached locally, no extra host. |
| Repos | Public, under `anka-ventures-labs`: `strap-hub` (this repo, MIT) and `strap-edge` (fork of `OpenStrap/edge`, AGPL-3.0, renamed per its `TRADEMARKS.md`) | Owner decision. Public means no personal data in either repo (see Privacy rule). |

## Architecture

```
WHOOP 5.0 MG ─BLE─▶ iPhone: Edge fork ── JSON digest after each sync ──▶ Mac mini hub
                     (bond, decode,        (HTTPS over Tailscale,           ├─ ingest API
                      score, store)         bearer token)                   ├─ SQLite store (durable history)
                                                                            ├─ MCP server ◀── Hermes agent
                                                                            └─ HA publisher ─▶ Home Assistant
```

The phone stays the only place that talks to the band and the only place that
computes scores. The hub stores history, serves agents and feeds Home Assistant.
It never recomputes OpenStrap's scores, so the agent and the app always agree.

### License boundary

The Edge fork (`strap-edge`) is public, so it is distributed: it stays AGPL-3.0,
keeps OpenStrap's copyright notices, and per OpenStrap's `TRADEMARKS.md` any
modified build uses a different name and icon ("based on OpenStrap" is fine). The hub is our own code and must not copy AGPL code from Edge,
protocol or analytics. It only consumes the JSON contract the fork sends. Table
and column names are facts about the data, not copied code.

### Privacy rule

Both repos are public. No health data, names, emails, tokens, hostnames, tailnet
names, entity IDs containing personal names, or profile paths go into Git.
Owner-specific values live in an ignored `.env` / local config on the Mac mini,
with only their shape in `.env.example`. Test fixtures are synthetic.

## Existing setup found on the Mac mini (2026-11-21)

- Home Assistant already runs the community WHOOP cloud integration
  (`prankstr/hassio-whoop`, cloud polling): about 50 sensors (recovery score,
  HRV, RHR, SpO2, skin temp, sleep performance and stages, day strain, last
  workout). It stops working once the WHOOP membership ends.
- Home Assistant also has an "asleep" `input_boolean` plus last-webhook helpers
  described as WHOOP-webhook driven. No automation in `automations.yaml` sets it;
  the writer is outside HA and still to be identified.
- The assistant's Hermes profile has no Home Assistant credentials yet.

Consequence: the hub's HA publisher (W4) mirrors the metrics the cloud
integration exposes today, under its own entity names, so anything that uses
them can switch over at cutover. The asleep helper is kept as an existing
contract; how the new stack sets it is decided in W4.

## Adopt before building (inventory)

| Need | Existing solution | Use |
|---|---|---|
| Agent tool access | Hermes `mcp_servers` (stdio or URL), `~/.hermes/hermes-agent/cli-config.yaml.example:1342` | Register the hub's MCP server per profile. No Hermes changes. |
| MCP server | Official MCP Python SDK (`mcp`, FastMCP) | Build the server on it. |
| Generic SQL over MCP | `mcp-server-sqlite` reference server | Rejected as the main interface: open SQL invites bad queries and large token use. Fixed tools instead. |
| Home Assistant from agents | Hermes `tools/homeassistant_tool.py` (`HASS_URL`, `HASS_TOKEN`) | Agents already read HA states. The hub publishes sensors; Hermes can read them natively. |
| WHOOP data before cutover | HA WHOOP cloud integration (`prankstr/hassio-whoop`), already configured | Quick win P-1: give the assistant HA credentials so it can answer from these sensors now, with zero new code. Retired at cutover. |
| Publishing HA sensors | HA REST `POST /api/states/<entity>` (no broker needed); MQTT discovery if a broker exists | REST first; no Mosquitto is installed on the Mac mini today. |
| Scheduled briefings | Hermes cron (`~/.hermes/cron`) | Morning sleep and recovery briefing, no new scheduler. |
| Transport and TLS | Tailscale (installed), `tailscale serve` | HTTPS inside the tailnet only. Never Funnel. |
| Phone-side data | Edge `metric_series`, `sessions`, `day_result` tables; `exportRecentSlice()` (`lib/data/recent_slice.dart:146`); auto-backup `.db.gz` (`lib/data/auto_backup.dart`) | The fork builds the digest from existing tables. The hub also imports a backup file, so it is useful before the fork exists. |
| Phone-side upload | None in Edge (only the legacy backend client, `lib/cloud/backend_client.dart`) | The one real gap. Thin hook after a sync completes. |
| Apple Health route | Edge writes sleep stages, RHR, HRV, resp rate, workouts to HealthKit | Fallback only; loses readiness and strain. |

## Data contract (v1, defined before any build)

Coordinator writes `docs/CONTRACT.md` first. Every workstream builds against it.

- `POST /v1/digest`, bearer token, JSON body:
  - `schema: 1`, `device`, `app_version`, `algo_version`, `sent_at`
  - `days[]`: `date` plus the scalar metrics (resting HR, HRV, readiness, strain,
    sleep minutes and stages, efficiency, resp rate, skin temp z, steps, calories)
  - `sleep[]`: per-night hypnogram segments
  - `workouts[]`: id, start/end, type, duration, strain, calories, max HR, zone minutes
  - `freshness`: last synced band timestamp
- Idempotent: upsert by `(date, key)`, workout `id` and night `date`; a newer
  `algo_version` replaces older values. Re-sending a day never duplicates it.
- Backup import: `hub import <file.db.gz>` maps the same tables into the same store.

## Parallel MECE workstreams

Step 0 is serial and owned by the coordinator: write `docs/CONTRACT.md` and the
hub store schema. After that, W1 to W3 run in parallel; each owns only its files.

| ID | Scope (owns) | Depends on | Route |
|---|---|---|---|
| W1 | Hub ingest and store: `hub/ingest/`, `hub/store/`, backup importer, launchd plist, `tailscale serve` config, tests with fixtures | Step 0 | Codex `gpt-6.2-sol` (high), worktree |
| W2 | MCP server and Hermes wiring: `hub/mcp/`, tools `sleep_summary`, `sleep_night`, `workouts`, `readiness_today`, `hrv_trend`, `data_freshness`; Hermes config snippet; tests against a fixture store | Step 0 (store schema) | Codex `gpt-6.2-sol` (high), worktree |
| W3 | `strap-edge` push: digest builder, upload after sync with retry queue, Keychain token, hub URL setting, rename and icon per `TRADEMARKS.md`, own bundle ID | Step 0 (contract) | Codex `gpt-6.2-sol` for Dart/Swift plumbing; the one settings screen row goes to Claude Opus (UI rule) |
| W4 | Home Assistant publisher: `hub/ha/`, sensors mirroring today's cloud-integration metrics; decide how the asleep helper is set | W1 | Codex `gpt-6.2-sol`, later |
| W5 | Hermes cron briefing: morning sleep and recovery summary | W2 | Coordinator config, later |

Assembly, end-to-end check (fixture digest → store → MCP answer), one
cross-provider review (`opencode-go/muse-spark-1.3-contributor`), pushes and the
PR stay with the coordinator. Exact model IDs are resolved from the live T3
catalogue and CodexBar headroom at dispatch time.

## Phases and acceptance

| Phase | Done when |
|---|---|
| P-1 Quick win | Declined: the HA WHOOP cloud integration is already broken (see `docs/BACKLOG.md`). |
| P0 Gate (owner) | Band firmware known; OpenStrap TestFlight installed; history (not just live HR) syncs for at least one night while the WHOOP membership is still active. |
| P1 Hub MVP | Hermes answers "how did I sleep the last 7 nights" and "workouts this week" from a real OpenStrap backup imported on the Mac mini. |
| P2 Automatic push | After a sync on the iPhone, the hub's `data_freshness` updates within a minute, with no manual step. |
| P3 Home Assistant | Recovery and sleep sensors visible in HA and updated after each push. |
| P4 Briefing | A morning summary arrives through Hermes on schedule. |
| Cancel WHOOP | P0 and P2 have held for a week. Then uninstall the WHOOP app so it cannot push firmware. |

## Risks

- **Firmware lock.** WHOOP 5.0/MG firmware 50.43.x can withhold history without a
  WHOOP-signed membership token. Reports conflict; no downgrade exists. Mitigation:
  P0 before anything else, keep the band off the WHOOP app after cutover.
- **Fork drift.** Edge changes daily. Keep the patch to one new module plus one
  call site after sync, rebase regularly.
- **iOS background limits.** Syncs run when iOS allows. Use Edge's Shortcuts
  `Sync Data` automations to add sync chances.
- **Health data privacy.** Hub binds to the tailnet only, token auth, store file
  mode 600, included in Mac backups. If an agent uses a cloud model, summaries go
  to that provider.
- **Scores are approximations.** OpenStrap's methods are published research, not
  WHOOP's algorithms, and not medical grade.

## Deferred (not in this plan)

- Recomputing scores on the hub with the `analytics` package.
- ECG waveform export to the hub (summary only first).
- MQTT discovery (only if a broker is added).
- Contributing an upstream "push to URL" feature to OpenStrap so the fork can go away.
