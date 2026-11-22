"""Serve the hub's read-only tools over MCP stdio."""

from __future__ import annotations

import json
import os
import sqlite3
from base64 import b64decode, urlsafe_b64encode
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping
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

from strap_hub.mcp.cloud import (
    BAND_WORKOUTS, RECOVERY, SLEEP_SCORE_FIELDS, has_cloud_schema, metric_table,
    sleep_record, token_status,
)
from strap_hub.store import connect
from strap_hub.store.cloud import EFFECTIVE_WORKOUTS
from strap_hub.whoop_cloud.sync import last_sync

mcp = MCPServer("strap-hub")
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
QUERY_TOOLS: dict[str, Callable[..., str]] = {}
ISO_DATE = StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
SLEEP_MINUTES = ("tst_min", "deep_min", "rem_min", "light_min")
MINUTE_KEYS = {*SLEEP_MINUTES, "nap_min", "duration_min"}
SLEEP_KEYS = (*SLEEP_MINUTES, "efficiency", "rmssd", "rhr", "resp_rate")
SLEEP_KEY_PARAMS = ", ".join("?" for _ in SLEEP_KEYS)
BAND_DISPLAY_KEYS = {
    *MINUTE_KEYS, *SLEEP_KEYS, "readiness", "strain", "skin_temp_z", "stress", "steps",
    "calories", "calories_total", "max_hr", "hrr_bpm",
}
DISPLAY_KEYS = {
    *BAND_DISPLAY_KEYS, "avg_hr", "kilojoule", "spo2_percentage",
    "skin_temp_celsius", "sleep_performance_percentage", "sleep_consistency_percentage",
    "percent_recorded", "distance_meter", "altitude_gain_meter", "altitude_change_meter",
}
# Normalize both UTC suffixes to sort fractional timestamps at full precision.
# Numeric UTC instant, so ordering is correct across offsets and fractional seconds.
WORKOUT_START = "julianday(start)"


class HubToolError(ToolError):
    """A tool error with a message safe to show to callers."""


class InvalidToolArguments(HubToolError):
    """An argument error discovered while running a tool."""


def _query_tool[Tool: Callable[..., str]](fn: Tool) -> Tool:
    """Register the same read-only function for MCP and the query CLI."""
    mcp.add_tool(fn, annotations=READ_ONLY, structured_output=False)
    QUERY_TOOLS[fn.__name__] = fn
    return fn


@contextmanager
def _read_store() -> Iterator[sqlite3.Connection]:
    configured_path = os.environ.get("HUB_DB_PATH")
    if not configured_path:
        raise HubToolError("Set HUB_DB_PATH to the existing hub database")

    try:
        connection = connect(Path(configured_path), read_only=True)
    except sqlite3.OperationalError as exc:
        raise HubToolError("Could not open the hub database read-only. Check HUB_DB_PATH and file permissions.") from exc

    with closing(connection) as db:
        db.execute("BEGIN")  # Keep the data and freshness in the same snapshot.
        yield db


def _today() -> date:
    name = os.environ.get("HUB_TZ")
    try:
        zone = ZoneInfo(name) if name else None
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HubToolError("HUB_TZ must be an installed IANA timezone name") from exc
    return datetime.now(zone).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidToolArguments("Dates must be valid calendar dates in YYYY-MM-DD format") from exc


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


def _preferred_source(sources: Iterable[str]) -> str:
    return "whoop_cloud" if "whoop_cloud" in sources else "band"


def _result(db: sqlite3.Connection, *, source: str = "band", **fields: object) -> str:
    as_of = last_sync(db) if source == "whoop_cloud" else _as_of(db)
    return json.dumps(
        {"as_of": as_of, "source": source, **fields}, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    )


def _metric_number(key: str, value: float) -> int | float:
    if key not in DISPLAY_KEYS:
        return value
    if key in MINUTE_KEYS:
        return round(value)
    rounded = round(value, 1)
    return int(rounded) if rounded.is_integer() else rounded


def _metrics(values: Mapping[str, float]) -> dict[str, int | float]:
    return {key: _metric_number(key, value) for key, value in values.items()}


