"""Import scalar metrics, sleep stages and public sessions from Edge backups."""

import gzip
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from strap_hub.ingest.models import Day, Night, Workout
from strap_hub.ingest.writes import (
    log_digest,
    revoke_workout,
    utc_now,
    write_day,
    write_night,
    write_transaction,
    write_workout,
)

Reporter = Callable[[str], None]
DEFAULT_MAX_BYTES = 2 * 1024**3
SERVED_CEILING = re.compile(
    r"\bFROM\s+day_result\s+WHERE\s+algo_version\s*<=\s*([0-9]{1,19})\s+GROUP\s+BY\s+day_id\b",
    re.IGNORECASE,
)


@contextmanager
def open_backup(path: Path, max_bytes: int) -> Iterator[sqlite3.Connection]:
    with ExitStack() as stack:
        if path.name.endswith(".db.gz"):
            directory = stack.enter_context(TemporaryDirectory(prefix="strap-hub-import-"))
            unpacked = Path(directory) / "backup.db"
            unpacked.touch(mode=0o600, exist_ok=False)
            remaining = max_bytes
            with gzip.open(path, "rb") as compressed, unpacked.open("wb") as output:
                while chunk := compressed.read(min(1024**2, remaining + 1)):
                    if len(chunk) > remaining:
                        raise ValueError(f"gzip backup exceeds the {max_bytes}-byte expansion limit")
                    output.write(chunk)
                    remaining -= len(chunk)
            path = unpacked
        connection = stack.enter_context(
            closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True))
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("BEGIN")
        yield connection


def available_columns(
    connection: sqlite3.Connection, table: str, required: set[str], report: Reporter
) -> set[str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        report(f"Skipped {table}: table is missing.")
        return set()
    columns = {
        row["name"] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))
    }
    if missing := required - columns:
        report(f"Skipped {table}: missing columns {', '.join(sorted(missing))}.")
        return set()
    return columns


def import_metrics(
    source: sqlite3.Connection, target: sqlite3.Connection, received_at: str, report: Reporter
) -> int:
    if not available_columns(source, "metric_series", {"date", "key", "value"}, report):
        return 0

    versions = available_columns(
        source, "metric_series_version", {"date", "algo_version"}, report
    )
    if versions:
        rows = source.execute(
            """
            SELECT m.date, m.key, m.value, COALESCE(v.algo_version, 0) AS version
            FROM metric_series AS m LEFT JOIN metric_series_version AS v ON v.date = m.date
            """
        )
    else:
        rows = source.execute("SELECT date, key, value, 0 AS version FROM metric_series")

    dates = set()
    skipped = 0
    for row in rows:
        try:
            day = Day(date=row["date"], metrics={row["key"]: row["value"]})
        except ValidationError:
            skipped += 1
            continue
        version = row["version"]
        write_day(
            target, day, version if type(version) is int and version >= 0 else 0, received_at,
            source="import",
        )
        dates.add(day.date)

    if skipped:
        report(f"Skipped {skipped} metric_series rows with invalid or missing values.")
    return len(dates)


def epoch_seconds(value: int) -> str:
    if type(value) is not int:
        raise ValueError("expected integer epoch seconds")
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def hypnogram_timestamp(value: int | float) -> str:
    if type(value) not in (int, float):
        raise ValueError("expected a numeric hypnogram timestamp")
    seconds = value / 1_000 if value > 1e11 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


def recorded_algo_ceiling(source: sqlite3.Connection) -> int | None:
    # Edge exports the served-day view with the build's ceiling embedded as a
    # literal. Read that metadata without executing SQL from the backup view.
    row = source.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'view' AND name = 'v_hypnogram'"
    ).fetchone()
    if row and (match := SERVED_CEILING.search(row["sql"])):
        version = int(match[1])
        if version < 2**63:
            return version
    return None


