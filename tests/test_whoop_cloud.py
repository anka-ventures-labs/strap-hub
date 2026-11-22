import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, date, datetime
from threading import Event
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from strap_hub.cli import main
from strap_hub.store import connect
from strap_hub.whoop_cloud import commands
from strap_hub.whoop_cloud.client import API_BASE, WhoopClient
from strap_hub.whoop_cloud.oauth import (
    AUTH_URL, SCOPES, TOKEN_URL, OAuth, OAuthConfig, TokenStore, Tokens, WhoopError,
    authorization_url, callback_code, new_state, token_path,
)
from strap_hub.whoop_cloud.sync import LAST_SYNC, SYNC_END, sync, sync_window

SLEEP_ID = "00000000-0000-4000-8000-000000000001"
NAP_ID = "00000000-0000-4000-8000-000000000002"
WORKOUT_ID = "00000000-0000-4000-8000-000000000003"
NOW = datetime(2026, 1, 14, 19, tzinfo=UTC)
SINCE = date(2026, 1, 7)


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    db = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db))
    monkeypatch.delenv("HUB_WHOOP_TOKEN_PATH", raising=False)
    monkeypatch.delenv("WHOOP_SYNC_WINDOW_DAYS", raising=False)
    store = TokenStore(token_path(db))
    with store.locked() as credentials:
        credentials.save(Tokens("fixture-access", "fixture-refresh", time.time() + 3600))
    return str(db), store


@pytest.fixture
def collections():
    sleep = {
        "id": SLEEP_ID, "user_id": 1, "nap": False, "score_state": "SCORED", "timezone_offset": "+07:00",
        "start": "2026-01-13T16:00:00Z", "end": "2026-01-13T23:00:00Z",
        "updated_at": "2026-01-14T01:00:00Z",
        "score": {
            "stage_summary": {
                "total_in_bed_time_milli": 25200000, "total_awake_time_milli": 1200000,
                "total_no_data_time_milli": 600000, "total_light_sleep_time_milli": 12600000,
                "total_slow_wave_sleep_time_milli": 5400000, "total_rem_sleep_time_milli": 5400000,
                "sleep_cycle_count": 4, "disturbance_count": 2,
            },
            "sleep_needed": {"baseline_milli": 25200000, "need_from_sleep_debt_milli": 1800000,
                             "need_from_recent_strain_milli": 900000, "need_from_recent_nap_milli": -1200000},
            "respiratory_rate": 14.2, "sleep_efficiency_percentage": 94.2,
            "sleep_performance_percentage": 86, "sleep_consistency_percentage": 91,
        },
    }
    return {
        "/cycle": [
            {
                "id": 1, "user_id": 1, "start": "2026-01-12T20:00:00Z", "end": "2026-01-13T20:00:00Z",
                "timezone_offset": "+07:00", "score_state": "SCORED", "step_count": 8100,
                "score": {"strain": 13.2, "kilojoule": 8368, "average_heart_rate": 72, "max_heart_rate": 160},
            },
            {
                "id": 2, "user_id": 1, "start": "2026-01-13T20:00:00Z", "end": None,
                "timezone_offset": "+07:00", "score_state": "PENDING_SCORE", "step_count": None,
                "score": {"strain": 99},
            },
        ],
        "/recovery": [{
            "cycle_id": 1, "user_id": 1, "sleep_id": SLEEP_ID, "score_state": "SCORED",
            "score": {"recovery_score": 80.4, "resting_heart_rate": 48, "hrv_rmssd_milli": 66.2,
                      "spo2_percentage": 98.1, "skin_temp_celsius": 33.2, "user_calibrating": False},
        }],
        "/activity/sleep": [sleep, {
            **sleep, "id": NAP_ID, "nap": True, "score_state": "UNSCORABLE",
            "start": "2026-01-14T05:00:00Z", "end": "2026-01-14T05:20:00Z", "score": None,
        }],
        "/activity/workout": [{
            "id": WORKOUT_ID, "user_id": 1, "start": "2026-01-14T05:00:00Z", "end": "2026-01-14T05:45:00Z",
            "timezone_offset": "-06:00", "sport_name": "running", "sport_id": 0, "score_state": "SCORED",
            "score": {"strain": 10.8, "average_heart_rate": 120, "max_heart_rate": 158, "kilojoule": 1046,
                      "percent_recorded": 95, "distance_meter": 4500, "altitude_gain_meter": 35,
                      "altitude_change_meter": -5, "zone_durations": {"zone_zero_milli": 120000,
                                                                     "zone_two_milli": 1200000}},
        }],
    }


