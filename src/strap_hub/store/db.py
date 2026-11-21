"""Open and initialise the hub store (docs/CONTRACT.md section 4).

Owned by the coordinator. W1 writes through it, W2 reads through it.
"""
from __future__ import annotations

import os
import sqlite3
from stat import S_ISREG
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_metric (
  date TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL,
  algo_version INTEGER NOT NULL, received_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'push',
  PRIMARY KEY (date, key));
CREATE TABLE IF NOT EXISTS sleep_segment (
  date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  stage TEXT NOT NULL, algo_version INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'push',
  PRIMARY KEY (date, start));
CREATE TABLE IF NOT EXISTS workout (
  id TEXT PRIMARY KEY, date TEXT NOT NULL, start TEXT NOT NULL, end TEXT,
  type TEXT NOT NULL, duration_min REAL, strain REAL, calories REAL,
  max_hr REAL, hrr_bpm REAL, steps INTEGER, zone_min_json TEXT,
  algo_version INTEGER NOT NULL, received_at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'push');
CREATE INDEX IF NOT EXISTS idx_workout_date ON workout(date);
CREATE TABLE IF NOT EXISTS workout_tombstone (
  id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL, source TEXT NOT NULL,
  seq_floor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS digest_log (
  id INTEGER PRIMARY KEY, received_at TEXT NOT NULL, sent_at TEXT,
  app_version TEXT, algo_version INTEGER, last_band_record_at TEXT,
  days INTEGER, nights INTEGER, workouts INTEGER, source TEXT NOT NULL);
"""


def _secure_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = db_path.with_name(db_path.name + suffix)
        try:
            if not S_ISREG(sidecar.lstat().st_mode):
                raise sqlite3.OperationalError("Hub store sidecar must be a regular file")
            os.chmod(sidecar, 0o600)
        except FileNotFoundError:
            pass  # SQLite creates missing sidecars from the now-private DB mode.


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is not None:
        if row[0] == str(SCHEMA_VERSION):
            return
        if row[0] != "1":
            raise sqlite3.DatabaseError("Unsupported hub store schema version")
        for table in ("day_metric", "sleep_segment", "workout"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL DEFAULT 'push'")

    for statement in SCHEMA.strip().rstrip(";").split(";"):
        conn.execute(statement)
    conn.execute(
        """INSERT INTO meta(key, value) VALUES ('schema_version', ?)
           ON CONFLICT (key) DO UPDATE SET value = excluded.value""",
        (str(SCHEMA_VERSION),),
    )


def connect(path: str | os.PathLike[str] | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Return a connection to the store, securing and migrating it on writable opens.

    `path` defaults to $HUB_DB_PATH. Read-only connections never create the file.
    """
    db_path = Path(path or os.environ["HUB_DB_PATH"]).expanduser().resolve()
    if read_only:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        db_path.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        pass
    if not db_path.is_file():
        raise sqlite3.OperationalError("Hub store path must be a regular file")
    os.chmod(db_path, 0o600)
    _secure_sidecars(db_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise sqlite3.OperationalError("Could not enable WAL on the hub store")
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            _secure_sidecars(db_path)
            _initialize_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn
