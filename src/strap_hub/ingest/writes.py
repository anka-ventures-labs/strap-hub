"""Transactional writes shared by phone digests and backups."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from strap_hub.ingest.models import Day, Night, Workout
from strap_hub.store import connect

WriteSource = Literal["push", "import"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@contextmanager
def write_transaction(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(db_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        yield connection


def write_day(
    connection: sqlite3.Connection, day: Day, version: int, received_at: str, *, source: WriteSource
) -> None:
    connection.executemany(
        """
        INSERT INTO day_metric (date, key, value, algo_version, received_at, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (date, key) DO UPDATE SET
          value = excluded.value, algo_version = excluded.algo_version,
          received_at = excluded.received_at, source = excluded.source
        WHERE (
          (excluded.source = 'push' AND excluded.algo_version >= day_metric.algo_version)
          OR (day_metric.source = 'import' AND excluded.algo_version > day_metric.algo_version)
        )
          AND (excluded.algo_version, excluded.value, excluded.source)
              IS NOT (day_metric.algo_version, day_metric.value, day_metric.source)
        """,
        (
            (day.date, key, value, version, received_at, source)
            for key, value in day.metrics.items()
        ),
    )


def write_night(connection: sqlite3.Connection, night: Night, version: int, *, source: WriteSource) -> None:
    empty_key = f"empty_night:{night.date}"
    stored = connection.execute(
        'SELECT start, "end", stage, algo_version, source FROM sleep_segment WHERE date = ? ORDER BY start',
        (night.date,),
    ).fetchall()
    empty = connection.execute(
        "SELECT value FROM meta WHERE key = ?", (empty_key,)
    ).fetchone()
    versions = {(row["source"], row["algo_version"]) for row in stored}
    if empty is not None:
        # Schema v1 stored just the version. Like migrated rows, it defaults to push.
        empty_source, _, empty_version = empty["value"].rpartition(":")
        versions.add((empty_source or "push", int(empty_version)))
    if any(
        version < stored_version
        or (source == "import" and (stored_source == "push" or version == stored_version))
        for stored_source, stored_version in versions
    ):
        return

    segments = sorted(
        (segment.start, segment.end, segment.stage, version, source) for segment in night.segments
    )
    if segments != [tuple(row) for row in stored]:
        connection.execute("DELETE FROM sleep_segment WHERE date = ?", (night.date,))
        connection.executemany(
            'INSERT INTO sleep_segment (date, start, "end", stage, algo_version, source) VALUES (?, ?, ?, ?, ?, ?)',
            ((night.date, *segment) for segment in segments),
        )

    # An empty replacement still needs a version so a stale retry cannot restore it.
    if not segments:
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            WHERE excluded.value IS NOT meta.value
            """,
            (empty_key, f"{source}:{version}"),
        )
    elif empty is not None:
        connection.execute("DELETE FROM meta WHERE key = ?", (empty_key,))


MAX_PUSH_SEQ_KEY = "max_push_seq"


def max_push_seq(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (MAX_PUSH_SEQ_KEY,)).fetchone()
    return int(row[0]) if row is not None else 0


def record_push_seq(connection: sqlite3.Connection, seq: int | None) -> None:
    if seq is None or seq <= max_push_seq(connection):
        return
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (MAX_PUSH_SEQ_KEY, str(seq)),
    )


def write_workout(
    connection: sqlite3.Connection,
    workout: Workout,
    version: int,
    received_at: str,
    *,
    source: WriteSource,
    seq: int | None = None,
) -> None:
    tombstone = connection.execute(
        "SELECT seq_floor FROM workout_tombstone WHERE id = ?", (workout.id,)
    ).fetchone()
    if tombstone is not None:
        # Only a push numbered above the revocation's floor republishes the workout.
        # Imports, unnumbered pushes and stale or delayed pushes never do.
        if source != "push" or seq is None or seq <= tombstone[0]:
            return
        connection.execute("DELETE FROM workout_tombstone WHERE id = ?", (workout.id,))

    row = workout.model_dump(exclude={"zone_min"})
    row.update(
        zone_min_json=(
            json.dumps(workout.zone_min, sort_keys=True) if workout.zone_min is not None else None
        ),
        algo_version=version,
        received_at=received_at,
        source=source,
    )
    connection.execute(
        """
        INSERT INTO workout (
          id, date, start, "end", type, duration_min, strain, calories, max_hr,
          hrr_bpm, steps, zone_min_json, algo_version, received_at, source
        )
        VALUES (
          :id, :date, :start, :end, :type, :duration_min, :strain, :calories, :max_hr,
          :hrr_bpm, :steps, :zone_min_json, :algo_version, :received_at, :source
        )
        ON CONFLICT (id) DO UPDATE SET
          date = excluded.date, start = excluded.start, "end" = excluded."end",
          type = excluded.type, duration_min = excluded.duration_min, strain = excluded.strain,
          calories = excluded.calories, max_hr = excluded.max_hr, hrr_bpm = excluded.hrr_bpm,
          steps = excluded.steps, zone_min_json = excluded.zone_min_json,
          algo_version = excluded.algo_version, received_at = excluded.received_at, source = excluded.source
        WHERE (
          (excluded.source = 'push' AND excluded.algo_version >= workout.algo_version)
          OR (workout.source = 'import' AND excluded.algo_version > workout.algo_version)
        )
          AND (
            excluded.date, excluded.start, excluded."end", excluded.type, excluded.duration_min,
            excluded.strain, excluded.calories, excluded.max_hr, excluded.hrr_bpm, excluded.steps,
            excluded.zone_min_json, excluded.algo_version, excluded.source
          ) IS NOT (
            workout.date, workout.start, workout."end", workout.type, workout.duration_min,
            workout.strain, workout.calories, workout.max_hr, workout.hrr_bpm, workout.steps,
            workout.zone_min_json, workout.algo_version, workout.source
          )
        """,
        row,
    )


def revoke_workout(
    connection: sqlite3.Connection,
    workout_id: str,
    received_at: str,
    *,
    source: WriteSource,
    seq: int | None = None,
) -> None:
    # Every push numbered at or below this floor predates (or is) the revocation.
    floor = max(max_push_seq(connection), seq or 0)
    connection.execute("DELETE FROM workout WHERE id = ?", (workout_id,))
    connection.execute(
        """INSERT INTO workout_tombstone (id, revoked_at, source, seq_floor) VALUES (?, ?, ?, ?)
           ON CONFLICT (id) DO UPDATE SET revoked_at = excluded.revoked_at, source = excluded.source,
             seq_floor = MAX(workout_tombstone.seq_floor, excluded.seq_floor)""",
        (workout_id, received_at, source, floor),
    )


def log_digest(
    connection: sqlite3.Connection,
    *,
    received_at: str,
    source: str,
    days: int,
    nights: int,
    workouts: int,
    sent_at: str | None = None,
    app_version: str | None = None,
    algo_version: int | None = None,
    last_band_record_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO digest_log (
          received_at, sent_at, app_version, algo_version, last_band_record_at,
          days, nights, workouts, source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (received_at, sent_at, app_version, algo_version, last_band_record_at, days, nights, workouts, source),
    )
