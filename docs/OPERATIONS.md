# Operating strap-hub

## Install and configure

From the repository checkout on the hub, run `uv sync --locked --no-dev` with
Python 3.12 or later available to uv. Create a private config directory and file.

```sh
install -d -m 700 "$HOME/.config/strap-hub"
(umask 077; touch "$HOME/.config/strap-hub/.env")
chmod 600 "$HOME/.config/strap-hub/.env"
```

Set the following in `~/.config/strap-hub/.env`. Replace the token placeholder
with a random value, for example one generated locally by `openssl rand -hex 32`.
Set the same token in the phone app.

```dotenv
HUB_TOKEN=<random bearer token>
HUB_DB_PATH=~/.config/strap-hub/hub.db
HUB_BIND=127.0.0.1:8770
```

Use an absolute store path or one beginning with `~/` so every process opens the
same database. Each writable open sets the database and existing WAL and SHM
files to mode 600 and aborts if it cannot set those permissions. The service
runs with umask 077. The first writable open of a schema v1 store migrates it
in place and treats existing rows as phone pushes. To run the server in the
foreground from the checkout, use
`uv run --no-sync --env-file "$HOME/.config/strap-hub/.env" strap-hub serve`.

## Launch at login on macOS

Run the service from a dedicated checkout pinned to a released commit (for example
`~/.local/share/strap-hub/app`, `git checkout --detach <sha>`, then
`uv sync --locked --no-dev`), not from a development checkout whose branch changes.

Copy the template to `~/Library/LaunchAgents/com.anka.strap-hub.plist` and replace
the placeholders with absolute paths. Launchd does not expand `~` in the plist.

| Placeholder | Value |
| --- | --- |
| `@UV_BIN@` | The output of `command -v uv` |
| `@PROJECT_DIR@` | The repository checkout where you ran `uv sync` |
| `@ENV_FILE@` | The absolute path to `~/.config/strap-hub/.env` |
| `@STATE_DIR@` | The absolute path to the private `~/.config/strap-hub` directory |

Check and load the agent from the logged-in user session.

```sh
plist="$HOME/Library/LaunchAgents/com.anka.strap-hub.plist"
plutil -lint "$plist"
launchctl bootstrap "gui/$(id -u)" "$plist"
launchctl print "gui/$(id -u)/com.anka.strap-hub"
```

To unload it, run `launchctl bootout "gui/$(id -u)/com.anka.strap-hub"`.
Unload and bootstrap again after changing the configuration. Server logs are in
`serve.stdout.log` and `serve.stderr.log` inside the private config directory.

## Expose HTTPS inside the tailnet

Keep the hub bound to loopback. With the device joined to the tailnet, run
`tailscale serve --bg http://127.0.0.1:8770`. If the root HTTPS port (443) already
proxies another local service, check with `tailscale serve status` first and give the
hub its own HTTPS port instead of replacing it:
`tailscale serve --bg --https=8770 http://127.0.0.1:8770` (the phone then uses
`https://<host>:8770`). Follow the HTTPS setup prompt if
shown. Use the HTTPS base URL printed by `tailscale serve status` in the phone
app. The phone must be on the tailnet. Use Tailscale Serve only. Never enable
Funnel for the hub. To remove the root HTTPS proxy, run
`tailscale serve --https=443 off`. [Tailscale Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)

## Back up the hub store

Use SQLite's online backup API to capture a consistent copy while the service
runs, including transactions in WAL. From the checkout, run this command to
create a fresh private backup directory under `~/.config/strap-hub`.

```sh
uv run --no-sync --env-file "$HOME/.config/strap-hub/.env" python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import mkdtemp

store = Path(os.environ["HUB_DB_PATH"]).expanduser().resolve()
backup_dir = Path(mkdtemp(prefix="snapshot-", dir=Path.home() / ".config/strap-hub"))
backup = backup_dir / "hub.db"
backup.touch(mode=0o600, exist_ok=False)
with closing(sqlite3.connect(f"{store.as_uri()}?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect(backup)) as target:
        source.backup(target)
print(backup)
PY
```

Keep the backup in private, encrypted storage. It contains the hub's health
history. To use a restored snapshot, stop the service, set `HUB_DB_PATH` to a
private copy of the snapshot, and restart the service.

## Import an Edge backup

Use an Edge export or automatic backup. From the checkout, run either command
with your local backup path.

```sh
uv run --no-sync --env-file "$HOME/.config/strap-hub/.env" strap-hub import /path/to/edge.db
uv run --no-sync --env-file "$HOME/.config/strap-hub/.env" strap-hub import /path/to/edge.db.gz
```

The importer opens the source database read-only. For gzip input, it temporarily
expands the database into a private directory, then removes it. The default
expansion limit is 2 GiB. Use `--max-bytes N` to specify a different byte limit.
An oversized file aborts the import and the partial copy is removed. The command
prints accepted counts and reports missing tables, missing columns, and invalid
rows. It skips workouts when the database lacks a privacy flag and leaves missing
optional workout fields null. Invalid or empty heart-rate zone data leaves the
workout available without zone minutes.