def import_nights(
    source: sqlite3.Connection, target: sqlite3.Connection, report: Reporter, algo_ceiling: int | None
) -> int:
    columns = available_columns(source, "day_result", {"day_id", "payload_json"}, report)
    if not columns:
        return 0

    ordering = ["day_id"]
    version_column = "0"
    if "algo_version" in columns:
        version_column = "COALESCE(algo_version, 0)"
        ordering.append("algo_version DESC")
        if algo_ceiling is None:
            algo_ceiling = recorded_algo_ceiling(source)
            if algo_ceiling is None:
                report("Warning: no algorithm ceiling recorded; using the highest day_result version per day.")
    else:
        report("Skipped day_result.algo_version: column is missing. Using version 0.")
    if "computed_at" in columns:
        ordering.append("computed_at DESC")

    ceiling_filter = f"WHERE {version_column} <= ?" if algo_ceiling is not None else ""
    rows = source.execute(
        f"""
        SELECT day_id, payload_json, version FROM (
          SELECT day_id, payload_json, {version_column} AS version,
                 ROW_NUMBER() OVER (PARTITION BY day_id ORDER BY {", ".join(ordering)}) AS rank
          FROM day_result {ceiling_filter}
        ) WHERE rank = 1
        """,
        (algo_ceiling,) if algo_ceiling is not None else (),
    )
    imported = 0
    skipped = 0
    for row in rows:
        try:
            segments = json.loads(row["payload_json"])["series"]["hypnogram"]
            if not isinstance(segments, list):
                raise ValueError("expected a segment list")
            night = Night(
                date=row["day_id"],
                segments=[
                    {
                        "start": hypnogram_timestamp(segment["start"]),
                        "end": hypnogram_timestamp(segment["end"]),
                        "stage": segment["stage"],
                    }
                    for segment in segments
                ],
            )
        except (ValueError, TypeError, KeyError, OverflowError, OSError, RecursionError):
            skipped += 1
            continue

        version = row["version"]
        write_night(target, night, version if type(version) is int and version >= 0 else 0, source="import")
        imported += 1

    if skipped:
        report(f"Skipped {skipped} day_result nights with missing or invalid hypnograms.")
    return imported


SESSION_FIELDS = ("end_ts", "duration_min", "strain", "calories", "max_hr", "hrr_bpm", "steps", "zone_min_json")


def import_workouts(
    source: sqlite3.Connection, target: sqlite3.Connection, received_at: str, report: Reporter
) -> int:
    columns = available_columns(source, "sessions", {"id", "private"}, report)
    if not columns:
        return 0

    for (workout_id,) in source.execute("SELECT id FROM sessions WHERE private = 1 AND typeof(id) = 'text'"):
        revoke_workout(target, workout_id, received_at, source="import")

    if missing := {"start_ts", "type"} - columns:
        report(f"Skipped public sessions: missing columns {', '.join(sorted(missing))}.")
        return 0

    if missing := set(SESSION_FIELDS) - columns:
        report(f"Skipped sessions columns {', '.join(sorted(missing))}: missing; stored as NULL.")
    optional = [field if field in columns else f"NULL AS {field}" for field in SESSION_FIELDS]
    rows = source.execute(
        f"""
        SELECT id, start_ts, type, date(start_ts, 'unixepoch', 'localtime') AS local_date,
               {", ".join(optional)}
        FROM sessions
        WHERE private = 0
        """
    )
    imported = 0
    skipped = 0
    invalid_zones = 0
    for row in rows:
        try:
            zones = json.loads(row["zone_min_json"]) if row["zone_min_json"] is not None else None
            if isinstance(zones, list) and len(zones) == 5:
                zones = {f"z{index}": minutes for index, minutes in enumerate(zones, start=1)}
            elif zones in ([], {}):
                zones = None
        except (ValueError, TypeError, RecursionError):
            zones = None
            invalid_zones += 1
        try:
            workout = Workout(
                id=row["id"],
                date=row["local_date"],
                start=epoch_seconds(row["start_ts"]),
                end=epoch_seconds(row["end_ts"]) if row["end_ts"] is not None else None,
                type=row["type"],
                duration_min=row["duration_min"],
                strain=row["strain"],
                calories=row["calories"],
                max_hr=row["max_hr"],
                hrr_bpm=row["hrr_bpm"],
                steps=row["steps"],
                zone_min=zones,
            )
        except (ValueError, TypeError, OverflowError, OSError, RecursionError):
            skipped += 1
            continue
        if zones is not None and workout.zone_min is None:
            invalid_zones += 1
        # Edge sessions have no per-row algorithm version.
        write_workout(target, workout, 0, received_at, source="import")
        imported += 1

    if skipped:
        report(f"Skipped {skipped} sessions with invalid values.")
    if invalid_zones:
        report(f"Skipped invalid zone minutes for {invalid_zones} sessions.")
    return imported


def import_backup(
    path: str | Path, db_path: str | Path, *, report: Reporter = print,
    algo_ceiling: int | None = None, max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, int]:
    path = Path(path).expanduser()
    db_path = Path(db_path).expanduser()
    if not path.name.endswith((".db", ".db.gz")):
        raise ValueError("import requires an Edge .db or .db.gz backup")
    if algo_ceiling is not None and not 0 <= algo_ceiling < 2**63:
        raise ValueError("--algo-ceiling must be a nonnegative SQLite integer")
    if max_bytes < 0:
        raise ValueError("--max-bytes must be nonnegative")
    if db_path.exists() and path.samefile(db_path):
        raise ValueError("the import file and hub store must be different files")

    with open_backup(path, max_bytes) as source, write_transaction(db_path) as target:
        received_at = utc_now()
        counts = {
            "days": import_metrics(source, target, received_at, report),
            "nights": import_nights(source, target, report, algo_ceiling),
            "workouts": import_workouts(source, target, received_at, report),
        }
        log_digest(target, received_at=received_at, source="import", **counts)
    return counts
