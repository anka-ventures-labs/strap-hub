import gzip
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from functools import partial
from tempfile import TemporaryDirectory

import pytest

from strap_hub.cli import main
from strap_hub.importer import import_backup
from strap_hub.store import connect

START = int(datetime(2026, 1, 2, 1, tzinfo=UTC).timestamp())


def build_backup(path, *, day_result=True, privacy=True):
    with closing(sqlite3.connect(path)) as source, source:
        source.execute("CREATE TABLE metric_series (date TEXT, key TEXT, value REAL)")
        source.execute("INSERT INTO metric_series VALUES ('2026-01-02', 'rhr', 57)")
        if privacy:
            source.execute(
                "CREATE TABLE sessions (id TEXT, start_ts INTEGER, type TEXT, private INTEGER, "
                "end_ts INTEGER, steps INTEGER, zone_min_json TEXT)"
            )
            source.executemany(
                "INSERT INTO sessions VALUES (?, ?, 'cycling', ?, ?, 102, ?)",
                [
                    ("synthetic-session", START, 0, START + 120, '{"z2": 1.5}'),
                    ("synthetic-private", START + 600, 1, START + 660, "{}"),
                ],
            )
        else:
            source.execute("CREATE TABLE sessions (id TEXT, start_ts INTEGER, type TEXT)")
            source.execute("INSERT INTO sessions VALUES ('synthetic-session', ?, 'cycling')", (START,))
        if day_result:
            source.executescript(
                """
                CREATE TABLE metric_series_version (date TEXT, algo_version INTEGER);
                INSERT INTO metric_series_version VALUES ('2026-01-02', 63);
                CREATE TABLE day_result (day_id TEXT, algo_version INTEGER, payload_json TEXT);
                """
            )
            source.executemany(
                "INSERT INTO day_result VALUES ('2026-01-02', ?, ?)",
                [
                    (
                        version,
                        json.dumps({
                            "series": {
                                "hypnogram": [{"start": START, "end": START + 120, "stage": stage}]
                            }
                        }),
                    )
                    for version, stage in [(64, "rem"), (62, "light")]
                ],
            )


def test_import_gzip_via_cli_and_reimport_without_duplicate_data(tmp_path, monkeypatch, capsys):
    raw = tmp_path / "edge.db"
    backup = tmp_path / "edge.db.gz"
    db_path = tmp_path / "hub.db"
    build_backup(raw)
    with closing(sqlite3.connect(raw)) as source:
        expected_date = source.execute("SELECT date(?, 'unixepoch', 'localtime')", (START,)).fetchone()[0]
    backup.write_bytes(gzip.compress(raw.read_bytes()))
    raw.unlink()
    backup_bytes = backup.read_bytes()
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))

    main(["import", str(backup)])
    assert "Accepted 1 days, 1 nights, and 1 workouts." in capsys.readouterr().out
    assert backup.read_bytes() == backup_bytes
    assert not raw.exists()

    with closing(connect(db_path)) as db:
        metrics = db.execute("SELECT * FROM day_metric").fetchall()
        nights = db.execute("SELECT * FROM sleep_segment").fetchall()
        workouts = db.execute("SELECT * FROM workout").fetchall()
        assert [(row["key"], row["value"], row["algo_version"]) for row in metrics] == [("rhr", 57, 63)]
        assert [tuple(row) for row in nights] == [
            ("2026-01-02", "2026-01-02T01:00:00Z", "2026-01-02T01:02:00Z", "rem", 64, "import")
        ]
        assert len(workouts) == 1
        workout = workouts[0]
        assert (workout["id"], workout["start"], workout["end"], workout["date"], workout["algo_version"]) == (
            "synthetic-session", "2026-01-02T01:00:00Z", "2026-01-02T01:02:00Z", expected_date, 0
        )
        assert workout["steps"] == 102
        assert workout["max_hr"] is None
        assert json.loads(workout["zone_min_json"]) == {"z2": 1.5}
        log = db.execute("SELECT * FROM digest_log").fetchone()
        assert (log["source"], log["days"], log["nights"], log["workouts"]) == ("import", 1, 1, 1)

        main(["import", str(backup)])
        assert db.execute("SELECT * FROM day_metric").fetchall() == metrics
        assert db.execute("SELECT * FROM sleep_segment").fetchall() == nights
        assert db.execute("SELECT * FROM workout").fetchall() == workouts


