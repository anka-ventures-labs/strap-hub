# Session log

## 2026-11-21: evaluation and plan

- Evaluated NOOP and OpenStrap plus other WHOOP projects. Chose OpenStrap Edge.
  Notes in `docs/EVALUATION.md`. Clones were kept in a temporary scratch folder only.
- Owner is on iOS with an Apple developer account. Chosen route: private Edge fork
  that pushes a JSON digest after each sync to a hub on the Mac mini.
- Wrote `docs/PLAN.md` (architecture, adopt-before-build inventory, contract outline,
  MECE workstreams, phases) and `docs/BACKLOG.md` (owner requests and open decisions).
- Verified on this Mac: Hermes installed with `mcp_servers` support and a native
  Home Assistant tool; Tailscale installed; no MQTT broker; no `HASS_URL` configured
  in `~/.hermes/.env`.
- No code written. The repo has no Git remote yet, so the branch is committed locally only.
- Next step: owner reviews the plan and answers the open decisions; owner runs P0;
  coordinator writes `docs/CONTRACT.md` (S0), then dispatches W1 and W2.

## 2026-11-21: owner decisions and repo setup

- Owner answers: agent is the owner's personal assistant; Home Assistant on the Mac mini;
  public repos under `anka-ventures-labs`, private data kept out; agent keeps its model.
- Found: the assistant is an app-managed Hermes profile with its own `mcp_servers` block and
  a gateway model alias. Home Assistant Core 2026.2 runs natively here and already
  has the community WHOOP cloud integration (about 50 sensors) and an asleep helper.
  Plan updated: quick win P-1 (assistant reads the existing HA sensors), W4 mirrors
  those metrics for cutover.
- Owner-specific paths, names and entity IDs are deliberately not recorded in this
  public repo.
- Repos: `anka-ventures-labs/strap-hub` (this repo, MIT) and `anka-ventures-labs/strap-edge`
  (fork of `OpenStrap/edge`, AGPL-3.0).
- Next: owner creates an HA long-lived token; coordinator writes `docs/CONTRACT.md` (S0).

## 2026-11-21: email fix, Home Assistant access, contract

- Rewrote `strap-hub` history so every commit uses the GitHub noreply author, and
  force-pushed both branches (owner-approved). This repo now sets that author locally.
- Home Assistant: created a dedicated admin user for the hub with `hass --script auth`
  (HA stopped and restarted through launchd; auth storage backed up first), then minted
  a long-lived token through the login flow. Credentials live only in the hub machine's
  private config directory (mode 600), outside Git.
- Found the HA WHOOP cloud integration has failed token refresh since at least
  2026-11-11, so P-1 is declined.
- S0 done: `docs/CONTRACT.md` (digest v1, store schema, agent tools),
  `src/strap_hub/store/db.py`, `pyproject.toml` (uv), `tests/test_store.py`.
  Verified: `uv run pytest` → 2 passed.
- Next: dispatch W1 (ingest + import + ops) and W2 (MCP server) in parallel; W3a
  (strap-edge push plumbing) in parallel against the contract.

## 2026-11-21 — W1 digest API and Edge backup import

- Added authenticated digest and health endpoints, a 2 MB streamed request limit,
  and transactional, version-aware writes. Identical digests keep their original
  receipt. An empty night retains its version so a stale push cannot repopulate it.
- Added the `serve` and `import` commands. Imports read Edge SQLite backups
  read-only, support gzip, report missing data, and skip workouts whose privacy
  flag is unavailable.
- Added the launchd template and operator instructions for configuration,
  tailnet HTTPS, SQLite snapshots, and imports.
- Verified `uv run pytest -q` with 25 passing tests on Python 3.14 and 3.12.
  The launchd template passed `plutil -lint`.
- Next step for the coordinator is to assemble W1 with the MCP and phone work.

## 2026-11-21 — W2 read-only MCP server