Dates for imported workouts use SQLite `localtime` on the hub. They approximate
the phone's local dates if its time zone differs from the hub's. Daily metrics use
the backup's recorded algorithm versions when available. A night uses the highest
version at or below `--algo-ceiling N` if supplied, or the build ceiling recorded in
the backup's `v_hypnogram` view. If neither is available, the importer warns and
takes the highest version for each day. Hypnogram timestamps can be in seconds
or milliseconds. Rows with no recorded version use version 0.

Imports fill gaps and replace previously imported rows only at a strictly higher
algorithm version. A phone push can replace rows at an equal or higher version.
A private session in a backup or a workout revocation in a phone push removes
the workout and records a tombstone. An import skips tombstoned workouts. A
later phone push that includes the workout can restore it.

Imports record their receipt time in `digest_log`. A phone push supplies its
band's freshness timestamp. The hub logs each distinct push once. An identical
retry returns the same acceptance response and keeps the original receipt time.

## Sync WHOOP cloud data

1. Create or open a dedicated app in the WHOOP Developer Dashboard. Enable
   `read:recovery`, `read:cycles`, `read:workout`, `read:sleep`, `read:profile`,
   `read:body_measurement`, and `offline`. Register the exact redirect URL.
   Retire any other client using the same app, including a Home Assistant WHOOP
   integration. Each refresh rotates both tokens, so the hub must be the
   sole holder. [WHOOP OAuth documentation](https://developer.whoop.com/docs/developing/oauth/)
2. Add these settings to the hub's private `.env`. Keep client credentials out of Git.

   ```dotenv
   WHOOP_CLIENT_ID=<app client ID>
   WHOOP_CLIENT_SECRET=<app client secret>
   WHOOP_REDIRECT_URI=https://localhost:8771/whoop/callback
   WHOOP_SYNC_WINDOW_DAYS=7
   ```

   Set `HUB_WHOOP_TOKEN_PATH` if you need a different private location outside the
   checkout. The default adds `.whoop-token.json` to `HUB_DB_PATH`. The rotating
   token file, lock and any staging files are owner-only (mode 600). Their
   directory must belong to the service user and disallow group and world writes.
   The CLI creates a missing directory with mode 700 and refuses parent paths
   where another user could replace it. Keep the lock file in place while the
   app is configured. If token promotion fails after a refresh, fix the
   filesystem issue and run the sync again. It recovers a saved token pair from
   staging before making a request. If WHOOP rotated the pair and no valid
   replacement reached disk, stop the sync job and authorize again after
   clearing any incomplete staging file.
3. From the hub checkout, run authorization once. Open the printed URL in a
   browser and grant access. For an HTTPS or `whoop://` redirect, paste the
   complete redirected URL at the hidden prompt, even if the browser reports a
   connection error. The auth command defaults to
   `http://localhost:8771/whoop/callback` and listens on `127.0.0.1` for up to three
   minutes if you register a plain HTTP loopback URL. WHOOP documents HTTPS and
   `whoop://` redirect schemes. Use the HTTPS setup shown above if the dashboard
   rejects plain HTTP. `--paste` also works for a registered loopback URL when
   the listener cannot receive the response.

   ```sh
   uv run --no-sync --env-file "$HUB_ENV_FILE" strap-hub whoop-auth --paste
   uv run --no-sync --env-file "$HUB_ENV_FILE" strap-hub whoop-sync
   uv run --no-sync --env-file "$HUB_ENV_FILE" strap-hub whoop-status
   ```

   Set `HUB_ENV_FILE` to the absolute path of the private config file from the
   install step. `whoop-status` reports local token state, expiry, last successful
   sync, and stored row counts. The first sync loads the trailing window. Later
   polls overlap the last successful cutoff by `WHOOP_SYNC_WINDOW_DAYS`, including
   any time the job was offline. Use `--since YYYY-MM-DD` to backfill from that
   date at midnight UTC. Add `--dry-run` to print the planned window without
   contacting WHOOP or changing the database or tokens.
4. Install `ops/launchd/com.anka.strap-hub-whoop.plist.template` as a LaunchAgent
   using the same placeholders and login-session procedure as the digest
   service. Use the filename `com.anka.strap-hub-whoop.plist` and the label
   `com.anka.strap-hub-whoop`. The job runs at load and every 15 minutes with
   umask 077. Its logs are `whoop-sync.stdout.log` and `whoop-sync.stderr.log` in the
   private state directory. The sync retries transient reads with backoff. A
   rate-limit reset of more than 60 seconds ends the run; retry after the reset
   or let the scheduled job resume.
5. To disconnect, unload the sync LaunchAgent, then run
   `uv run --no-sync --env-file "$HUB_ENV_FILE" strap-hub whoop-revoke`.
   After WHOOP confirms revocation, the CLI removes the local token file.
   Previously synced rows stay in the hub for historical queries.