def test_missing_day_result_keeps_other_rows(tmp_path, capsys):
    backup = tmp_path / "edge?.db"
    db_path = tmp_path / "hub.db"
    build_backup(backup, day_result=False)
    original = backup.read_bytes()

    assert import_backup(backup, db_path) == {"days": 1, "nights": 0, "workouts": 1}
    output = capsys.readouterr().out
    assert "Skipped day_result: table is missing" in output
    assert "Skipped sessions columns" in output
    assert backup.read_bytes() == original

    with closing(connect(db_path)) as db:
        assert tuple(db.execute("SELECT value, algo_version FROM day_metric").fetchone()) == (57, 0)
        assert db.execute("SELECT count(*) FROM sleep_segment").fetchone()[0] == 0
        assert db.execute("SELECT id FROM workout").fetchone()[0] == "synthetic-session"


def test_missing_privacy_column_skips_sessions_only(tmp_path, capsys):
    backup = tmp_path / "edge.db"
    db_path = tmp_path / "hub.db"
    build_backup(backup, privacy=False)

    assert import_backup(backup, db_path) == {"days": 1, "nights": 1, "workouts": 0}
    assert "Skipped sessions: missing columns private" in capsys.readouterr().out


def test_import_keeps_workouts_with_edge_zone_arrays_or_invalid_zones(tmp_path, capsys):
    backup = tmp_path / "edge.db"
    db_path = tmp_path / "hub.db"
    build_backup(backup, day_result=False)
    with closing(sqlite3.connect(backup)) as source, source:
        source.executemany(
            "INSERT INTO sessions (id, start_ts, type, private, zone_min_json) VALUES (?, ?, 'rowing', 0, ?)",
            [
                ("array", START, "[1, 2.5, 3, 4, 0]"),
                ("empty", START, "[]"),
                ("broken-json", START, "["),
                ("bad-minutes", START, '[1, 2, "bad", 4, 5]'),
                ("bad-length", START, "[1, 2]"),
            ],
        )
    assert import_backup(backup, db_path)["workouts"] == 6
    assert "Skipped invalid zone minutes for 3 sessions." in capsys.readouterr().out
    with closing(connect(db_path)) as db:
        zones = dict(db.execute("SELECT id, zone_min_json FROM workout"))
        assert json.loads(zones.pop("array")) == {"z1": 1, "z2": 2.5, "z3": 3, "z4": 4, "z5": 0}
        assert json.loads(zones.pop("synthetic-session")) == {"z2": 1.5}
        assert zones == {"empty": None, "broken-json": None, "bad-minutes": None, "bad-length": None}


def test_import_private_session_deletes_existing_workout_and_blocks_old_backup(tmp_path):
    public = tmp_path / "public.db"
    private = tmp_path / "private.db"
    db_path = tmp_path / "hub.db"
    build_backup(public, day_result=False)
    with closing(sqlite3.connect(private)) as source, source:
        source.execute("CREATE TABLE sessions (id TEXT, private INTEGER)")
        source.execute("INSERT INTO sessions VALUES ('synthetic-session', 1)")

    import_backup(public, db_path)
    with closing(connect(db_path)) as db:
        assert db.execute("SELECT id FROM workout").fetchone()[0] == "synthetic-session"
        import_backup(private, db_path)
        import_backup(public, db_path)
        assert db.execute("SELECT id FROM workout").fetchall() == []
        assert tuple(db.execute(
            "SELECT id, source FROM workout_tombstone WHERE id = 'synthetic-session'"
        ).fetchone()) == ("synthetic-session", "import")