def transport_for(collections, *, reject=None):
    def respond(request):
        assert request.url.host == "api.prod.whoop.com"
        assert request.headers["Authorization"] == "Bearer fixture-access"
        endpoint = request.url.path.removeprefix("/developer/v2")
        if reject == endpoint:
            return httpx.Response(403, text="fixture response body")
        return httpx.Response(200, json={"records": collections[endpoint], "next_token": None})

    return httpx.MockTransport(respond)


def test_authorization_url_and_rejects_callback_with_wrong_or_duplicate_state():
    config = OAuthConfig("fixture-id", "fixture-secret")
    state = new_state()
    assert len(state) == 8
    url = urlsplit(authorization_url(config, state))
    assert url._replace(query="").geturl() == AUTH_URL
    assert parse_qs(url.query) == {
        "response_type": ["code"], "client_id": ["fixture-id"], "redirect_uri": [config.redirect_uri],
        "scope": [" ".join(SCOPES)], "state": [state],
    }
    callback = config.redirect_uri + "?code=fixture-code&state="
    with pytest.raises(WhoopError, match="state did not match"):
        callback_code(callback + "wrong", config.redirect_uri, state)
    with pytest.raises(WhoopError, match="state did not match"):
        callback_code(callback + state + "&state=" + state, config.redirect_uri, state)
    with pytest.raises(WhoopError, match="does not match"):
        callback_code(callback.replace("/callback", "/elsewhere") + state, config.redirect_uri, state)
    assert callback_code(callback + state, config.redirect_uri, state) == "fixture-code"


def test_refresh_rotates_both_tokens_once_under_contention(cloud):
    _, store = cloud
    with store.locked() as credentials:
        credentials.save(Tokens("fixture-old-access", "fixture-old-refresh", time.time() - 10))
    first_refresh = Event()
    second_attempt = Event()
    calls = []

    def respond(request):
        calls.append(request)
        assert str(request.url) == TOKEN_URL
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["refresh_token"], "refresh_token": ["fixture-old-refresh"], "scope": ["offline"],
            "client_id": ["fixture-id"], "client_secret": ["fixture-secret"],
        }
        first_refresh.set()
        assert second_attempt.wait(timeout=5)
        return httpx.Response(200, json={"access_token": "fixture-new-access", "refresh_token": "fixture-new-refresh",
                                        "expires_in": 3600})

    def use_token(oauth):
        with oauth.session() as session:
            result = session.access_token()
            assert session.credentials.load().refresh_token == "fixture-new-refresh"
            return result

    with httpx.Client(transport=httpx.MockTransport(respond)) as http, ThreadPoolExecutor(2) as workers:
        oauth = OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http)
        first = workers.submit(use_token, oauth)
        assert first_refresh.wait(timeout=5)

        def contender():
            second_attempt.set()
            return use_token(oauth)

        second = workers.submit(contender)
        assert first.result(timeout=5) == second.result(timeout=5) == "fixture-new-access"

    assert len(calls) == 1
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.with_suffix(".json.lock").stat().st_mode & 0o777 == 0o600
    assert not store.path.with_suffix(".json.staged").exists()
    assert not store.path.with_suffix(".json.promote").exists()


