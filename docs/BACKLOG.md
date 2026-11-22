# Backlog

Remaining work only. History goes in `SESSION_LOG.md`. A row closes only when it is
live for the owner (with evidence) or explicitly declined (with the reason).

## Owner requests

| # | Date | Request (owner's words, condensed) | State | Waits on |
|---|---|---|---|---|
| R1 | 2026-11-21 | Replace the expensive WHOOP 5.0 MG subscription with something open source and self-hosted | Not started (plan drafted) | P0 gate, then P1–P2 |
| R2 | 2026-11-21 | Compare NOOP and OpenStrap, find others, pick one to start with | Done: OpenStrap Edge chosen (`docs/EVALUATION.md`) | — |
| R3 | 2026-11-21 | Connect my Hermes agents to my WHOOP (sleep performance, workouts this week) | Partly built: the agent is connected to the live hub; no WHOOP data flows yet | P0, W3a/W3b, phone install |
| R4 | 2026-11-21 | Plug it into Home Assistant | Not started (hub HA user and token ready) | W1, W4 |
| R5 | 2026-11-21 | Plan this out | Done: `docs/PLAN.md` | — |
| R6 | 2026-11-21 | Connect the owner's personal assistant agent | Partly built: MCP registered and verified with a tool call; empty store | Data (P0, W3) |
| R7 | 2026-11-21 | Public repo on the anka org; keep private data out of the repo entirely | Done: repos created; history rewritten to the GitHub noreply author; privacy rule in `docs/PLAN.md`. Residual: old commit objects stay fetchable by exact SHA until GitHub purges them | — |
| R8 | 2026-11-21 | Use whatever model the Hermes agent uses | Decided: the assistant keeps the model its profile already uses | — |
| R9 | 2026-11-22 | The agent has as much control over my WHOOP as it can: mostly read, plus writes such as logging my journal each day (owner has a WHOOP developer account and will use the official app until the membership ends) | Not started; read path in W6. Journal write: official API has no write endpoints; only a private reverse-engineered API exists (terms and breakage risk), awaiting owner decision | W6; owner decision on journal |

## Work rows

| ID | Item | State | Waits on |
|---|---|---|---|
| P-1 | Assistant reads the existing HA WHOOP cloud sensors | Declined 2026-11-21: the HA WHOOP cloud integration has failed to sign in since at least 2026-11-11 (all values unavailable); fixing it needs the owner's WHOOP login and it is retired at cutover anyway | — |
| P0 | Firmware check + OpenStrap TestFlight history sync while membership active | Owner action | Owner |
| S0 | Write `docs/CONTRACT.md` (digest JSON v1) and hub store schema | Done: contract, `src/strap_hub/store/`, tests | — |
| W1 | Hub ingest, store, backup importer, launchd, `tailscale serve` | Built and merged into `feat/hub-mvp`; review fixes in progress | Fix round |
| W2 | MCP server + Hermes config | Built and merged into `feat/hub-mvp`; review fixes in progress | Fix round |
| RV1 | Review of `feat/hub-mvp` (Sol): round 1 + targeted re-check; all findings fixed | Done; PR #1 merged (20bc0ca) | — |
| DEP1 | Deploy hub on the hub machine (launchd, tailnet HTTPS, agent MCP registration) | Live: running, reachable in the tailnet, the agent calls its tools; store still empty | P0 (real data) |
| W3a | strap-edge: digest builder, outbox, push after sync (no UI) | Built (0da9409, 02e1cd4), draft PR anka-ventures-labs/strap-edge#1; not compiled | Owner: enable Actions on the fork (web-only button), then CI |
| W3b | strap-edge: hub settings screen, rename to Strap Edge, new icon, OpenStrap credit (Opus) | In progress (separate T3 thread, branch feat/hub-settings-rebrand on top of feat/hub-push) | — |
| W3c | strap-edge: own bundle ID and signing with the owner's Apple developer team, device build, install on iPhone | Planned | W3a/W3b CI green; disk space or build volume for an iOS build |
| W6 | WHOOP cloud source: official API v2 OAuth (offline), poller, store, MCP tools for strain/steps and sleep detail (Sol) | In progress | WHOOP client ID in the owner's secret store; one-time browser consent |
| W4 | Home Assistant publisher (mirror cloud-integration metrics; asleep helper) | Planned; dedicated HA user and token created on the hub machine (outside Git) | W1 |
| Q1 | Identify what currently sets HA's asleep helper (outside HA) | Planned | — |
| W5 | Hermes morning briefing via Hermes cron | Planned | W2 |
| D1 | Contract: deleting a day metric or a whole night on the phone (no tombstone today; the hub keeps the last value) | Deferred | Real need |
| D2 | Contract: splitting a digest that would exceed 2 MB (today a 413 is dropped by the phone; 30 days is far below the limit) | Deferred | Real need |

## Owner decisions

Answered 2026-11-21: agent is the owner's personal assistant; Home Assistant is the one on
the Mac mini; repos are public under `anka-ventures-labs` with no private data; the
agent keeps the model its Hermes profile already uses.

Open:
None (W3a thread approved 2026-11-21).