@_query_tool
def data_freshness() -> str:
    """Check band freshness, the last WHOOP sync, credential state and stored dates."""
    with _read_store() as db:
        last_digest = _latest_log_time(db, "received_at")
        cloud_sync = last_sync(db)
        cloud_dates = """
                   UNION SELECT date FROM cloud_cycle
                   UNION SELECT date FROM cloud_recovery_day
                   UNION SELECT date FROM cloud_sleep WHERE date IS NOT NULL
                   UNION SELECT date FROM cloud_workout
        """ if has_cloud_schema(db) else ""
        coverage = db.execute(
            f"""SELECT MAX(date), COUNT(*) FROM (
                   SELECT date FROM day_metric UNION
                   SELECT date FROM sleep_segment UNION
                   SELECT date FROM workout {cloud_dates}
               )"""
        ).fetchone()
        return _result(
            db,
            source="whoop_cloud" if cloud_sync else "band",
            last_digest_at=last_digest,
            last_band_record_at=_as_of(db),
            newest_day=coverage[0],
            days_stored=coverage[1],
            cloud_last_sync_at=cloud_sync,
            **token_status(os.environ["HUB_DB_PATH"]),
        )


def _sleep_averages(nights: dict[str, dict[str, float]]) -> dict[str, float]:
    samples: dict[str, list[float]] = defaultdict(list)
    for metrics in nights.values():
        for key, value in metrics.items():
            samples[key].append(value)
    return {key: fmean(values) for key, values in samples.items()}


@_query_tool
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
    sources: dict[str, dict[str, str]] = defaultdict(dict)
    with _read_store() as db:
        rows = db.execute(
            f"""SELECT date, key, value, source FROM {metric_table(db)}
                WHERE date BETWEEN ? AND ? AND key IN ({SLEEP_KEY_PARAMS})
                ORDER BY date, key""",
            (previous_start.isoformat(), today.isoformat(), *SLEEP_KEYS),
        )
        for day, key, value, source in rows:
            sources[day][key] = source
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
            db,
            source=_preferred_source(source for daily in sources.values() for source in daily.values()),
            nights=[
                {"date": day, **_metric_provenance(sources[day]), **_metrics(metrics)}
                for day, metrics in nights.items()
            ],
            averages=_metrics(averages),
            change=_metrics(change),
        )


def _metric_provenance(sources: Mapping[str, str], *, default: str = "band") -> dict[str, object]:
    source = _preferred_source(sources.values()) if sources else default
    result: dict[str, object] = {"source": source}
    band_keys = sorted(key for key, origin in sources.items() if origin == "band")
    if source == "whoop_cloud" and band_keys:
        result["band_keys"] = band_keys
    return result


def _band_night(db: sqlite3.Connection, day: str) -> dict:
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

    totals: dict[str, float] = defaultdict(float)
    for start, end, stage in merged:
        totals[f"{stage}_min"] += (end - start).total_seconds() / 60
    if totals:
        totals["tst_min"] = sum(totals.get(stage, 0) for stage in ("light_min", "deep_min", "rem_min"))
    return {
        "segments": [
            {"start": _utc_label(start), "end": _utc_label(end), "stage": stage}
            for start, end, stage in merged
        ],
        "totals": {key: round(value) for key, value in totals.items()},
        "wake_count": sum(stage == "wake" for _, _, stage in merged) if merged else None,
    }


def _band_sleep_totals(band: Mapping[str, float], night: dict) -> dict:
    stage_totals = {key: band[key] for key in SLEEP_MINUTES if key in band}
    return _metrics(stage_totals) | night["totals"]


def _sleep_provenance(band_keys: Iterable[str], cloud_keys: Iterable[str], *, has_cloud_record: bool) -> dict:
    origins = dict.fromkeys(band_keys, "band") | dict.fromkeys(cloud_keys, "whoop_cloud")
    return _metric_provenance(origins, default="whoop_cloud" if has_cloud_record else "band")


