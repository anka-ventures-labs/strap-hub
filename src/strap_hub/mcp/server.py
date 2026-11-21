"""Serve the hub's six read-only tools over MCP stdio."""

from __future__ import annotations

import json
import os
import sqlite3
from base64 import b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import fmean
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field, StringConstraints

from strap_hub.store import connect

mcp = MCPServer("strap-hub")
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ISO_DATE = StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SLEEP_MINUTES = ("tst_min", "deep_min", "rem_min", "light_min")
MINUTE_KEYS = {*SLEEP_MINUTES, "nap_min", "duration_min"}
SLEEP_KEYS = (*SLEEP_MINUTES, "efficiency", "rmssd", "rhr", "resp_rate")
SLEEP_KEY_PARAMS = ", ".join("?" for _ in SLEEP_KEYS)
DISPLAY_KEYS = {
    *MINUTE_KEYS, *SLEEP_KEYS, "readiness", "strain", "skin_temp_z", "stress", "steps",
    "calories", "calories_total", "max_hr", "hrr_bpm",
}
# Normalize both UTC suffixes to sort fractional timestamps at full precision.
# Numeric UTC instant, so ordering is correct across offsets and fractional seconds.
WORKOUT_START = "julianday(start)"


@contextmanager
def _read_store() -> Iterator[sqlite3.Connection]:
    configured_path = os.environ.get("HUB_DB_PATH")
    if not configured_path:
        raise ToolError("Set HUB_DB_PATH to the existing hub database")

    try:
        connection = connect(Path(configured_path), read_only=True)
    except sqlite3.OperationalError as exc:
        raise ToolError("Could not open the hub database read-only. Check HUB_DB_PATH and file permissions.") from exc

    with closing(connection) as db:
        db.execute("BEGIN")  # Keep the data and freshness in the same snapshot.
        yield db


def _today() -> date:
    name = os.environ.get("HUB_TZ")
    try:
        zone = ZoneInfo(name) if name else None
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ToolError("HUB_TZ must be an installed IANA timezone name") from exc
    return datetime.now(zone).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError("Dates must be valid calendar dates in YYYY-MM-DD format") from exc


def _latest_log_time(
    db: sqlite3.Connection, column: Literal["last_band_record_at", "received_at"]
) -> str | None:
    # SQLite's date functions can tie within a millisecond. Break ties with
    # the UTC text after removing the two common suffixes.
    row = db.execute(
        f"""SELECT {column} FROM digest_log WHERE {column} IS NOT NULL
            ORDER BY julianday({column}) DESC,
                     replace(replace({column}, '+00:00', ''), 'Z', '') DESC,
                     id DESC LIMIT 1"""
    ).fetchone()
    return row[0] if row else None


def _as_of(db: sqlite3.Connection) -> str | None:
    return _latest_log_time(db, "last_band_record_at")