- Implemented the six contract tools with the locked MCP SDK's `MCPServer`
  stdio API. Each query uses a read-only SQLite snapshot and returns compact
  JSON with band freshness. Date windows use the hub's local timezone or `HUB_TZ`.
- Added Hermes profile configuration and tool examples in `docs/AGENTS.md`.
  Documented that tool results go to the profile's model provider.
- `uv run pytest -q` passed all 28 tests on Python 3.12.14 and 3.14.9.
  A stdio smoke test through `strap-hub-mcp` listed all six tools and read
  fixture freshness with both SDK connection modes.
- Ready for the coordinator to assemble with ingest and import.

## 2026-11-21 — Hub review fixes

- Migrated writable stores in place to schema v2 and secured the database,
  WAL, and SHM files on every writable open. The service sets umask 077.
- Added row ownership and version-based conflict rules. Push revocations and
  private backup sessions remove workouts and leave tombstones that block
  backup imports. A later phone push can restore the workout.
- Edge imports now read the served-night ceiling from the backup's view
  definition or `--algo-ceiling`, normalize hypnogram timestamps, convert
  zone arrays, and cap gzip expansion at 2 GiB by default.
- Added newest-first workout pagination with full-range totals, the 30-day
  readiness comparison, and full precision for unknown trend keys.
- `uv run pytest -q` passed all 73 tests on Python 3.14.9. The same suite
  passed on Python 3.12.14 in a separate uv environment.
- Local checkpoint ready for coordinator assembly.

## 2026-11-21: review round 1, fixes, CI

- Review round 1 (Sol, at 2f8e551): 5 blocking and 4 non-blocking findings. The
  coordinator wrote contract v1.1 (c5dea7a); a Sol builder fixed all nine (30bd7b8),
  merged as cb323f3. Verified: `uv run pytest -q` → 73 passed. A targeted re-check is running.
- Added `.github/workflows/test.yml`: pytest on GitHub-hosted Ubuntu, Python 3.12 and 3.14,
  locked uv environment.
- Deployment approach decided: install with `uv tool install` from a pinned revision,
  launchd service, `tailscale serve` (tailnet only), and register the MCP server with the
  assistant's profile through Hermes's native `hermes mcp add` (it probes before saving).

## 2026-11-21: review round 2 fixes (coordinator)

- Re-check (Sol, at cb323f3): 7 of 9 confirmed fixed. Still open: a delayed digest built
  before a privacy revocation could republish the workout; workout order used local date
  before UTC start; earlier commits still carried the assistant's persona name.
- Fixed by the coordinator: digests carry a per-install `seq`; tombstones keep a
  `seq_floor`, and only a push with a higher `seq` republishes. Timestamps are normalised
  to UTC on ingest; MCP orders workouts by `julianday(start)` then id, and the cursor
  follows. Contract updated (sections 2, 3, 4). New tests cover the delayed-digest,
  unnumbered-push and cross-time-zone cases. Verified: `uv run pytest -q` → 74 passed.
- History: `feat/hub-mvp` is squashed into one commit on top of `main` so no commit in
  the public PR carries the persona name; the old `docs/whoop-hub-plan` branch is deleted
  from GitHub. Old commit objects stay fetchable by exact SHA until GitHub purges them.

## 2026-11-21: PR #1 merged and hub deployed on the hub machine

- PR #1 merged by rebase at 20bc0ca after both CI checks passed on head f23d5b5.
  All commits on `main` use the noreply author.
- Deployed: a dedicated checkout pinned to 20bc0ca, `uv sync --locked --no-dev`, a
  launchd agent from the template, private config and store (mode 600), loopback bind.
  Health check works with the token, and returns 401 without it.
- Tailscale Serve: the root HTTPS port already proxied another local service, so the
  hub has its own tailnet HTTPS port. Funnel stays off. Verified over the tailnet.
- Agent: registered with `hermes mcp add` (it probed the server and found 6 tools), and
  the server was added to the profile's per-platform toolset allowlists. A fresh agent
  session called `data_freshness` and got the correct empty-hub answer. The profile
  config was backed up first, privately, outside Git.