@_query_tool
def sleep_night(date: Annotated[str, ISO_DATE, Field(description="Date the source attributes the night to")]) -> str:
    """Inspect one night's stage totals and any recorded stage timeline or disturbances."""
    day = _parse_date(date).isoformat()
    with _read_store() as db:
        night = _band_night(db, day)
        if has_cloud_schema(db):
            row = db.execute("SELECT * FROM cloud_sleep_night WHERE date = ?", (day,)).fetchone()
            if row is not None:
                cloud = sleep_record(row)
                band = dict(db.execute("SELECT key, value FROM day_metric WHERE date = ?", (day,)))
                band_totals = _band_sleep_totals(band, night)
                band_keys = band_totals.keys() - cloud["totals"].keys()
                if night["segments"]:
                    band_keys.update(("segments", "wake_count"))
                cloud_keys = set(cloud["totals"]) | (cloud.keys() & {"disturbance_count"})
                night["totals"] = band_totals | cloud["totals"]
                return _result(
                    db, date=day, **night, **_sleep_provenance(band_keys, cloud_keys, has_cloud_record=True),
                    disturbance_count=cloud.get("disturbance_count"), score_state=cloud["score_state"],
                )
        return _result(db, date=day, **night)


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
    raise InvalidToolArguments("Invalid workout cursor")


