"""Normalize and reconcile WHOOP collections as one successful sync."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from strap_hub.ingest.writes import utc_now, write_transaction
from strap_hub.store import connect
from strap_hub.whoop_cloud.client import COLLECTIONS, WhoopClient
from strap_hub.whoop_cloud.oauth import OAuth, WhoopError

SCORE_STATES = {"SCORED", "PENDING_SCORE", "UNSCORABLE"}
ZONE_FIELDS = tuple(f"zone_{zone}_milli" for zone in ("zero", "one", "two", "three", "four", "five"))
STAGE_FIELDS = (
    "total_in_bed_time_milli", "total_awake_time_milli", "total_no_data_time_milli",
    "total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli",
    "sleep_cycle_count", "disturbance_count",
)
NEED_FIELDS = ("baseline_milli", "need_from_sleep_debt_milli", "need_from_recent_strain_milli",
               "need_from_recent_nap_milli")
LAST_SYNC = "whoop_last_sync_at"
SYNC_END = "whoop_sync_end"
OWNER = "whoop_owner_id"


def _utc(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError
    return stamp


def _local_day(stamp: datetime, offset: str) -> str:
    hours, minutes = int(offset[1:3]), int(offset[4:])
    delta = timedelta(hours=hours, minutes=minutes)
    return (stamp.astimezone(UTC) + (-delta if offset.startswith("-") else delta)).date().isoformat()


def _numeric_fields(payload: dict, names: tuple[str, ...], kind: type) -> dict:
    result = {name: payload.get(name) for name in names}
    for value in result.values():
        if value is None:
            continue
        if kind is float:
            if type(value) not in (float, int) or not math.isfinite(value):
                raise ValueError
        elif type(value) is not kind:
            raise ValueError
    return result


def _normalize(table: str, record: dict, observed_at: str) -> dict:
    try:
        score_state = record["score_state"]
        if score_state not in SCORE_STATES:
            raise ValueError
        score = (record.get("score") or {}) if score_state == "SCORED" else {}
        raw = record if score_state == "SCORED" else {k: v for k, v in record.items() if k != "score"}
        row = {
            "score_state": score_state,
            "raw_json": json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False),
            "updated_at": _utc(_timestamp(record["updated_at"])) if record.get("updated_at") else observed_at,
        }
        if table == "cloud_recovery":
            row.update(_numeric_fields(record, ("cycle_id",), int))
            if row["cycle_id"] is None:
                raise ValueError
            row["sleep_id"] = str(UUID(record["sleep_id"]))
            row.update(_numeric_fields(score, ("user_calibrating",), bool))
            row.update(_numeric_fields(score, (
                "recovery_score", "resting_heart_rate", "hrv_rmssd_milli", "spo2_percentage",
                "skin_temp_celsius",
            ), float))
            return row

        start = _timestamp(record["start"])
        end = _timestamp(record["end"]) if record.get("end") else None
        offset = record["timezone_offset"]
        if not re.fullmatch(r"[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]", offset) or (end and end < start):
            raise ValueError
        day_stamp = end if table == "cloud_sleep" else start
        row.update({
            "date": _local_day(day_stamp, offset) if day_stamp else None,
            "start": _utc(start), "end": _utc(end) if end else None, "timezone_offset": offset,
        })
        if table == "cloud_cycle":
            if type(record["id"]) is not int:
                raise ValueError
            row["id"] = record["id"]
            row.update(_numeric_fields(record, ("step_count",), int))
        else:
            row["id"] = str(UUID(record["id"]))
        if table == "cloud_sleep":
            if type(record["nap"]) is not bool:
                raise ValueError
            row["nap"] = record["nap"]
            row.update(_numeric_fields(score.get("stage_summary") or {}, STAGE_FIELDS, int))
            row.update(_numeric_fields(score.get("sleep_needed") or {}, NEED_FIELDS, int))
            row.update(_numeric_fields(score, (
                "respiratory_rate", "sleep_performance_percentage", "sleep_consistency_percentage",
                "sleep_efficiency_percentage",
            ), float))
        else:
            row.update(_numeric_fields(score, ("strain", "kilojoule"), float))
            row.update(_numeric_fields(score, ("average_heart_rate", "max_heart_rate"), int))
        if table == "cloud_workout":
            if not isinstance(record["sport_name"], str) or not record["sport_name"]:
                raise ValueError
            row["sport_name"] = record["sport_name"]
            row.update(_numeric_fields(record, ("sport_id",), int))
            row.update(_numeric_fields(score, (
                "percent_recorded", "distance_meter", "altitude_gain_meter", "altitude_change_meter",
            ), float))
            row.update(_numeric_fields(score.get("zone_durations") or {}, ZONE_FIELDS, int))
        return row
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        raise WhoopError(f"WHOOP returned an invalid {table} record") from None


def _metadata(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def last_sync(db: sqlite3.Connection) -> str | None:
    return _metadata(db, LAST_SYNC)


def _set_metadata(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _upsert(db: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    identity = "cycle_id" if table == "cloud_recovery" else "id"
    columns = tuple(rows[0])
    replacements = ", ".join(f"{key} = excluded.{key}" for key in columns if key != identity)
    if table == "cloud_recovery":
        replacements += (
            ", sleep_start = CASE WHEN cloud_recovery.sleep_id = excluded.sleep_id "
            "THEN cloud_recovery.sleep_start END"
        )
    placeholders = ", ".join(f":{key}" for key in columns)
    db.executemany(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({identity}) DO UPDATE SET {replacements} "
        f"WHERE {table}.raw_json IS NOT excluded.raw_json",
        rows,
    )


@dataclass(frozen=True)
class SyncWindow:
    start: str
    end: str
    advance_cursor: bool


def _remove_missing(db: sqlite3.Connection, table: str, rows: list[dict], window: SyncWindow) -> None:
    identity = "id"
    start = "start"
    if table == "cloud_recovery":
        # WHOOP dates recoveries by the related sleep's start.
        identity = "cycle_id"
        start = "sleep_start"
    db.execute(
        f"""DELETE FROM {table}
            WHERE {start} >= ? AND {start} < ?
            AND {identity} NOT IN (SELECT value FROM json_each(?))""",
        (window.start, window.end, json.dumps([row[identity] for row in rows])),
    )


def sync_window(db_path: str, since: date | None = None) -> SyncWindow:
    try:
        days = int(os.environ.get("WHOOP_SYNC_WINDOW_DAYS", "7"))
        if days < 1:
            raise ValueError
        overlap = timedelta(days=days)
    except (OverflowError, ValueError):
        raise WhoopError("WHOOP_SYNC_WINDOW_DAYS must be a positive number of days") from None

    now = datetime.now(UTC)
    cursor = None
    if Path(db_path).expanduser().exists():
        with closing(connect(db_path, read_only=True)) as db:
            cursor = _metadata(db, SYNC_END)
    anchor = min(_timestamp(cursor), now) if cursor else now
    try:
        incremental_start = anchor - overlap
    except OverflowError:
        raise WhoopError("WHOOP_SYNC_WINDOW_DAYS reaches before the supported calendar") from None
    start = datetime.combine(since, datetime.min.time(), UTC) if since else incremental_start
    if start >= now:
        raise WhoopError("WHOOP sync start must be before now")
    return SyncWindow(_utc(start), _utc(now), cursor is None or start <= incremental_start)


def sync(db_path: str, oauth: OAuth, since: date | None = None) -> dict:
    with oauth.session() as session:
        window = sync_window(db_path, since)
        client = WhoopClient(session)
        batches = {}
        owners = set()
        for table in COLLECTIONS:
            rows = []
            for record in client.records(table, window.start, window.end):
                owner = record.get("user_id")
                if owner is not None:
                    if type(owner) is not int:
                        raise WhoopError("WHOOP returned an invalid account identifier")
                    owners.add(str(owner))
                rows.append(_normalize(table, record, window.end))
            batches[table] = rows

        completed_at = utc_now()
        with write_transaction(db_path) as db:
            stored_owner = _metadata(db, OWNER)
            if len(owners) > 1 or (stored_owner and owners and stored_owner not in owners):
                raise WhoopError("Use a separate hub database for a different WHOOP account")
            if owners:
                _set_metadata(db, OWNER, owners.pop())
            for table, rows in batches.items():
                _upsert(db, table, rows)
            # Keep the start after the linked sleep disappears from a later response.
            db.execute(
                """UPDATE cloud_recovery SET sleep_start = cloud_sleep.start
                   FROM cloud_sleep WHERE cloud_sleep.id = cloud_recovery.sleep_id
                   AND cloud_recovery.sleep_start IS NOT cloud_sleep.start"""
            )
            for table, rows in batches.items():
                _remove_missing(db, table, rows, window)
            _set_metadata(db, LAST_SYNC, completed_at)
            if window.advance_cursor:
                _set_metadata(db, SYNC_END, window.end)
    return {"last_sync_at": completed_at, "row_counts": {table: len(rows) for table, rows in batches.items()}}


def store_status(db_path: str) -> dict:
    counts = dict.fromkeys(COLLECTIONS, 0)
    synced_at = None
    if Path(db_path).expanduser().exists():
        with closing(connect(db_path, read_only=True)) as db:
            db.execute("BEGIN")
            synced_at = last_sync(db)
            if _metadata(db, "schema_version") == "3":
                counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in counts}
    return {"last_sync_at": synced_at, "row_counts": counts}