@pytest.mark.parametrize(("recorded", "override", "expected"), [(None, None, 64), (63, None, 62), (63, 64, 64)])
def test_import_selects_served_night_with_seconds_and_milliseconds(tmp_path, monkeypatch, capsys,
                                                             recorded, override, expected):
    backup = tmp_path / "edge.db"
    db_path = tmp_path / "hub.db"
    build_backup(backup)
    with closing(sqlite3.connect(backup)) as source, source:
        for version, stage in [(64, "rem"), (62, "light")]:
            hypnogram = [
                {"start": START, "end": START + 60, "stage": stage},
                {"start": (START + 60) * 1_000, "end": (START + 120) * 1_000 + 125, "stage": "deep"},
            ]
            source.execute(
                "UPDATE day_result SET payload_json = ? WHERE algo_version = ?",
                (json.dumps({"series": {"hypnogram": hypnogram}}), version),
            )
        source.execute("INSERT INTO day_result VALUES ('2026-01-03', 100, '{}')")
        if recorded is not None:
            source.execute(f"""CREATE VIEW v_hypnogram AS
                SELECT day_id, MAX(algo_version) FROM day_result
                WHERE algo_version <= {recorded} GROUP BY day_id""")
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    args = ["import", str(backup)]
    if override is not None:
        args.extend(["--algo-ceiling", str(override)])
    main(args)
    report = capsys.readouterr().out
    assert ("Warning: no algorithm ceiling recorded" in report) == (recorded is None and override is None)
    assert ("invalid hypnograms" in report) == (recorded is None and override is None)

    with closing(connect(db_path)) as db:
        assert [tuple(row) for row in db.execute(
            "SELECT date, start, end, stage, algo_version FROM sleep_segment ORDER BY start"
        )] == [
            ("2026-01-02", "2026-01-02T01:00:00Z", "2026-01-02T01:01:00Z",
             "light" if expected == 62 else "rem", expected),
            ("2026-01-02", "2026-01-02T01:01:00Z", "2026-01-02T01:02:00.125000Z", "deep", expected),
        ]


def test_gzip_expansion_limit_cleans_partial_file(tmp_path, monkeypatch, capsys):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr("strap_hub.importer.TemporaryDirectory", partial(TemporaryDirectory, dir=staging))
    backup = tmp_path / "edge.db.gz"
    db_path = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    limit = 1024**2
    backup.write_bytes(gzip.compress(b"\0" * (limit + 1)))

    with pytest.raises(SystemExit) as error:
        main(["import", str(backup), "--max-bytes", str(limit)])
    assert error.value.code == 1
    assert "expansion limit" in capsys.readouterr().err
    assert list(staging.iterdir()) == []
    assert not db_path.exists()

    raw = tmp_path / "edge.db"
    build_backup(raw)
    data = raw.read_bytes()
    backup.write_bytes(gzip.compress(data))
    main(["import", str(backup), "--max-bytes", str(len(data))])
    assert "Accepted 1 days, 1 nights, and 1 workouts." in capsys.readouterr().out
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(("configured", "expected"), [(None, ("127.0.0.1", 8770)), ("[::1]:9870", ("::1", 9870))])
def test_serve_uses_configured_bind(configured, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("HUB_DB_PATH", str(tmp_path / "hub.db"))
    if configured is None:
        monkeypatch.delenv("HUB_BIND", raising=False)
    else:
        monkeypatch.setenv("HUB_BIND", configured)

    binds = []
    masks = []
    monkeypatch.setattr("strap_hub.cli.os.umask", masks.append)
    monkeypatch.setattr("strap_hub.cli.uvicorn.run", lambda app, **kwargs: binds.append(kwargs))
    main(["serve"])
    assert [(bind["host"], bind["port"]) for bind in binds] == [expected]
    assert masks == [0o077]
