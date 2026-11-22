"""WHOOP records and their local-calendar projections for read-only queries."""

CLOUD_SCHEMA = """
CREATE TABLE IF NOT EXISTS cloud_cycle (
  id INTEGER PRIMARY KEY, date TEXT NOT NULL, start TEXT NOT NULL, end TEXT,
  timezone_offset TEXT NOT NULL, score_state TEXT NOT NULL, step_count INTEGER,
  strain REAL, kilojoule REAL, average_heart_rate INTEGER, max_heart_rate INTEGER,
  raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_recovery (
  cycle_id INTEGER PRIMARY KEY, sleep_id TEXT NOT NULL, sleep_start TEXT, score_state TEXT NOT NULL,
  recovery_score REAL, resting_heart_rate REAL, hrv_rmssd_milli REAL,
  spo2_percentage REAL, skin_temp_celsius REAL, user_calibrating INTEGER,
  raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_sleep (
  id TEXT PRIMARY KEY NOT NULL, date TEXT, start TEXT NOT NULL, end TEXT,
  timezone_offset TEXT NOT NULL, nap INTEGER NOT NULL, score_state TEXT NOT NULL,
  total_in_bed_time_milli INTEGER, total_awake_time_milli INTEGER,
  total_no_data_time_milli INTEGER, total_light_sleep_time_milli INTEGER,
  total_slow_wave_sleep_time_milli INTEGER, total_rem_sleep_time_milli INTEGER,
  sleep_cycle_count INTEGER, disturbance_count INTEGER,
  baseline_milli INTEGER, need_from_sleep_debt_milli INTEGER,
  need_from_recent_strain_milli INTEGER, need_from_recent_nap_milli INTEGER,
  respiratory_rate REAL, sleep_performance_percentage REAL,
  sleep_consistency_percentage REAL, sleep_efficiency_percentage REAL,
  raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cloud_workout (
  id TEXT PRIMARY KEY NOT NULL, date TEXT NOT NULL, start TEXT NOT NULL, end TEXT,
  timezone_offset TEXT NOT NULL, sport_name TEXT NOT NULL, sport_id INTEGER,
  score_state TEXT NOT NULL, strain REAL, average_heart_rate INTEGER, max_heart_rate INTEGER,
  kilojoule REAL, percent_recorded REAL, distance_meter REAL,
  altitude_gain_meter REAL, altitude_change_meter REAL,
  zone_zero_milli INTEGER, zone_one_milli INTEGER, zone_two_milli INTEGER,
  zone_three_milli INTEGER, zone_four_milli INTEGER, zone_five_milli INTEGER,
  raw_json TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_cloud_cycle_date ON cloud_cycle(date);
CREATE INDEX IF NOT EXISTS idx_cloud_sleep_date ON cloud_sleep(date, nap);
CREATE INDEX IF NOT EXISTS idx_cloud_workout_date ON cloud_workout(date);

CREATE VIEW IF NOT EXISTS cloud_cycle_day AS
  SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY date ORDER BY start DESC, id DESC) AS position
    FROM cloud_cycle
  ) WHERE position = 1;

CREATE VIEW IF NOT EXISTS cloud_sleep_night AS
  SELECT * FROM (
    SELECT *, total_light_sleep_time_milli + total_slow_wave_sleep_time_milli
      + total_rem_sleep_time_milli AS total_sleep_milli,
      ROW_NUMBER() OVER (PARTITION BY date ORDER BY end DESC, id DESC) AS position
    FROM cloud_sleep WHERE nap = 0 AND date IS NOT NULL
  ) WHERE position = 1;

CREATE VIEW IF NOT EXISTS cloud_recovery_day AS
  SELECT * FROM (
    SELECT r.*, COALESCE(s.date, c.date) AS date,
      ROW_NUMBER() OVER (PARTITION BY COALESCE(s.date, c.date)
        ORDER BY COALESCE(s.end, c.start) DESC, r.cycle_id DESC) AS position
    FROM cloud_recovery r
    LEFT JOIN cloud_sleep s ON s.id = r.sleep_id
    LEFT JOIN cloud_cycle c ON c.id = r.cycle_id
    WHERE COALESCE(s.date, c.date) IS NOT NULL
  ) WHERE position = 1;

CREATE VIEW IF NOT EXISTS cloud_metric AS
  SELECT date, metric.key, metric.value
  FROM cloud_cycle_day, json_each(json_object(
    'strain', strain, 'steps', step_count, 'kilojoule', kilojoule,
    'calories_total', kilojoule / 4.184, 'avg_hr', average_heart_rate, 'max_hr', max_heart_rate
  )) AS metric WHERE metric.value IS NOT NULL
  UNION ALL
  SELECT date, metric.key, metric.value
  FROM cloud_recovery_day, json_each(json_object(
    'readiness', recovery_score, 'rhr', resting_heart_rate, 'rmssd', hrv_rmssd_milli,
    'spo2_percentage', spo2_percentage, 'skin_temp_celsius', skin_temp_celsius
  )) AS metric WHERE metric.value IS NOT NULL
  UNION ALL
  SELECT date, metric.key, metric.value
  FROM cloud_sleep_night, json_each(json_object(
    'tst_min', total_sleep_milli / 60000.0, 'deep_min', total_slow_wave_sleep_time_milli / 60000.0,
    'rem_min', total_rem_sleep_time_milli / 60000.0, 'light_min', total_light_sleep_time_milli / 60000.0,
    'efficiency', sleep_efficiency_percentage, 'resp_rate', respiratory_rate,
    'sleep_performance_percentage', sleep_performance_percentage,
    'sleep_consistency_percentage', sleep_consistency_percentage
  )) AS metric WHERE metric.value IS NOT NULL
  UNION ALL
  SELECT date, 'nap_min', SUM(total_light_sleep_time_milli + total_slow_wave_sleep_time_milli
    + total_rem_sleep_time_milli) / 60000.0 AS value
  FROM cloud_sleep WHERE nap = 1 AND date IS NOT NULL GROUP BY date HAVING value IS NOT NULL;

CREATE VIEW IF NOT EXISTS effective_day_metric AS
  SELECT date, key, value, 'whoop_cloud' AS source FROM cloud_metric
  UNION ALL
  SELECT date, key, value, 'band' AS source FROM day_metric b WHERE NOT EXISTS (
    SELECT 1 FROM cloud_metric c WHERE c.date = b.date AND c.key = b.key
  );

"""

