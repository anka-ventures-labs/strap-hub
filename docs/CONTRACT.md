# Contract v1: phone → hub digest, hub store, agent tools

This is the single interface between the three workstreams. W1 (ingest and store),
W2 (MCP server) and W3 (phone push) build against this file only. Changing it is a
coordinator decision; bump `schema` for breaking changes.

Field names mirror OpenStrap Edge's own tables (`metric_series`, `sessions`,
`day_result` hypnogram). Those names are facts about the data; no Edge code is
copied into the hub.

## 1. Transport

- `POST /v1/digest` on the hub, JSON body (UTF-8), `Content-Type: application/json`.
- Auth: `Authorization: Bearer <HUB_TOKEN>`. Wrong or missing token → `401`, body ignored.
- Reachable only inside the tailnet (`tailscale serve` in front of a `127.0.0.1` bind).
- Body limit 2 MB → `413`. Invalid JSON or schema → `422` with a short reason.
- Success → `200` `{"accepted": {"days": n, "nights": n, "workouts": n}, "freshness": "<iso>"}`.
- `GET /v1/health` → `200` `{"ok": true, "last_digest_at": "<iso>|null"}`, same auth.

The phone retries on network errors and `5xx` with backoff, and drops the payload
on `4xx` other than `401` (logged on the phone).

## 2. Digest payload (`schema: 1`)

```json
{
  "schema": 1,
  "seq": 42,
  "sent_at": "2026-11-21T07:05:00Z",
  "source": {"app": "strap-edge", "app_version": "x.y.z", "algo_version": 70, "device_family": "whoop5"},
  "freshness": {"last_band_record_at": "2026-11-21T06:58:12Z"},
  "days": [
    {"date": "2026-11-20", "metrics": {"rhr": 52, "rmssd": 61.2, "readiness": 74, "strain": 11.3}}
  ],
  "nights": [
    {"date": "2026-11-20", "segments": [
      {"start": "2026-11-19T15:42:00Z", "end": "2026-11-19T16:10:00Z", "stage": "light"}
    ]}
  ],
  "workouts": [
    {"id": "…", "date": "2026-11-20", "start": "2026-11-20T10:00:00Z", "end": "2026-11-20T10:45:00Z",
     "type": "running", "duration_min": 45, "strain": 9.8, "calories": 410,
     "max_hr": 172, "hrr_bpm": 31, "steps": 6100, "zone_min": {"z1": 5, "z2": 20}}
  ]
}
```

Rules:

- `date` is the phone's **local** calendar day (`YYYY-MM-DD`), the same day label Edge uses.
  Timestamps are ISO-8601 UTC.
- `days[].metrics` is a flat map of `metric_series` keys to numbers. Unknown keys are
  stored as-is (forward compatible). Keys the tools rely on:

  | Key | Meaning | Unit |
  |---|---|---|
  | `rhr` | resting heart rate | bpm |
  | `rmssd` | nightly HRV (RMSSD, pulse-derived) | ms |
  | `readiness` | OpenStrap readiness / recovery | 0–100 |
  | `strain` | day strain | OpenStrap scale |
  | `tst_min`, `deep_min`, `rem_min`, `light_min`, `nap_min` | sleep minutes | min |
  | `efficiency` | sleep efficiency | % |
  | `resp_rate` | sleeping respiratory rate | breaths/min |
  | `skin_temp_z` | skin temperature vs own baseline | z-score |
  | `stress` | day stress | OpenStrap scale |
  | `steps`, `calories`, `calories_total` | activity | count, kcal |

- `nights[].segments[].stage` ∈ `wake`, `light`, `deep`, `rem`, `unobserved`.
  A night is keyed by its `date` (the day the sleep is attributed to, as in Edge).
- `workouts[]` come from Edge `sessions` minus private sessions. `id` is Edge's session id.
  `zone_min` is a map `{"z1": min, …, "z5": min}`. Edge stores `zone_min_json` as a
  five-element array indexed from Z1 (or `[]` when zones are unavailable); the sender
  converts the array to this map and omits `zone_min` when it is empty or unreadable.
  Invalid zone data never drops the workout.
- `seq` (integer ≥ 1, optional for older senders, required for strap-edge): a
  per-install number that strictly increases with every new digest the phone builds.
  A retry of the same digest keeps its `seq`. It is how the hub orders digests for
  privacy decisions (section 3); the phone's clock is never used for that.
- `revoked_workout_ids[]` (optional, default `[]`): ids of workouts that were sent
  before and are now private or deleted on the phone. The hub deletes them and
  records a tombstone (section 3).
- The phone sends every day, night and workout that changed since its last
  acknowledged digest, plus the current day. A first push sends the last 30 days.
- Payloads contain no names, emails, device serials or location. Workout GPS routes
  are not sent in v1.

## 3. Idempotency and versions

- Days: upsert per `(date, key)`. Nights: replace all segments for that `date`.
  Workouts: upsert by `id`.
- Each stored row keeps the `algo_version` it came with. A row is only replaced by a
  payload with `algo_version >=` the stored one, so a stale retry can't overwrite a
  re-derived day.
- Re-sending the same digest changes nothing and still returns `200`.

### Sources and conflicts (push vs import)

Every stored day metric, night and workout records its `source` (`push` or `import`).

- A **push** is the phone's current state. It replaces a stored row when its
  `algo_version >=` the stored one, whatever the stored row's source.
