import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest
from fastapi.testclient import TestClient

from strap_hub.importer import import_backup
from strap_hub.ingest.api import MAX_BODY_BYTES, create_app
from strap_hub.mcp import server
from strap_hub.store import connect

TOKEN = "synthetic-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def hub(tmp_path):
    db_path = tmp_path / "hub.db"
    with TestClient(create_app(token=TOKEN, db_path=db_path)) as client:
        yield client, db_path


def sample_digest(version=70):
    return {
        "schema": 1,
        "sent_at": "2026-01-02T08:10:00Z",
        "source": {
            "app": "strap-edge",
            "app_version": "0.0.1",
            "algo_version": version,
            "device_family": "whoop5",
        },
        "freshness": {"last_band_record_at": "2026-01-02T08:00:00Z"},
        "days": [{"date": "2026-01-02", "metrics": {"rhr": 53, "future_metric": 0.5}}],
        "nights": [
            {
                "date": "2026-01-02",
                "segments": [
                    {"start": "2026-01-01T23:00:00Z", "end": "2026-01-02T00:00:00Z", "stage": "light"},
                    {"start": "2026-01-02T00:00:00Z", "end": "2026-01-02T01:00:00Z", "stage": "deep"},
                ],
            }
        ],
        "workouts": [
            {
                "id": "synthetic-workout",
                "date": "2026-01-01",
                "start": "2026-01-02T01:00:00Z",
                "end": "2026-01-02T02:00:00Z",
                "type": "cycling",
                "duration_min": 60,
                "strain": 9.5,
                "calories": 210,
                "max_hr": 160,
                "hrr_bpm": 26,
                "steps": 101,
                "zone_min": {"z1": 5, "z2": 25.5},
            }
        ],
    }


def sample_backup(path, *, version=125, rhr=65, stage="rem", private=0):
    with closing(sqlite3.connect(path)) as backup, backup:
        backup.executescript("""
            CREATE TABLE metric_series (date TEXT, key TEXT, value REAL);
            CREATE TABLE metric_series_version (date TEXT, algo_version INTEGER);
            CREATE TABLE day_result (day_id TEXT, algo_version INTEGER, payload_json TEXT);
            CREATE TABLE sessions (id TEXT, start_ts INTEGER, type TEXT, private INTEGER);
            INSERT INTO metric_series VALUES ('2026-01-02', 'rmssd', 35);
        """)
        backup.execute("INSERT INTO metric_series VALUES ('2026-01-02', 'rhr', ?)", (rhr,))
        backup.execute("INSERT INTO metric_series_version VALUES ('2026-01-02', ?)", (version,))
        hypnogram = [{"start": 1767315600, "end": 1767319200, "stage": stage}]
        backup.execute(
            "INSERT INTO day_result VALUES ('2026-01-02', ?, ?)",
            (version, json.dumps({"series": {"hypnogram": hypnogram}})),
        )
        backup.execute(
            "INSERT INTO sessions VALUES ('synthetic-workout', 1767315600, 'running', ?)", (private,)
        )


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic synthetic-test-token"])
def test_authentication_precedes_body(hub, authorization):
    client, db_path = hub
    headers = {"Authorization": authorization} if authorization else {}

    def ignored_body():
        raise AssertionError("the body must be ignored without valid auth")
        yield b""

    assert client.get("/v1/health", headers=headers).status_code == 401
    response = client.post("/v1/digest", headers=headers, content=ignored_body())
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert not db_path.exists()


def test_body_limit_applies_to_streams_and_allows_boundary(hub):
    client, _ = hub
    headers = {**AUTH, "Content-Type": "application/json"}
    body = json.dumps(sample_digest()).encode()
    padding = b" " * (MAX_BODY_BYTES - len(body))
    assert client.post("/v1/digest", headers=headers, content=body + padding).status_code == 200
    for oversized in (body + padding + b" ", iter((body, padding, b" "))):
        assert client.post("/v1/digest", headers=headers, content=oversized).status_code == 413

    def ignored_body():
        raise AssertionError("the declared body size already exceeds the limit")
        yield b""

    response = client.post(
        "/v1/digest",
        headers={**headers, "Content-Length": str(MAX_BODY_BYTES + 1)},
        content=ignored_body(),
    )
    assert response.status_code == 413


@pytest.mark.parametrize("body", [b"{", b'{"bad":"\xff"}', b"[]", b'{"schema":2}'])
def test_invalid_json_or_schema_returns_short_422(hub, body):
    client, db_path = hub
    response = client.post(
        "/v1/digest", headers={**AUTH, "Content-Type": "application/json"}, content=body
    )
    assert response.status_code == 422
    assert len(response.json()["detail"]) < 100
    assert not db_path.exists()


@pytest.mark.parametrize(
    ("part", "field", "value"),
    [
        ("days", "date", "2026-02-30"),
        ("days", "metrics", {"rhr": True}),
        ("days", "metrics", {"rhr": "53"}),
        ("days", "metrics", {"rhr": {"nested": 53}}),
        ("nights", "segments", [{"start": "2026-01-02T00:00:00Z", "end": "2026-01-02T01:00:00Z", "stage": "invalid"}]),
        ("workouts", "date", None),
        ("workouts", "start", "2026-01-02T01:00:00+01:00"),
    ],
)
def test_schema_rejects_invalid_fields(hub, part, field, value):
    client, _ = hub
    payload = sample_digest()
    payload[part][0][field] = value
    assert client.post("/v1/digest", headers=AUTH, json=payload).status_code == 422


def test_invalid_unicode_in_metric_key_returns_422(hub):
    client, db_path = hub
    payload = sample_digest()
    payload["days"][0]["metrics"] = {"\ud800": 53}
    response = client.post(
        "/v1/digest",
        headers={**AUTH, "Content-Type": "application/json"},
        content=json.dumps(payload),
    )
    assert response.status_code == 422
    assert not db_path.exists()


def test_full_digest_and_identical_retry(hub):
    client, db_path = hub
    assert client.get("/v1/health", headers=AUTH).json() == {"ok": True, "last_digest_at": None}
    payload = sample_digest()
    with closing(connect(db_path)) as db:
        first = client.post("/v1/digest", headers=AUTH, json=payload)
        assert first.status_code == 200
        assert first.json() == {
            "accepted": {"days": 1, "nights": 1, "workouts": 1},
            "freshness": "2026-01-02T08:00:00Z",
        }
        metrics = db.execute("SELECT * FROM day_metric ORDER BY key").fetchall()
        sleep = db.execute("SELECT * FROM sleep_segment ORDER BY start").fetchall()
        workout = db.execute("SELECT * FROM workout").fetchone()
        assert [(row["key"], row["value"], row["algo_version"]) for row in metrics] == [
            ("future_metric", 0.5, 70),
            ("rhr", 53, 70),
        ]
        assert [tuple(row) for row in sleep] == [
            ("2026-01-02", "2026-01-01T23:00:00Z", "2026-01-02T00:00:00Z", "light", 70, "push"),
            ("2026-01-02", "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z", "deep", 70, "push"),
        ]
        assert workout["date"] == "2026-01-01"
        for key, value in payload["workouts"][0].items():
            if key != "zone_min":
                assert workout[key] == value
        assert json.loads(workout["zone_min_json"]) == {"z1": 5, "z2": 25.5}
        assert workout["algo_version"] == 70

        health = client.get("/v1/health", headers=AUTH).json()
        payload["days"][0]["metrics"] = {"future_metric": 0.5, "rhr": 53}
        retry = client.post("/v1/digest", headers=AUTH, json=payload)
        assert retry.status_code == 200
        assert retry.json() == first.json()
        assert db.execute("SELECT * FROM day_metric ORDER BY key").fetchall() == metrics
        assert db.execute("SELECT * FROM sleep_segment ORDER BY start").fetchall() == sleep
        assert db.execute("SELECT * FROM workout").fetchall() == [workout]
        logs = db.execute("SELECT * FROM digest_log ORDER BY id").fetchall()
        assert len(logs) == 1
        log = logs[0]
        assert (log["sent_at"], log["app_version"], log["algo_version"], log["last_band_record_at"]) == (
            "2026-01-02T08:10:00Z", "0.0.1", 70, "2026-01-02T08:00:00Z",
        )
        assert (log["source"], log["days"], log["nights"], log["workouts"]) == ("push", 1, 1, 1)
        assert client.get("/v1/health", headers=AUTH).json() == health
        assert health == {"ok": True, "last_digest_at": log["received_at"]}