- Docs: OPERATIONS.md (pinned deploy checkout, Serve port when 443 is in use) and
  AGENTS.md (toolset allowlist gotcha).

## 2026-11-21: strap-edge CI, first compile of the phone push

- Fork Actions enabled by the owner. CI ran on draft PR anka-ventures-labs/strap-edge#1.
- Run 1 (02e1cd4): our failures were the backup guard tests (new `hub_push_url` pref,
  a new secure-storage owner) and one analyzer lint. Fixed in 0ed3fc5: the hub URL is
  denylisted from backups, `lib/hub/hub_config.dart` is listed in `kSecretsNotBackedUp`,
  and zone minutes use a null-aware map entry. All `test/hub/` tests passed.
- Run 2 (0ed3fc5): every remaining failure already exists in upstream code at the fork
  base (06585ef0) or on upstream main:
  - welcome.dart lints
  - zh/fr/hi/ru string coverage
  - a raw InkWell in day_timeline
  - whoop_freeze_golden gen4/gen5 (also failing on upstream main 73685c14)
  - ui2_revision_reload: `bumpInsights()` sits 3189 characters into
    `importEdgeBackup`, past the test's 2600-character window, identical on the base
  Shard 2 passed.
- The PR stays draft until W3b (settings screen and rebrand) is assembled on top.

## 2026-11-22: WHOOP cloud source planned

- Owner will keep the official WHOOP app until the membership ends (~10 months) and wants the
  agent to read as much WHOOP data as possible, plus journal writes if feasible.
- Read the official developer docs: API v2 is read-only (recovery, sleep, cycles incl. steps,
  workouts, profile, body), OAuth with `offline` refresh tokens that rotate on every refresh,
  100 req/min and 10k/day, webhooks for sleep/workout/recovery only. No journal, behaviours,
  ECG or blood pressure endpoints, and no write endpoints at all.
- Likely cause of the failed Home Assistant WHOOP integration: refresh-token rotation; two
  clients refreshing the same grant invalidate each other. The hub must be the only holder.
- W6 dispatched to a Sol builder in its own worktree. Journal writes stay out of scope until the
  owner decides on the private-API route.

## 2026-11-22 W6 cloud storage and sync

- Reused the hub's private WAL connection and transactional write helper. Schema
  v3 adds four WHOOP tables, read projections and sync metadata. Synthetic
  fixtures cover migration, rescoring, pagination, partial failures and retries.
- Promoted the locked HTTPX version to a runtime dependency and added OAuthLib
  for authorization-code and refresh forms. The hub supplies the loopback
  callback, file lock and atomic token rotation. The maintained MIT
  `whoop` client adds Requests and Authlib and would still need the hub's
  persistence and date handling. [HTTPX transports](https://www.python-httpx.org/advanced/transports/)
  · [OAuthLib](https://github.com/oauthlib/oauthlib)
  · [Authlib OAuth clients](https://docs.authlib.org/en/stable/oauth2/client/http/index.html)
  · [WHOOP Python client](https://pypi.org/project/whoop/)
- Added local auth, sync, status and revoke commands plus the 15-minute
  LaunchAgent template. The fixture exercises authorization through revocation.
  Verify dashboard redirect support and first live sync during owner setup.

## 2026-11-22 W6 MCP assembly

- Added `strain_day` and `sleep_detail`. Existing MCP calls keep their input
  schemas and now read cloud data with band fallback and source labels.
  Workouts choose one list for each date. Pending cloud sleep remains visible
  with its score state, and cloud sleep reports aggregate stages and disturbances.
- Verified mixed-source precedence, UTC offset and wake-day mapping, freshness,
  workout pagination, read-only v2 compatibility, and band ingest and import.
  The full pytest suite passed on Python 3.12.14 and 3.14.9 with 86 tests each.
  The CLI help and LaunchAgent template also passed local checks.
- Owner setup still needs browser consent, a first live sync and verification
  of the scheduled job, in that order.
