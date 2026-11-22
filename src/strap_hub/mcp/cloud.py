"""Read-only cloud projections and compatibility with pre-v3 hub databases."""

import sqlite3

from strap_hub.whoop_cloud.oauth import TokenStore, WhoopError, token_path

BAND_METRICS = "(SELECT date, key, value, 'band' AS source FROM day_metric)"
BAND_WORKOUTS = """(SELECT *, 'band' AS source, NULL AS score_state, NULL AS kilojoule, NULL AS avg_hr,
    NULL AS percent_recorded, NULL AS distance_meter, NULL AS altitude_gain_meter,
    NULL AS altitude_change_meter, NULL AS band_keys_json FROM workout)"""
SLEEP_TOTALS = {
    "tst_min": "total_sleep_milli",
    "light_min": "total_light_sleep_time_milli",
    "deep_min": "total_slow_wave_sleep_time_milli",
    "rem_min": "total_rem_sleep_time_milli",
    "wake_min": "total_awake_time_milli",
    "unobserved_min": "total_no_data_time_milli",
    "in_bed_min": "total_in_bed_time_milli",
}
SLEEP_NEED = {
    "baseline": "baseline_milli",
    "debt": "need_from_sleep_debt_milli",
    "recent_strain": "need_from_recent_strain_milli",
    "recent_nap": "need_from_recent_nap_milli",
}
SLEEP_SCORE_FIELDS = (
    "disturbance_count", "sleep_cycle_count", "sleep_performance_percentage",
    "sleep_consistency_percentage", "sleep_efficiency_percentage", "respiratory_rate",
)
RECOVERY = {
    "readiness": "recovery_score",
    "rmssd": "hrv_rmssd_milli",
    "rhr": "resting_heart_rate",
    "spo2_percentage": "spo2_percentage",
    "skin_temp_celsius": "skin_temp_celsius",
}


def has_cloud_schema(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = 'cloud_metric'").fetchone() is not None


def metric_table(db: sqlite3.Connection) -> str:
    return "effective_day_metric" if has_cloud_schema(db) else BAND_METRICS


def token_status(db_path: str) -> dict[str, str | None]:
    try:
        return TokenStore(token_path(db_path)).status()
    except (WhoopError, OSError):
        return {"token_state": "unreadable", "token_expires_at": None}


def minutes(row: sqlite3.Row, columns: dict[str, str]) -> dict[str, int]:
    return {key: round(row[column] / 60000) for key, column in columns.items() if row[column] is not None}


def sleep_record(row: sqlite3.Row) -> dict:
    scored = row["score_state"] == "SCORED"
    return {
        "source": "whoop_cloud",
        "start": row["start"],
        "end": row["end"],
        "nap": bool(row["nap"]),
        "score_state": row["score_state"],
        "totals": minutes(row, SLEEP_TOTALS) if scored else {},
        "sleep_need_min": minutes(row, SLEEP_NEED) if scored else {},
        **({key: row[key] for key in SLEEP_SCORE_FIELDS if row[key] is not None} if scored else {}),
    }