def test_night_replacement_and_version_rules(hub):
    client, db_path = hub

    def post(payload):
        assert client.post("/v1/digest", headers=AUTH, json=payload).status_code == 200

    previous = sample_digest(70)
    post(previous)
    latest = sample_digest(71)
    latest["days"][0]["metrics"] = {"rhr": 56}
    latest["workouts"][0]["type"] = "running"
    latest["nights"][0]["segments"] = [
        {"start": "2026-01-02T02:00:00Z", "end": "2026-01-02T03:00:00Z", "stage": "rem"},
    ]
    post(latest)
    previous["sent_at"] = "2026-01-02T08:11:00Z"
    post(previous)

    with closing(connect(db_path)) as db:
        assert [tuple(row) for row in db.execute("SELECT key, value, algo_version FROM day_metric ORDER BY key")] == [
            ("future_metric", 0.5, 70), ("rhr", 56, 71),
        ]
        assert tuple(db.execute("SELECT type, algo_version FROM workout").fetchone()) == ("running", 71)
        assert [tuple(row) for row in db.execute("SELECT start, stage, algo_version FROM sleep_segment")] == [
            ("2026-01-02T02:00:00Z", "rem", 71),
        ]

        latest["nights"][0]["segments"] = []
        latest["days"][0]["metrics"]["rhr"] = 57
        latest["workouts"][0]["type"] = "walking"
        post(latest)
        previous["sent_at"] = "2026-01-02T08:12:00Z"
        post(previous)
        assert db.execute("SELECT start FROM sleep_segment").fetchall() == []
        assert db.execute("SELECT value FROM day_metric WHERE key = 'rhr'").fetchone()[0] == 57
        assert db.execute("SELECT type FROM workout").fetchone()[0] == "walking"


@pytest.mark.parametrize("revocation_source", ["push", "import"])
def test_revoke_removes_pushed_workout_and_only_newer_numbered_push_restores(
    hub, tmp_path, revocation_source
):
    client, db_path = hub
    public = sample_digest(125)
    public["seq"] = 1
    workout_id = public["workouts"][0]["id"]
    assert client.post("/v1/digest", headers=AUTH, json=public).status_code == 200

    revoke = sample_digest(124)
    revoke["seq"] = 2
    revoke["sent_at"] = "2026-01-02T08:11:00Z"
    revoke["workouts"] = []
    revoke["revoked_workout_ids"] = [workout_id, "synthetic-unseen"]
    if revocation_source == "push":
        assert client.post("/v1/digest", headers=AUTH, json=revoke).status_code == 200
    else:
        private_backup = tmp_path / "private.db"
        sample_backup(private_backup, private=1)
        import_backup(private_backup, db_path)
    floor = 2 if revocation_source == "push" else 1

    def visible(db):
        return [row[0] for row in db.execute("SELECT id FROM workout")]

    with closing(connect(db_path)) as db:
        # Exact retry of an accepted digest.
        assert client.post("/v1/digest", headers=AUTH, json=public).status_code == 200
        # Old public backup.
        public_backup = tmp_path / "public.db"
        sample_backup(public_backup)
        import_backup(public_backup, db_path)
        # A delayed digest built before the revocation, arriving for the first time.
        delayed = sample_digest(125)
        delayed["seq"] = floor
        delayed["sent_at"] = "2026-01-02T08:10:30Z"
        assert client.post("/v1/digest", headers=AUTH, json=delayed).status_code == 200
        # An unnumbered sender can never republish.
        unnumbered = sample_digest(125)
        unnumbered["sent_at"] = "2026-01-02T08:13:00Z"
        assert client.post("/v1/digest", headers=AUTH, json=unnumbered).status_code == 200
        assert visible(db) == []
        tombstones = dict(db.execute("SELECT id, source FROM workout_tombstone"))
        assert tombstones.pop(workout_id) == revocation_source
        assert tombstones == ({"synthetic-unseen": "push"} if revocation_source == "push" else {})

        # The owner makes it public again: a newer numbered push republishes it.
        republish = sample_digest(125)
        republish["seq"] = 3
        republish["sent_at"] = "2026-01-02T08:14:00Z"
        assert client.post("/v1/digest", headers=AUTH, json=republish).status_code == 200
        assert visible(db) == [workout_id]
        assert dict(db.execute("SELECT id, source FROM workout_tombstone")) == tombstones
        if revocation_source == "push":
            # Replaying the old revocation is a no-op receipt.
            assert client.post("/v1/digest", headers=AUTH, json=revoke).status_code == 200
            assert visible(db) == [workout_id]