@pytest.mark.parametrize("operation", ["rename", "directory_sync"])
def test_failed_token_promotion_recovers_rotated_pair_in_next_process(cloud, monkeypatch, operation):
    _, store = cloud
    with store.locked() as credentials:
        credentials.save(Tokens("fixture-old-access", "fixture-old-refresh", time.time() - 10))

    def respond(request):
        assert parse_qs(request.content.decode())["refresh_token"] == ["fixture-old-refresh"]
        return httpx.Response(200, json={"access_token": "fixture-new-access", "refresh_token": "fixture-new-refresh",
                                        "expires_in": 3600})

    replace = os.replace
    fsync = os.fsync
    replaced = False

    def replace_token(*args, **kwargs):
        nonlocal replaced
        if operation == "rename":
            raise OSError("fixture could not rename")
        replace(*args, **kwargs)
        replaced = True

    def sync_token(fd):
        if replaced:
            raise OSError("fixture could not sync directory")
        fsync(fd)

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        oauth = OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http)
        with monkeypatch.context() as patch:
            patch.setattr("strap_hub.whoop_cloud.oauth.os.replace", replace_token)
            patch.setattr("strap_hub.whoop_cloud.oauth.os.fsync", sync_token)
            with pytest.raises(OSError, match="fixture could not"):
                with oauth.session() as session:
                    session.access_token()
    staging = store.path.with_suffix(".json.staged")
    assert staging.stat().st_mode & 0o777 == 0o600

    subprocess.run([sys.executable, "-c", """
import sys
from pathlib import Path
import httpx
from strap_hub.whoop_cloud.oauth import OAuth, OAuthConfig, TokenStore

def unexpected_request(request):
    raise AssertionError("Recovery should reuse the rotated pair without refreshing")

store = TokenStore(Path(sys.argv[1]))
with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as http:
    with OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http).session() as session:
        assert session.access_token() == "fixture-new-access"
        assert session.tokens.refresh_token == "fixture-new-refresh"
""", str(store.path)], check=True, timeout=10)
    assert not staging.exists()
    assert not store.path.with_suffix(".json.promote").exists()
    assert json.loads(store.path.read_text())["refresh_token"] == "fixture-new-refresh"


def test_credentials_require_a_trusted_directory(tmp_path):
    parent = tmp_path / "shared"
    store = TokenStore(parent / "private" / "token.json")
    with store.locked() as credentials:
        assert credentials.load() is None
    assert store.path.parent.stat().st_mode & 0o777 == 0o700

    store.path.parent.chmod(0o770)
    with pytest.raises(WhoopError, match="not group/world writable"):
        with store.locked():
            pytest.fail("Opened an unsafe credential directory")
    assert store.status()["token_state"] == "unreadable"

    store.path.parent.chmod(0o700)
    parent.chmod(0o777)
    with pytest.raises(WhoopError, match="unsafe parent"):
        with store.locked():
            pytest.fail("A writable parent could swap the credential directory")


def test_pagination_and_rate_limit_backoff(cloud, monkeypatch):
    _, store = cloud
    waits = []
    requests = []
    monkeypatch.setattr("strap_hub.whoop_cloud.client.time.sleep", waits.append)

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "7"})
        if len(requests) == 2:
            return httpx.Response(503, headers={"Retry-After": "3"})
        if len(requests) == 3:
            return httpx.Response(200, json={"records": [{"id": 1}], "next_token": "mock += token"})
        assert request.url.params["nextToken"] == "mock += token"
        return httpx.Response(200, json={"records": [{"id": 2}], "next_token": None})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        oauth = OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http)
        with oauth.session() as session:
            assert list(WhoopClient(session).records("cloud_cycle", "from", "until")) == [{"id": 1}, {"id": 2}]

    assert waits == [7, 3]
    assert all(request.url.path == "/developer/v2/cycle" for request in requests)
    assert all(request.url.params["limit"] == "25" for request in requests)
    assert all(request.url.params["start"] == "from" and request.url.params["end"] == "until" for request in requests)