# Also used directly by read-only MCP connections so existing v3 stores get
# session matching without requiring a writable schema upgrade.
EFFECTIVE_WORKOUTS = """
  WITH RECURSIVE band_times AS (
    SELECT id, julianday(start) AS starts,
      COALESCE(julianday(end), julianday(start) + duration_min / 1440.0) AS ends
    FROM workout
  ), cloud_times AS (
    SELECT id, julianday(start) AS starts, julianday(end) AS ends
    FROM cloud_workout
  ), pair_times AS (
    SELECT c.id AS cloud_id, b.id AS band_id,
      ROUND(ABS(b.starts - c.starts) * 86400, 3) AS start_delta,
      MIN(b.ends, c.ends) - MAX(b.starts, c.starts) AS overlap,
      MAX(b.ends - b.starts, c.ends - c.starts) AS longest_duration
    FROM cloud_times c CROSS JOIN band_times b
  ), candidates AS (
    SELECT cloud_id, band_id, ROW_NUMBER() OVER (
      ORDER BY start_delta, overlap DESC, cloud_id, band_id
    ) AS position
    FROM pair_times
    WHERE (start_delta <= 120 AND (overlap > 0 OR overlap IS NULL))
      OR (overlap > 0 AND overlap >= 0.5 * longest_duration)
  ), assignments(position, pairs) AS (
    SELECT 0, '{}'
    UNION ALL
    SELECT c.position, CASE WHEN EXISTS (
      SELECT 1 FROM json_each(a.pairs) WHERE key = c.cloud_id OR value = c.band_id
    ) THEN a.pairs ELSE json_patch(a.pairs, json_object(c.cloud_id, c.band_id)) END
    FROM assignments a JOIN candidates c ON c.position = a.position + 1
  ), matches AS (
    SELECT key AS cloud_id, value AS band_id
    FROM assignments, json_each(assignments.pairs)
    WHERE position = (SELECT COUNT(*) FROM candidates)
  ), cloud_values AS (
    SELECT date, id, start, sport_name AS type,
      ROUND((julianday(end) - julianday(start)) * 1440, 4) AS duration_min,
      score_state,
      CASE WHEN score_state = 'SCORED' THEN strain END AS strain,
      CASE WHEN score_state = 'SCORED' THEN kilojoule END AS kilojoule,
      CASE WHEN score_state = 'SCORED' THEN max_heart_rate END AS max_hr,
      CASE WHEN score_state = 'SCORED' THEN average_heart_rate END AS avg_hr,
      CASE WHEN score_state = 'SCORED' THEN percent_recorded END AS percent_recorded,
      CASE WHEN score_state = 'SCORED' THEN distance_meter END AS distance_meter,
      CASE WHEN score_state = 'SCORED' THEN altitude_gain_meter END AS altitude_gain_meter,
      CASE WHEN score_state = 'SCORED' THEN altitude_change_meter END AS altitude_change_meter,
      (SELECT json_group_object(key, value / 60000.0) FROM json_each(json_object(
        'z0', zone_zero_milli, 'z1', zone_one_milli, 'z2', zone_two_milli, 'z3', zone_three_milli,
        'z4', zone_four_milli, 'z5', zone_five_milli
      )) WHERE value IS NOT NULL AND score_state = 'SCORED') AS zones
    FROM cloud_workout
  )
  SELECT c.date, 'whoop:' || c.id AS id, c.start, c.type,
    COALESCE(c.duration_min, b.duration_min) AS duration_min,
    COALESCE(c.strain, b.strain) AS strain,
    COALESCE(c.kilojoule / 4.184, b.calories) AS calories,
    COALESCE(c.max_hr, b.max_hr) AS max_hr,
    NULLIF(json_patch(COALESCE(b.zone_min_json, '{}'), c.zones), '{}') AS zone_min_json,
    'whoop_cloud' AS source, c.score_state, c.kilojoule, c.avg_hr,
    c.percent_recorded, c.distance_meter, c.altitude_gain_meter, c.altitude_change_meter,
    (SELECT json_group_array(key) FROM (
      SELECT key FROM json_each(json_object(
        'duration_min', c.duration_min IS NULL AND b.duration_min IS NOT NULL,
        'strain', c.strain IS NULL AND b.strain IS NOT NULL,
        'calories', c.kilojoule IS NULL AND b.calories IS NOT NULL,
        'max_hr', c.max_hr IS NULL AND b.max_hr IS NOT NULL
      )) WHERE value
      UNION ALL
      SELECT 'zone_min.' || band_zone.key FROM json_each(b.zone_min_json) band_zone
      WHERE NOT EXISTS (SELECT 1 FROM json_each(c.zones) WHERE key = band_zone.key)
      ORDER BY key
    )) AS band_keys_json
  FROM cloud_values c
  LEFT JOIN matches m ON m.cloud_id = c.id
  LEFT JOIN workout b ON b.id = m.band_id
  UNION ALL
  SELECT date, id, start, type, duration_min, strain, calories, max_hr, zone_min_json,
    'band' AS source, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL
  FROM workout b WHERE NOT EXISTS (SELECT 1 FROM matches WHERE band_id = b.id)
"""

WORKOUT_VIEW = f"CREATE VIEW IF NOT EXISTS effective_workout AS {EFFECTIVE_WORKOUTS}"
CLOUD_SCHEMA += f"{WORKOUT_VIEW};"