@pytest.mark.parametrize("segments", ["populated", "empty"])
def test_equal_and_higher_version_backups_fill_gaps_without_replacing_pushed_data(
    hub, tmp_path, monkeypatch, segments
):
    client, db_path = hub
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    monkeypatch.setattr(server, "_today", lambda: date(2026, 1, 2))
    push = sample_digest(125)
    if segments == "empty":
        push["nights"][0]["segments"] = []
    assert client.post("/v1/digest", headers=AUTH, json=push).status_code == 200

    with closing(connect(db_path, read_only=True)) as db:
        for version in (125, 126):
            backup = tmp_path / f"edge-{version}.db"
            sample_backup(backup, version=version, rhr=65)
            import_backup(backup, db_path)

            assert [tuple(row) for row in db.execute(
                "SELECT key, value, algo_version, source FROM day_metric ORDER BY key"
            )] == [("future_metric", 0.5, 125, "push"), ("rhr", 53, 125, "push"), ("rmssd", 35, version, "import")]
            assert [tuple(row) for row in db.execute(
                "SELECT stage, algo_version, source FROM sleep_segment ORDER BY start"
            )] == ([("light", 125, "push"), ("deep", 125, "push")] if segments == "populated" else [])
            assert tuple(db.execute("SELECT type, source FROM workout").fetchone()) == ("cycling", "push")
            assert json.loads(server.metric_trend("rhr"))["values"] == [{"date": "2026-01-02", "value": 53}]


def test_imports_only_replace_imports_at_higher_version_and_equal_push_claims_rows(hub, tmp_path):
    client, db_path = hub
    with closing(connect(db_path)) as db:
        for version, rhr, stage, expected in [
            (125, 65, "rem", (65, "rem")),
            (125, 60, "light", (65, "rem")),
            (126, 60, "deep", (60, "deep")),
        ]:
            backup = tmp_path / f"edge-{version}-{stage}.db"
            sample_backup(backup, version=version, rhr=rhr, stage=stage)
            import_backup(backup, db_path)
            assert tuple(db.execute("SELECT value, source FROM day_metric WHERE key = 'rhr'").fetchone()) == (
                expected[0], "import",
            )
            assert tuple(db.execute("SELECT stage, source FROM sleep_segment").fetchone()) == (
                expected[1], "import",
            )
        push = sample_digest(126)
        push["days"][0]["metrics"] = {"rhr": 60}  # Same value and version still claims push ownership.
        assert client.post("/v1/digest", headers=AUTH, json=push).status_code == 200
        assert tuple(db.execute("SELECT value, source FROM day_metric WHERE key = 'rhr'").fetchone()) == (60, "push")
        assert [row[0] for row in db.execute("SELECT DISTINCT source FROM sleep_segment")] == ["push"]
        assert tuple(db.execute("SELECT type, source FROM workout").fetchone()) == ("cycling", "push")


def test_push_omits_invalid_zone_data_and_keeps_workout(hub):
    client, db_path = hub
    payload = sample_digest()
    payload["workouts"][0]["zone_min"] = {"z2": "invalid"}
    assert client.post("/v1/digest", headers=AUTH, json=payload).status_code == 200
    with closing(connect(db_path)) as db:
        assert tuple(db.execute("SELECT id, zone_min_json FROM workout").fetchone()) == (
            "synthetic-workout", None,
        )


def test_first_push_without_band_data_is_accepted(hub):
    client, db_path = hub
    digest = sample_digest(125)
    digest["freshness"] = {"last_band_record_at": None}
    digest["source"]["device_family"] = "unknown"
    digest["days"] = [{"date": digest["days"][0]["date"], "metrics": {}}]
    digest["nights"] = []
    digest["workouts"] = []
    response = client.post("/v1/digest", headers=AUTH, json=digest)
    assert response.status_code == 200, response.text
    health = client.get("/v1/health", headers=AUTH)
    assert health.status_code == 200