def _result(as_of: str | None, **fields: object) -> str:
    return json.dumps({"as_of": as_of, **fields}, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _metric_number(key: str, value: float) -> int | float:
    if key not in DISPLAY_KEYS:
        return value
    if key in MINUTE_KEYS:
        return round(value)
    rounded = round(value, 1)
    return int(rounded) if rounded.is_integer() else rounded


def _metrics(values: Mapping[str, float]) -> dict[str, int | float]:
    return {key: _metric_number(key, value) for key, value in values.items()}


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def data_freshness() -> str:
    """Check when the hub last received data and how recently the band recorded data."""
    with _read_store() as db:
        last_digest = _latest_log_time(db, "received_at")
        coverage = db.execute(
            """SELECT MAX(date), COUNT(*) FROM (
                   SELECT date FROM day_metric UNION
                   SELECT date FROM sleep_segment UNION
                   SELECT date FROM workout
               )"""
        ).fetchone()
        as_of = _as_of(db)
        return _result(
            as_of,
            last_digest_at=last_digest,
            last_band_record_at=as_of,
            newest_day=coverage[0],
            days_stored=coverage[1],
        )


def _sleep_averages(nights: dict[str, dict[str, float]]) -> dict[str, float]:
    samples: dict[str, list[float]] = defaultdict(list)
    for metrics in nights.values():
        for key, value in metrics.items():
            samples[key].append(value)
    return {key: fmean(values) for key, values in samples.items()}


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def sleep_summary(
    days: Annotated[int, Field(ge=1, le=90, strict=True, description="Calendar days ending today")] = 7,
) -> str:
    """Summarize recent sleep stages, efficiency, HRV, resting HR and breathing rate.

    Returns nightly metrics, averages and changes from the preceding period of equal
    length. For this week, use the number of days from local Monday through today.
    """
    today = _today()
    start = today - timedelta(days=days - 1)
    previous_start = start - timedelta(days=days)
    start_label = start.isoformat()
    nights: dict[str, dict[str, float]] = defaultdict(dict)
    previous: dict[str, dict[str, float]] = defaultdict(dict)
    with _read_store() as db:
        rows = db.execute(
            f"""SELECT date, key, value FROM day_metric
                WHERE date BETWEEN ? AND ? AND key IN ({SLEEP_KEY_PARAMS})
                ORDER BY date, key""",
            (previous_start.isoformat(), today.isoformat(), *SLEEP_KEYS),
        )
        for day, key, value in rows:
            period = nights if day >= start_label else previous
            period[day][key] = value
        averages = _sleep_averages(nights)
        previous_averages = _sleep_averages(previous)
        change = {
            key: value - previous_averages[key]
            for key, value in averages.items()
            if key in previous_averages
        }
        return _result(
            _as_of(db),
            nights=[{"date": day, **_metrics(metrics)} for day, metrics in nights.items()],
            averages=_metrics(averages),
            change=_metrics(change),
        )


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def sleep_night(date: Annotated[str, ISO_DATE, Field(description="Date the app attributes the night to")]) -> str:
    """Inspect one night's timeline, minutes by sleep stage and number of wake periods."""
    day = _parse_date(date).isoformat()
    with _read_store() as db:
        segments = [
            (datetime.fromisoformat(start), datetime.fromisoformat(end), stage)
            for start, end, stage in db.execute(
                "SELECT start, end, stage FROM sleep_segment WHERE date = ?", (day,)
            )
        ]
        merged: list[tuple[datetime, datetime, str]] = []
        for start, end, stage in sorted(segments):
            if merged and merged[-1][2] == stage and start <= merged[-1][1]:
                last_start, last_end, _ = merged[-1]
                merged[-1] = (last_start, max(last_end, end), stage)
            else:
                merged.append((start, end, stage))

        minutes: dict[str, float] = defaultdict(float)
        for start, end, stage in merged:
            minutes[stage] += (end - start).total_seconds() / 60
        totals = {f"{stage}_min": round(value) for stage, value in minutes.items()}
        if minutes:
            totals["tst_min"] = round(sum(minutes.get(stage, 0) for stage in ("light", "deep", "rem")))
        return _result(
            _as_of(db),
            date=day,
            segments=[
                {"start": _utc_label(start), "end": _utc_label(end), "stage": stage}
                for start, end, stage in merged
            ],
            totals=totals,
            wake_count=sum(stage == "wake" for _, _, stage in merged) if merged else None,
        )


def _utc_label(timestamp: datetime) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


def _decode_workout_cursor(cursor: str) -> tuple[float, str]:
    try:
        parts = json.loads(b64decode(cursor, altchars=b"-_", validate=True))
        if (
            isinstance(parts, list) and len(parts) == 2
            and isinstance(parts[0], (int, float)) and not isinstance(parts[0], bool)
            and isinstance(parts[1], str)
        ):
            parts[1].encode("utf-8")
            return float(parts[0]), parts[1]
    except (ValueError, RecursionError):
        pass
    raise ToolError("Invalid workout cursor")


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def workouts(
    from_date: Annotated[
        Annotated[str, ISO_DATE] | None,
        Field(validation_alias="from", description="First local date, inclusive. Defaults to this week's Monday"),
    ] = None,
    to: Annotated[
        Annotated[str, ISO_DATE] | None,
        Field(description="Last local date, inclusive. Defaults to today"),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, strict=True, description="Maximum workouts per page")] = 50,
    cursor: Annotated[str | None, Field(description="The next_cursor from the previous page")] = None,
) -> str:
    """List workouts and activity totals for an inclusive range of local dates.

    The default range runs from this week's Monday through today. Each workout
    includes recorded duration, strain, calories, peak heart rate and zone minutes.
    Results are newest first. Use next_cursor for more workouts in the same range.
    """
    today = _today()
    start = _parse_date(from_date) if from_date is not None else today - timedelta(days=today.weekday())
    end = _parse_date(to) if to is not None else today
    if start > end:
        raise ToolError("Workout 'from' must be on or before 'to'")

    bounds = (start.isoformat(), end.isoformat())
    after = _decode_workout_cursor(cursor) if cursor is not None else ()
    cursor_filter = f"AND ({WORKOUT_START}, id) < (?, ?)" if after else ""
    with _read_store() as db:
        page = db.execute(
            f"""SELECT date, {WORKOUT_START} AS start_key, id, type, duration_min, strain, calories,
                       max_hr, zone_min_json
                FROM workout WHERE date BETWEEN ? AND ? {cursor_filter}
                ORDER BY start_key DESC, id DESC LIMIT ?""",
            (*bounds, *after, limit + 1),
        ).fetchall()
        sessions: list[dict[str, object]] = []
        for row in page[:limit]:
            session: dict[str, object] = {"date": row["date"], "type": row["type"]}
            session.update(_metrics({
                key: row[key] for key in ("duration_min", "strain", "calories", "max_hr")
                if row[key] is not None
            }))
            if row["zone_min_json"] is not None:
                zones = json.loads(row["zone_min_json"])
                session["zone_min"] = {zone: round(value) for zone, value in zones.items()}
            sessions.append(session)

        count, duration, calories = db.execute(
            """SELECT COUNT(*), SUM(duration_min), SUM(calories)
               FROM workout WHERE date BETWEEN ? AND ?""", bounds,
        ).fetchone()
        totals: dict[str, object] = {}
        if count:
            totals = {"count": count, **_metrics({
                key: value for key, value in (("duration_min", duration), ("calories", calories))
                if value is not None
            })}
        total_zones: dict[str, float] = defaultdict(float)
        for (raw_zones,) in db.execute(
            "SELECT zone_min_json FROM workout WHERE date BETWEEN ? AND ? AND zone_min_json IS NOT NULL",
            bounds,
        ):
            for zone, value in json.loads(raw_zones).items():
                total_zones[zone] += value
        if total_zones:
            totals["zone_min"] = {zone: round(value) for zone, value in total_zones.items()}
        result: dict[str, object] = {"workouts": sessions, "totals": totals}
        if len(page) > limit:
            anchor = (page[limit - 1]["start_key"], page[limit - 1]["id"])
            result["next_cursor"] = urlsafe_b64encode(json.dumps(anchor).encode()).decode()
        return _result(_as_of(db), **result)


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def readiness_today() -> str:
    """Get today's readiness, HRV and resting heart rate, or the latest available day.

    Compares each metric with its average over the 30 days before
    that reading. Also provides the hub's local yesterday strain when available.
    """
    today = _today()
    yesterday = today - timedelta(days=1)
    with _read_store() as db:
        day = db.execute(
            "SELECT MAX(date) FROM day_metric WHERE date <= ? AND key IN ('readiness', 'rmssd', 'rhr')",
            (today.isoformat(),),
        ).fetchone()[0]
        days = []
        if day is not None:
            current = dict(
                db.execute(
                    "SELECT key, value FROM day_metric WHERE date = ? AND key IN ('readiness', 'rmssd', 'rhr')",
                    (day,),
                )
            )
            baseline_start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 30))
            baseline = dict(
                db.execute(
                    """SELECT key, AVG(value) FROM day_metric
                       WHERE date >= ? AND date < ? AND key IN ('readiness', 'rmssd', 'rhr') GROUP BY key""",
                    (baseline_start.isoformat(), day),
                )
            )
            change = {key: current[key] - value for key, value in baseline.items() if key in current}
            days.append(
                {"date": day, **_metrics(current), "baseline_30d": _metrics(baseline), "change": _metrics(change)}
            )

        strain = db.execute(
            "SELECT value FROM day_metric WHERE date = ? AND key = 'strain'", (yesterday.isoformat(),)
        ).fetchone()
        return _result(
            _as_of(db), days=days, strain_yesterday=_metric_number("strain", strain[0]) if strain else None
        )


@mcp.tool(annotations=READ_ONLY, structured_output=False)
def metric_trend(
    key: Annotated[str, Field(description="Stored metric key, for example rmssd, rhr or strain")],
    days: Annotated[int, Field(ge=1, le=365, strict=True, description="Calendar days ending today")] = 30,
) -> str:
    """Track one stored metric by day and return its mean, minimum and maximum."""
    today = _today()
    start = today - timedelta(days=days - 1)
    with _read_store() as db:
        rows = db.execute(
            "SELECT date, value FROM day_metric WHERE date BETWEEN ? AND ? AND key = ? ORDER BY date",
            (start.isoformat(), today.isoformat(), key),
        ).fetchall()
        values = [value for _, value in rows]
        stats = {}
        if values:
            stats = {
                "mean": _metric_number(key, fmean(values)),
                "min": _metric_number(key, min(values)),
                "max": _metric_number(key, max(values)),
            }
        return _result(
            _as_of(db),
            key=key,
            values=[{"date": day, "value": _metric_number(key, value)} for day, value in rows],
            **stats,
        )


def main() -> None:
    mcp.run(transport="stdio")