@_query_tool
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
        raise InvalidToolArguments("Workout 'from' must be on or before 'to'")

    bounds = (start.isoformat(), end.isoformat())
    after = _decode_workout_cursor(cursor) if cursor is not None else ()
    cursor_filter = f"AND ({WORKOUT_START}, id) < (?, ?)" if after else ""
    extras = ("kilojoule", "avg_hr", "percent_recorded", "distance_meter", "altitude_gain_meter",
              "altitude_change_meter")
    with _read_store() as db:
        table = f"({EFFECTIVE_WORKOUTS})" if has_cloud_schema(db) else BAND_WORKOUTS
        page = db.execute(
            f"""SELECT date, {WORKOUT_START} AS start_key, id, type, duration_min, strain, calories,
                       max_hr, zone_min_json, source, score_state, band_keys_json, {', '.join(extras)}
                FROM {table} WHERE date BETWEEN ? AND ? {cursor_filter}
                ORDER BY start_key DESC, id DESC LIMIT ?""",
            (*bounds, *after, limit + 1),
        ).fetchall()
        sessions: list[dict[str, object]] = []
        for row in page[:limit]:
            session: dict[str, object] = {"date": row["date"], "type": row["type"], "source": row["source"]}
            session.update(_metrics({
                key: row[key] for key in ("duration_min", "strain", "calories", "max_hr")
                if row[key] is not None
            }))
            if row["zone_min_json"] is not None:
                zones = json.loads(row["zone_min_json"])
                session["zone_min"] = {zone: round(value) for zone, value in zones.items()}
            if row["score_state"] is not None:
                session["score_state"] = row["score_state"]
                session.update(_metrics({key: row[key] for key in extras if row[key] is not None}))
                band_keys = json.loads(row["band_keys_json"])
                if band_keys:
                    session["band_keys"] = band_keys
            sessions.append(session)

        count, duration, calories, source = db.execute(
            f"""SELECT COUNT(*), SUM(duration_min), SUM(calories), MAX(source)
                FROM {table} WHERE date BETWEEN ? AND ?""", bounds,
        ).fetchone()
        totals: dict[str, object] = {}
        if count:
            totals = {"count": count, **_metrics({
                key: value for key, value in (("duration_min", duration), ("calories", calories))
                if value is not None
            })}
        total_zones: dict[str, float] = defaultdict(float)
        for (raw_zones,) in db.execute(
            f"SELECT zone_min_json FROM {table} WHERE date BETWEEN ? AND ? AND zone_min_json IS NOT NULL",
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
        return _result(db, source=source or "band", **result)


@_query_tool
def readiness_today() -> str:
    """Get today's readiness, HRV and resting heart rate, or the latest available day.

    Compares each metric with its average over the 30 days before
    that reading. Also provides the hub's local yesterday strain when available.
    """
    today = _today()
    yesterday = today - timedelta(days=1)
    with _read_store() as db:
        table = metric_table(db)
        day = db.execute(
            f"SELECT MAX(date) FROM {table} WHERE date <= ? AND key IN ('readiness', 'rmssd', 'rhr')",
            (today.isoformat(),),
        ).fetchone()[0]
        days = []
        sources = []
        if day is not None:
            rows = db.execute(
                f"SELECT key, value, source FROM {table} WHERE date = ? AND key IN ('readiness', 'rmssd', 'rhr')",
                (day,),
            ).fetchall()
            current = {key: value for key, value, _ in rows}
            provenance = _metric_provenance({key: source for key, _, source in rows})
            baseline_start = date.fromordinal(max(1, date.fromisoformat(day).toordinal() - 30))
            baseline_rows = db.execute(
                f"""SELECT key, AVG(value), MAX(source) FROM {table}
                    WHERE date >= ? AND date < ? AND key IN ('readiness', 'rmssd', 'rhr') GROUP BY key""",
                (baseline_start.isoformat(), day),
            ).fetchall()
            baseline = {key: value for key, value, _ in baseline_rows}
            sources = [*(row[2] for row in rows), *(row[2] for row in baseline_rows)]
            change = {key: current[key] - value for key, value in baseline.items() if key in current}
            days.append(
                {"date": day, **provenance, **_metrics(current), "baseline_30d": _metrics(baseline),
                 "change": _metrics(change)}
            )

        strain = db.execute(
            f"SELECT value, source FROM {table} WHERE date = ? AND key = 'strain'", (yesterday.isoformat(),)
        ).fetchone()
        strain_source = {}
        if strain:
            sources.append(strain["source"])
            strain_source["strain_yesterday_source"] = strain["source"]
        return _result(
            db, source=_preferred_source(sources), days=days,
            strain_yesterday=_metric_number("strain", strain["value"]) if strain else None, **strain_source,
        )


@_query_tool
def metric_trend(
    key: Annotated[str, Field(description="Stored metric key, for example rmssd, rhr or strain")],
    days: Annotated[int, Field(ge=1, le=365, strict=True, description="Calendar days ending today")] = 30,
) -> str:
    """Track one stored metric by day and return its mean, minimum and maximum."""
    today = _today()
    start = today - timedelta(days=days - 1)
    with _read_store() as db:
        rows = db.execute(
            f"SELECT date, value, source FROM {metric_table(db)} WHERE date BETWEEN ? AND ? AND key = ? ORDER BY date",
            (start.isoformat(), today.isoformat(), key),
        ).fetchall()
        values = [value for _, value, _ in rows]
        source = _preferred_source(origin for _, _, origin in rows)
        stats = {}
        if values:
            stats = {"mean": fmean(values), "min": min(values), "max": max(values)}
            if key in BAND_DISPLAY_KEYS or all(origin == "whoop_cloud" for _, _, origin in rows):
                stats = {name: _metric_number(key, value) for name, value in stats.items()}
        return _result(
            db,
            source=source,
            key=key,
            values=[
                {
                    "date": day, "source": origin,
                    "value": _metric_number(key, value) if origin == "whoop_cloud" or key in BAND_DISPLAY_KEYS else value,
                }
                for day, value, origin in rows
            ],
            **stats,
        )


@_query_tool
def strain_day(date: Annotated[str, ISO_DATE, Field(description="Local date of the activity cycle")]) -> str:
    """Get day strain, energy in kilojoules, heart rates and steps where recorded."""
    day = _parse_date(date).isoformat()
    with _read_store() as db:
        rows = db.execute(
            f"""SELECT key, value, source FROM {metric_table(db)} WHERE date = ?
                AND key IN ('strain', 'steps', 'kilojoule', 'avg_hr', 'max_hr', 'calories_total')""",
            (day,),
        )
        metrics = {}
        origins = {}
        for key, value, source in rows:
            metrics[key] = value
            origins[key] = source
        if "kilojoule" not in metrics and "calories_total" in metrics:
            metrics["kilojoule"] = metrics["calories_total"] * 4.184
            origins["kilojoule"] = origins["calories_total"]
        metrics.pop("calories_total", None)
        origins.pop("calories_total", None)
        extra = {}
        if has_cloud_schema(db):
            cycle = db.execute("SELECT score_state FROM cloud_cycle_day WHERE date = ?", (day,)).fetchone()
            if cycle is not None:
                extra["score_state"] = cycle["score_state"]
                origins["score_state"] = "whoop_cloud"
        return _result(db, date=day, **_metric_provenance(origins), **_metrics(metrics), **extra)


def _main_sleep_detail(cloud: dict, band: Mapping[str, float], night: dict) -> dict | None:
    cloud_totals = cloud.get("totals", {})
    band_totals = _band_sleep_totals(band, night)
    if "nap_min" in band:
        band_totals["nap_min"] = _metric_number("nap_min", band["nap_min"])
    band_metrics = _metrics({
        key: band[key] for key, cloud_key in (
            ("efficiency", "sleep_efficiency_percentage"), ("resp_rate", "respiratory_rate"),
        ) if key in band and cloud_key not in cloud
    })
    timeline = {key: night[key] for key in ("segments", "wake_count")} if night["segments"] else {}
    if not (cloud or band_totals or band_metrics):
        return None
    band_keys = (band_totals.keys() - cloud_totals.keys()) | band_metrics.keys() | timeline.keys()
    cloud_keys = set(cloud_totals) | (cloud.keys() & SLEEP_SCORE_FIELDS)
    if cloud.get("sleep_need_min"):
        cloud_keys.add("sleep_need_min")
    return {
        **cloud, **band_metrics, **timeline,
        "totals": band_totals | cloud_totals,
        **_sleep_provenance(band_keys, cloud_keys, has_cloud_record=bool(cloud)),
    }


def _recovery_detail(row: sqlite3.Row | None, band: Mapping[str, float]) -> dict | None:
    band_metrics = _metrics({key: band[key] for key in (*RECOVERY, "skin_temp_z") if key in band})
    cloud_metrics = {}
    state = {}
    if row is not None:
        state["score_state"] = row["score_state"]
        if row["score_state"] == "SCORED":
            cloud_metrics = _metrics({key: row[col] for key, col in RECOVERY.items() if row[col] is not None})
            if row["user_calibrating"] is not None:
                cloud_metrics["user_calibrating"] = bool(row["user_calibrating"])
    if not (state or band_metrics):
        return None
    return {
        **_sleep_provenance(band_metrics.keys() - cloud_metrics.keys(), cloud_metrics, has_cloud_record=bool(state)),
        **band_metrics, **cloud_metrics, **state,
    }


@_query_tool
def sleep_detail(date: Annotated[str, ISO_DATE, Field(description="Local wake date")]) -> str:
    """Get sleep stages, sleep need, performance, respiratory rate, naps and recovery."""
    day = _parse_date(date).isoformat()
    with _read_store() as db:
        cloud_sleep = {}
        recovery_row = None
        naps = []
        if has_cloud_schema(db):
            row = db.execute("SELECT * FROM cloud_sleep_night WHERE date = ?", (day,)).fetchone()
            if row is not None:
                cloud_sleep = sleep_record(row)
            naps = [
                sleep_record(row) for row in db.execute(
                    """SELECT *, total_light_sleep_time_milli + total_slow_wave_sleep_time_milli
                                     + total_rem_sleep_time_milli AS total_sleep_milli
                       FROM cloud_sleep WHERE date = ? AND nap = 1 ORDER BY end, id""",
                    (day,),
                )
            ]
            recovery_row = db.execute("SELECT * FROM cloud_recovery_day WHERE date = ?", (day,)).fetchone()

        band = dict(db.execute("SELECT key, value FROM day_metric WHERE date = ?", (day,)))
        sleep = _main_sleep_detail(cloud_sleep, band, _band_night(db, day))
        recovery = _recovery_detail(recovery_row, band)
        source = _preferred_source(part["source"] for part in (sleep, recovery, *naps) if part is not None)
        return _result(db, source=source, date=day, sleep=sleep, recovery=recovery, naps=naps)


def main() -> None:
    mcp.run(transport="stdio")