def test_sync_is_idempotent_rescores_and_keeps_unscored_rows(cloud, collections):
    db_path, store = cloud
    with httpx.Client(transport=transport_for(collections)) as http:
        oauth = OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http)
        assert sync(db_path, oauth, SINCE)["row_counts"] == {
            "cloud_cycle": 2, "cloud_recovery": 1, "cloud_sleep": 2, "cloud_workout": 1,
        }
        with closing(connect(db_path, read_only=True)) as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM cloud_cycle ORDER BY id")]
            assert tuple(db.execute(
                "SELECT date, score_state, strain, step_count FROM cloud_cycle WHERE id = 2"
            ).fetchone()) == ("2026-01-14", "PENDING_SCORE", None, None)
            assert "score" not in json.loads(db.execute("SELECT raw_json FROM cloud_cycle WHERE id = 2").fetchone()[0])
            assert tuple(db.execute(
                "SELECT date, nap, score_state, respiratory_rate FROM cloud_sleep WHERE nap = 1"
            ).fetchone()) == ("2026-01-14", 1, "UNSCORABLE", None)
        sync(db_path, oauth, SINCE)
        with closing(connect(db_path, read_only=True)) as db:
            assert before == [tuple(row) for row in db.execute("SELECT * FROM cloud_cycle ORDER BY id")]
        collections["/cycle"][1].update(score_state="SCORED", step_count=0, score={"strain": 11.5})
        sync(db_path, oauth)
    with closing(connect(db_path, read_only=True)) as db:
        assert tuple(db.execute("SELECT score_state, strain, step_count FROM cloud_cycle WHERE id = 2").fetchone()) == (
            "SCORED", 11.5, 0,
        )


def test_failed_collection_keeps_rows_and_cursor_untouched(cloud, collections):
    db_path, store = cloud
    config = OAuthConfig("fixture-id", "fixture-secret")
    with httpx.Client(transport=transport_for(collections)) as http:
        sync(db_path, OAuth(config, store, http), SINCE)
    with closing(connect(db_path, read_only=True)) as db:
        previous = dict(db.execute("SELECT key, value FROM meta"))
    collections["/cycle"][0]["step_count"] = 1
    with httpx.Client(transport=transport_for(collections, reject="/activity/workout")) as http:
        with pytest.raises(WhoopError, match="HTTP 403"):
            sync(db_path, OAuth(config, store, http))
    with closing(connect(db_path, read_only=True)) as db:
        assert dict(db.execute("SELECT key, value FROM meta")) == previous
        assert db.execute("SELECT step_count FROM cloud_cycle WHERE id = 1").fetchone()[0] == 8100


def test_sync_uses_previous_cutoff_through_outages(cloud, monkeypatch):
    db_path, _ = cloud
    connect(db_path).close()
    with closing(connect(db_path)) as db, db:
        db.execute("INSERT INTO meta VALUES (?, '2026-01-07T12:00:00Z')", (SYNC_END,))

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr("strap_hub.whoop_cloud.sync.datetime", FixedClock)
    monkeypatch.setenv("WHOOP_SYNC_WINDOW_DAYS", "3")
    assert sync_window(db_path).start == "2026-01-04T12:00:00.000000Z"
    assert sync_window(db_path, date(2026, 1, 1)).advance_cursor
    assert not sync_window(db_path, date(2026, 1, 13)).advance_cursor


