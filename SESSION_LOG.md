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