- An **import** is a historical backup. It only fills gaps: it writes a row when none
  exists, or replaces an `import` row with a strictly higher `algo_version`. It never
  replaces a `push` row, and never replaces anything at an equal version.
- **Privacy revocation.** A workout id in `revoked_workout_ids`, or a session that an
  import finds with `private = 1`, is deleted from `workout` and recorded in
  `workout_tombstone` with a `seq_floor`: the highest push `seq` the hub had seen at
  that moment (or the revoking digest's own `seq`, if higher). Imports never write a
  tombstoned id. A push that includes the id clears the tombstone and stores it only if
  its `seq` is greater than `seq_floor` (the owner made it public again later). A push
  without `seq`, an exact retry, or a delayed digest built before the revocation
  (`seq <= seq_floor`) leaves the workout hidden.
- Within one digest, `revoked_workout_ids` are applied after `workouts`, so revocation
  wins if an id appears in both.
- Workouts in MCP results are ordered by UTC `start` (then `id`), newest first; local
  `date` is used only for range filtering. Timestamps are normalised to UTC on ingest.

## 4. Hub store (SQLite, `HUB_DB_PATH`, file mode 600, WAL)

The database and its `-wal` and `-shm` files must be mode 600 at all times. On every
writable open the hub sets them to 600 (including a pre-existing or restored store)
and refuses to write if it cannot. The hub process runs with `umask 077`.
Schema v1 stores are migrated in place to v2 (new columns default to `push`, new
tombstone table).

```sql
CREATE TABLE day_metric (date TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL,
  algo_version INTEGER NOT NULL, received_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'push', PRIMARY KEY (date, key));
CREATE TABLE sleep_segment (date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  stage TEXT NOT NULL, algo_version INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'push', PRIMARY KEY (date, start));
CREATE TABLE workout (id TEXT PRIMARY KEY, date TEXT NOT NULL, start TEXT NOT NULL,
  end TEXT, type TEXT NOT NULL, duration_min REAL, strain REAL, calories REAL,
  max_hr REAL, hrr_bpm REAL, steps INTEGER, zone_min_json TEXT,
  algo_version INTEGER NOT NULL, received_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'push');
CREATE TABLE workout_tombstone (id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL,
  source TEXT NOT NULL, seq_floor INTEGER NOT NULL DEFAULT 0);  -- source: 'push' | 'import'
CREATE TABLE digest_log (id INTEGER PRIMARY KEY, received_at TEXT NOT NULL,
  sent_at TEXT, app_version TEXT, algo_version INTEGER, last_band_record_at TEXT,
  days INTEGER, nights INTEGER, workouts INTEGER, source TEXT NOT NULL);  -- 'push' | 'import'
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- schema_version = 2
```

`workout.date` is the local day of `start`, taken from the phone (W3 sends it as part
of the workout; the hub never guesses the phone's timezone). W3 therefore adds
`"date"` to each workout object; it is required.

### Backup import

`strap-hub import <file.db|file.db.gz>` reads an OpenStrap Edge database copy
(manual export or automatic backup) read-only and writes the same rows:
`metric_series` → `day_metric`, `sessions` (non-private, with the local date computed
by SQLite `'localtime'` on the hub, documented as approximate for imports),
the served `day_result` hypnogram → `sleep_segment`. Logged with `source = 'import'`
and subject to the conflict rules in section 3.

- **Served night.** Edge serves, per day, the highest `day_result.algo_version` at or
  below the exporting build's algorithm version. The importer applies the same rule
  with a ceiling: `--algo-ceiling N` if given, otherwise any ceiling the backup itself
  records (look for it in Edge's served-day logic), otherwise the highest version
  present with a warning in the report.
- **Timestamp units.** Hypnogram segment `start`/`end` may be epoch seconds or epoch
  milliseconds (legacy rows). Values above `1e11` are milliseconds.
- **Size limit.** A `.db.gz` is decompressed with a byte ceiling (default 2 GiB,
  `--max-bytes`); exceeding it aborts the import and deletes the partial file.
- The hub must tolerate missing tables or columns (older or newer Edge schemas) and
  report what it skipped.

## 5. Agent tools (MCP, stdio, read-only)

All tools read the store only. Dates are `YYYY-MM-DD`; ranges are inclusive. Every
result includes `"as_of"` (latest `last_band_record_at`) so the agent can say how
fresh the data is. Results stay small: no raw per-second data.

| Tool | Args | Returns |
|---|---|---|
| `data_freshness` | — | last digest time, last band record time, newest day with data, days stored |
| `sleep_summary` | `days` (1–90, default 7) | per night: total, deep, REM, light minutes, efficiency, HRV, RHR, resp rate; plus averages and the change vs the previous equal period |
| `sleep_night` | `date` | the night's stage segments merged into a compact timeline, totals, wake count |
| `workouts` | `from`, `to` (default: this week, Monday to today), `limit` (1–100, default 50), `cursor` (opaque, optional) | up to `limit` workouts (date, type, duration, strain, calories, max HR, zone minutes), newest first; `next_cursor` when more remain; `totals` always cover the whole requested range |
| `readiness_today` | — | today's (or latest) readiness, HRV and RHR, each vs its 30-day baseline (the 30 calendar days before the reported day), strain yesterday |
| `metric_trend` | `key`, `days` (1–365, default 30) | daily values for one `day_metric` key, plus mean, min, max. Known keys are rounded for display; unknown keys keep stored precision |

Errors are explicit: no data in range returns an empty list with `as_of`, never a guess.