def test_cli_authorizes_syncs_reports_status_and_revokes_without_disclosing_tokens(
    tmp_path, monkeypatch, capsys, collections,
):
    db_path = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    monkeypatch.delenv("HUB_WHOOP_TOKEN_PATH", raising=False)
    monkeypatch.setenv("WHOOP_CLIENT_ID", "fixture-id")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "fixture-secret")
    redirect = "https://localhost:8771/whoop/callback"
    monkeypatch.setenv("WHOOP_REDIRECT_URI", redirect)
    monkeypatch.setattr(commands, "new_state", lambda: "test1234")
    callback = redirect + "?state=test1234&code=fixture-code"
    monkeypatch.setattr(commands, "getpass", lambda _: callback)
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == TOKEN_URL
            assert parse_qs(request.content.decode()) == {
                "grant_type": ["authorization_code"], "client_id": ["fixture-id"],
                "client_secret": ["fixture-secret"], "code": ["fixture-code"], "redirect_uri": [redirect],
            }
            return httpx.Response(200, json={"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                                            "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer fixture-access"
        if request.method == "DELETE":
            assert str(request.url) == f"{API_BASE}/user/access"
            return httpx.Response(204)
        return httpx.Response(200, json={
            "records": collections[request.url.path.removeprefix("/developer/v2")],
        })

    monkeypatch.setattr(commands, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    main(["whoop-sync", "--dry-run", "--since", "2026-01-07"])
    assert json.loads(capsys.readouterr().out)["start"] == "2026-01-07T00:00:00.000000Z"
    assert not calls
    assert list(tmp_path.iterdir()) == []

    main(["whoop-auth", "--paste"])
    auth_output = capsys.readouterr().out
    assert AUTH_URL in auth_output
    assert not any(value in auth_output for value in ("fixture-secret", "fixture-code", "fixture-access"))
    main(["whoop-sync", "--since", "2026-01-07"])
    assert json.loads(capsys.readouterr().out)["row_counts"]["cloud_recovery"] == 1

    main(["whoop-status"])
    status_output = capsys.readouterr().out
    status = json.loads(status_output)
    assert status["token_state"] == "ready"
    assert status["token_expires_at"]
    assert status["last_sync_at"]
    assert "fixture-" not in status_output

    main(["whoop-revoke"])
    assert capsys.readouterr().out == "WHOOP access revoked.\n"
    main(["whoop-status"])
    after_revoke = json.loads(capsys.readouterr().out)
    assert after_revoke["token_state"] == "missing"
    assert after_revoke["row_counts"] == status["row_counts"]


def test_loopback_listener_ignores_bad_state_and_accepts_valid_callback(monkeypatch):
    import io

    attempts = iter([
        "/whoop/callback?state=incorrect&code=fixture-bad",
        "/whoop/callback?state=test1234&code=fixture-good",
    ])
    responses = []

    class LocalServer:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8771)
            self.handler = handler

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def handle_request(self):
            request = self.handler.__new__(self.handler)
            request.path = next(attempts)
            request.wfile = io.BytesIO()
            request.send_response = responses.append
            request.send_header = lambda *_: None
            request.end_headers = lambda: None
            request.do_GET()

    monkeypatch.setattr(commands, "HTTPServer", LocalServer)
    config = OAuthConfig("fixture-id", "fixture-secret")
    assert commands._loopback_code(config, authorization_url(config, "test1234"), "test1234") == "fixture-good"
    assert responses == [400, 200]


def test_mcp_prefers_cloud_and_labels_band_fallback(cloud, collections, monkeypatch):
    from strap_hub.mcp import server

    db_path, store = cloud
    monkeypatch.setattr(server, "_today", lambda: NOW.date())
    with httpx.Client(transport=transport_for(collections)) as http:
        sync_result = sync(db_path, OAuth(OAuthConfig("fixture-id", "fixture-secret"), store, http), SINCE)
    with closing(connect(db_path)) as db, db:
        db.executemany(
            "INSERT INTO day_metric (date, key, value, algo_version, received_at) VALUES (?, ?, ?, 1, ?)",
            [(day, key, value, NOW.isoformat()) for day, key, value in (
                ("2026-01-12", "rhr", 54),
                ("2026-01-13", "strain", 7),
                ("2026-01-14", "strain", 8),
                ("2026-01-14", "rhr", 64),
                ("2026-01-14", "readiness", 65),
                ("2026-01-14", "rmssd", 40),
                ("2026-01-14", "steps", 9000),
                ("2026-01-14", "tst_min", 290),
            )],
        )
        db.execute(
            """INSERT INTO sleep_segment (date, start, end, stage, algo_version) VALUES
               ('2026-01-12', '2026-01-12T03:30:00Z', '2026-01-12T04:00:00Z', 'rem', 1)"""
        )
        db.executemany(
            """INSERT INTO workout (id, date, start, type, duration_min, algo_version, received_at)
               VALUES (?, ?, ?, 'band-session', 30, 1, ?)""",
            [
                ("fixture-dupe", "2026-01-13", "2026-01-13T05:00:00Z", NOW.isoformat()),
                ("fixture-fallback", "2026-01-12", "2026-01-12T05:00:00Z", NOW.isoformat()),
            ],
        )

    freshness = json.loads(server.data_freshness())
    assert freshness["source"] == "whoop_cloud"
    assert freshness["token_state"] == "ready"
    assert freshness["cloud_last_sync_at"] == freshness["as_of"] == sync_result["last_sync_at"]
    assert freshness["days_stored"] == 3

    strain = json.loads(server.strain_day("2026-01-13"))
    assert strain["source"] == "whoop_cloud"
    assert strain["strain"] == 13.2 and strain["steps"] == 8100
    assert strain["kilojoule"] == 8368 and strain["avg_hr"] == 72 and strain["max_hr"] == 160
    pending = json.loads(server.strain_day("2026-01-14"))
    assert pending["score_state"] == "PENDING_SCORE"
    assert pending["strain"] == 8 and pending["steps"] == 9000
    assert pending["band_keys"] == ["steps", "strain"]

    night = json.loads(server.sleep_night("2026-01-14"))
    assert night["source"] == "whoop_cloud"
    assert night["segments"] == [] and night["wake_count"] is None
    assert night["totals"]["tst_min"] == 390 and night["disturbance_count"] == 2
    band_night = json.loads(server.sleep_night("2026-01-12"))
    assert band_night["source"] == "band" and band_night["totals"]["rem_min"] == 30

    detail = json.loads(server.sleep_detail("2026-01-14"))
    assert detail["sleep"]["sleep_need_min"] == {"baseline": 420, "debt": 30, "recent_strain": 15, "recent_nap": -20}
    assert detail["sleep"]["sleep_performance_percentage"] == 86
    assert detail["sleep"]["sleep_consistency_percentage"] == 91
    assert detail["sleep"]["sleep_efficiency_percentage"] == 94.2
    assert detail["sleep"]["respiratory_rate"] == 14.2
    assert detail["recovery"] == {
        "source": "whoop_cloud", "score_state": "SCORED", "readiness": 80.4, "rmssd": 66.2,
        "rhr": 48, "spo2_percentage": 98.1, "skin_temp_celsius": 33.2, "user_calibrating": False,
    }
    assert detail["naps"][0]["nap"] and detail["naps"][0]["totals"] == {}
    band_detail = json.loads(server.sleep_detail("2026-01-12"))
    assert band_detail["source"] == band_detail["recovery"]["source"] == "band"

    summary = json.loads(server.sleep_summary(1))
    assert summary["nights"][0]["source"] == "whoop_cloud"
    assert summary["nights"][0]["tst_min"] == 390 and summary["nights"][0]["rmssd"] == 66.2
    readiness = json.loads(server.readiness_today())
    assert readiness["days"][0]["source"] == readiness["strain_yesterday_source"] == "whoop_cloud"
    assert readiness["days"][0]["readiness"] == 80.4 and readiness["strain_yesterday"] == 13.2
    assert json.loads(server.metric_trend("rhr", 3))["values"] == [
        {"date": "2026-01-12", "value": 54, "source": "band"},
        {"date": "2026-01-14", "value": 48, "source": "whoop_cloud"},
    ]

    workout_page = json.loads(server.workouts(limit=1))
    assert workout_page["workouts"] == [{
        "date": "2026-01-13", "type": "running", "source": "whoop_cloud", "duration_min": 45,
        "strain": 10.8, "calories": 250, "max_hr": 158, "zone_min": {"z0": 2, "z2": 20},
        "score_state": "SCORED", "kilojoule": 1046, "avg_hr": 120, "percent_recorded": 95,
        "distance_meter": 4500, "altitude_gain_meter": 35, "altitude_change_meter": -5,
    }]
    following = json.loads(server.workouts(limit=1, cursor=workout_page["next_cursor"]))
    assert following["workouts"][0]["source"] == "band"
    assert following["totals"] == workout_page["totals"] == {
        "count": 2, "duration_min": 75, "calories": 250, "zone_min": {"z0": 2, "z2": 20},
    }
