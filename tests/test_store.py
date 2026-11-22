import os
import sqlite3
import stat
from contextlib import closing

import pytest

from strap_hub.store import SCHEMA_VERSION, connect


def test_store_creates_schema_with_private_mode(tmp_path):
    db = tmp_path / "hub.db"
    conn = connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"day_metric", "sleep_segment", "workout", "workout_tombstone", "digest_log", "meta"} <= tables
    assert {"cloud_cycle", "cloud_recovery", "cloud_sleep", "cloud_workout"} <= tables
    assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_read_only_connection_cannot_write(tmp_path):
    db = tmp_path / "hub.db"
    connect(db).close()
    ro = connect(db, read_only=True)
    try:
        ro.execute("INSERT INTO meta VALUES ('x','y')")
        raise AssertionError("write should fail")
    except Exception as exc:  # sqlite3.OperationalError
        assert "readonly" in str(exc).lower()


def test_writable_open_secures_existing_database_and_live_wal(tmp_path):
    path = tmp_path / "hub.db"
    paths = [path, tmp_path / "hub.db-wal", tmp_path / "hub.db-shm"]
    with closing(connect(path)) as existing:
        with existing:
            existing.execute("INSERT INTO meta VALUES ('fixture', 'present')")
        for file in paths:
            file.chmod(0o644)
        with closing(connect(path)) as reopened:
            assert reopened.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert reopened.execute("SELECT value FROM meta WHERE key = 'fixture'").fetchone()[0] == "present"
            assert [stat.S_IMODE(file.stat().st_mode) for file in paths] == [0o600, 0o600, 0o600]


@pytest.mark.parametrize("filename", ["hub.db", "hub.db-wal", "hub.db-shm"])
def test_writable_open_refuses_store_when_chmod_fails(tmp_path, monkeypatch, filename):
    path = tmp_path / "hub.db"
    chmod = os.chmod

    def deny(file, mode):
        if file.name == filename:
            raise PermissionError("fixture cannot secure file")
        chmod(file, mode)

    with closing(connect(path)):
        monkeypatch.setattr("strap_hub.store.db.os.chmod", deny)
        with pytest.raises(PermissionError, match="cannot secure file"):
            connect(path)


def test_writable_open_leaves_non_file_mode_untouched(tmp_path):
    path = tmp_path / "hub.db"
    path.mkdir(mode=0o700)
    with pytest.raises(sqlite3.OperationalError, match="regular file"):
        connect(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_migrates_v1_rows_and_preserves_metadata(tmp_path):
    path = tmp_path / "hub.db"
    with closing(sqlite3.connect(path)) as legacy:
        legacy.executescript("""
            CREATE TABLE day_metric (date TEXT NOT NULL, key TEXT NOT NULL, value REAL NOT NULL,
              algo_version INTEGER NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY (date, key));
            CREATE TABLE sleep_segment (date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
              stage TEXT NOT NULL, algo_version INTEGER NOT NULL, PRIMARY KEY (date, start));
            CREATE TABLE workout (id TEXT PRIMARY KEY, date TEXT NOT NULL, start TEXT NOT NULL,
              end TEXT, type TEXT NOT NULL, duration_min REAL, strain REAL, calories REAL,
              max_hr REAL, hrr_bpm REAL, steps INTEGER, zone_min_json TEXT,
              algo_version INTEGER NOT NULL, received_at TEXT NOT NULL);
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '1'), ('empty_night:2026-01-03', '125');
            INSERT INTO day_metric VALUES ('2026-01-02', 'rhr', 53, 125, '2026-01-02T08:00:00Z');
            INSERT INTO sleep_segment VALUES
              ('2026-01-02', '2026-01-02T01:00:00Z', '2026-01-02T02:00:00Z', 'rem', 125);
            INSERT INTO workout (id, date, start, type, algo_version, received_at) VALUES
              ('synthetic', '2026-01-02', '2026-01-02T03:00:00Z', 'running', 125, '2026-01-02T08:00:00Z');
        """)
    inode = path.stat().st_ino

    for _ in range(2):
        with closing(connect(path)) as db:
            assert dict(db.execute("SELECT key, value FROM meta")) == {
                "schema_version": "3", "empty_night:2026-01-03": "125",
            }
            assert tuple(db.execute("SELECT value, source FROM day_metric").fetchone()) == (53, "push")
            assert tuple(db.execute("SELECT stage, source FROM sleep_segment").fetchone()) == ("rem", "push")
            assert tuple(db.execute("SELECT id, source FROM workout").fetchone()) == ("synthetic", "push")
            assert db.execute("SELECT id FROM workout_tombstone").fetchall() == []
    assert path.stat().st_ino == inode


def test_v2_database_migrates_to_v3_without_rewriting_band_rows(tmp_path):
    from strap_hub.store.db import SCHEMA

    path = tmp_path / "hub.db"
    with closing(sqlite3.connect(path)) as legacy, legacy:
        legacy.executescript(SCHEMA)
        legacy.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '2'), ('max_push_seq', '42');
            INSERT INTO workout_tombstone VALUES ('synthetic', '2026-01-01T00:00:00Z', 'push', 42);
        """)
        legacy.execute("INSERT INTO day_metric VALUES ('2026-01-01', 'rhr', 52, 125, "
                       "'2026-01-01T00:00:00Z', 'import')")
    inode = path.stat().st_ino
    path.chmod(0o644)
    with closing(connect(path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert dict(db.execute("SELECT key, value FROM meta")) == {"schema_version": "3", "max_push_seq": "42"}
        assert tuple(db.execute("SELECT source, seq_floor FROM workout_tombstone").fetchone()) == ("push", 42)
        assert db.execute("SELECT * FROM cloud_cycle").fetchall() == []
        assert tuple(db.execute("SELECT value, algo_version, source FROM day_metric").fetchone()) == (52, 125, "import")
    assert path.stat().st_ino == inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
