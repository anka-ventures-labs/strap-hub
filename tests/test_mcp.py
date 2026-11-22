import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from mcp import Client

from strap_hub.mcp import server
from strap_hub.store import connect

AS_OF = "2026-01-14T06:00:00.000125Z"
RECEIVED = "2026-01-14T07:00:00Z"
EXAMPLE_CALLS = {
    "data_freshness": {},
    "sleep_summary": {},
    "sleep_night": {"date": "2026-01-12"},
    "workouts": {},
    "readiness_today": {},
    "metric_trend": {"key": "rhr"},
    "strain_day": {"date": "2026-01-12"},
    "sleep_detail": {"date": "2026-01-12"},
}


def freeze_clock(monkeypatch, instant):
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(server, "datetime", FixedClock)


@pytest.fixture
def empty_store(tmp_path, monkeypatch):
    db_path = tmp_path / "hub.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    monkeypatch.setenv("HUB_TZ", "Etc/UTC")
    monkeypatch.delenv("HUB_WHOOP_TOKEN_PATH", raising=False)
    freeze_clock(monkeypatch, datetime(2026, 1, 14, 12, tzinfo=UTC))
    connect().close()
    return db_path


@pytest.fixture
def populated_store(empty_store):
    metrics = {
        "2025-12-14": {"rhr": 200, "readiness": 5},
        "2025-12-15": {"rhr": 65, "rmssd": 30, "readiness": 60},
        "2025-12-16": {"rhr": 60},
        "2026-01-07": {"tst_min": 380, "rmssd": 45},
        "2026-01-08": {"tst_min": 390.4},
        "2026-01-11": {"rhr": 59, "rmssd": 40},
        "2026-01-12": {
            "tst_min": 420.6,
            "deep_min": 80.4,
            "rem_min": 90,
            "light_min": 250,
            "efficiency": 92.16,
            "rhr": 56,
            "rmssd": 50,
            "resp_rate": 13.46,
            "readiness": 70,
        },
        "2026-01-13": {"tst_min": 400.4, "rmssd": 55, "strain": 8.46},
        "2026-01-14": {"tst_min": 450.6, "rhr": 52, "rmssd": 60, "readiness": 78, "custom_metric": 2.345},
        "2026-01-15": {"tst_min": 900, "rhr": 19, "rmssd": 170, "readiness": 98},
    }
    with closing(connect()) as db, db:
        db.executemany(
            "INSERT INTO day_metric (date, key, value, algo_version, received_at) VALUES (?, ?, ?, 70, ?)",
            [(day, key, value, RECEIVED) for day, daily in metrics.items() for key, value in daily.items()],
        )
        db.executemany(
            'INSERT INTO sleep_segment (date, start, "end", stage, algo_version) VALUES (?, ?, ?, ?, 70)',
            [
                ("2026-01-10", "2026-01-09T23:00:00Z", "2026-01-10T00:00:00Z", "light"),
                ("2026-01-12", "2026-01-11T23:00:00Z", "2026-01-11T23:30:00Z", "light"),
                ("2026-01-12", "2026-01-11T23:30:00+00:00", "2026-01-12T00:00:00Z", "light"),
                ("2026-01-12", "2026-01-12T00:00:00Z", "2026-01-12T00:01:00Z", "wake"),
                ("2026-01-12", "2026-01-12T00:01:00Z", "2026-01-12T00:03:00Z", "wake"),
                ("2026-01-12", "2026-01-12T00:03:00Z", "2026-01-12T00:43:00Z", "deep"),
                ("2026-01-12", "2026-01-12T00:43:00Z", "2026-01-12T01:13:00Z", "rem"),
                ("2026-01-12", "2026-01-12T01:13:00Z", "2026-01-12T01:18:00Z", "unobserved"),
                ("2026-01-12", "2026-01-12T01:18:00Z", "2026-01-12T01:19:00Z", "wake"),
                ("2026-01-12", "2026-01-12T01:25:00Z", "2026-01-12T01:27:00Z", "wake"),
            ],
        )
        db.executemany(
            """INSERT INTO workout (
                   id, date, start, type, duration_min, strain, calories, max_hr,
                   zone_min_json, algo_version, received_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 70, ?)""",
            [
                (
                    "test-run", "2026-01-12", "2026-01-11T23:45:00Z", "running", 30.4, 9.76, 250.14, 172,
                    '{"z1": 10.4, "z2": 4.4}', RECEIVED,
                ),
                (
                    "test-strength", "2026-01-14", "2026-01-14T11:00:00Z", "strength", 45.4, None, 120.14, None,
                    '{"z1": 5.4, "z3": 6.6}', RECEIVED,
                ),
                (
                    "test-row", "2026-01-11", "2026-01-11T11:00:00Z", "rowing", 25, None, None, None,
                    None, RECEIVED,
                ),
                (
                    "test-cycle", "2026-01-09", "2026-01-09T11:00:00Z", "cycling", 20, None, None, None,
                    None, RECEIVED,
                ),
            ],
        )
        db.executemany(
            "INSERT INTO digest_log (received_at, last_band_record_at, source) VALUES (?, ?, ?)",
            [
                (RECEIVED, AS_OF, "push"),
                ("2026-01-14T07:01:00Z", "2026-01-14T06:00:00+00:00", "push"),
                ("2026-01-14T07:02:00Z", None, "import"),
            ],
        )
    return empty_store


def call(name, **arguments):
    async def invoke():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, arguments)

    return asyncio.run(invoke())


def decode(result):
    assert not result.is_error, result.content
    assert len(result.content) == 1
    content = result.content[0].text
    data = json.loads(content)
    assert "as_of" in data
    assert data["source"] in {"band", "whoop_cloud"}
    assert content == json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return data


def test_data_freshness(populated_store):
    assert decode(call("data_freshness")) == {
        "as_of": AS_OF,
        "last_digest_at": "2026-01-14T07:02:00Z",
        "last_band_record_at": AS_OF,
        "newest_day": "2026-01-15",
        "days_stored": 12,
        "source": "band",
        "cloud_last_sync_at": None,
        "token_state": "missing",
        "token_expires_at": None,
    }


def test_sleep_summary_default_and_previous_equal_period(populated_store):
    result = decode(call("sleep_summary"))
    assert result["as_of"] == AS_OF
    assert [night["date"] for night in result["nights"]] == [
        "2026-01-08", "2026-01-11", "2026-01-12", "2026-01-13", "2026-01-14",
    ]
    assert result["nights"][0] == {"date": "2026-01-08", "tst_min": 390, "source": "band"}
    assert result["nights"][2] == {
        "date": "2026-01-12", "source": "band", "tst_min": 421, "deep_min": 80, "rem_min": 90, "light_min": 250,
        "efficiency": 92.2, "rhr": 56, "rmssd": 50, "resp_rate": 13.5,
    }
    assert result["averages"] == {
        "tst_min": 416, "deep_min": 80, "rem_min": 90, "light_min": 250,
        "efficiency": 92.2, "rhr": 55.7, "rmssd": 51.2, "resp_rate": 13.5,
    }
    assert result["change"] == {"tst_min": 36, "rmssd": 6.2}
    assert decode(call("sleep_summary", days=1))["change"] == {"tst_min": 50, "rmssd": 5}
    assert decode(call("sleep_summary", days=90))["nights"][0]["date"] == "2025-12-14"


def test_sleep_night_merges_only_touching_segments_and_counts_wakes(populated_store):
    result = decode(call("sleep_night", date="2026-01-12"))
    assert result["as_of"] == AS_OF
    assert [segment["stage"] for segment in result["segments"]] == [
        "light", "wake", "deep", "rem", "unobserved", "wake", "wake",
    ]
    assert result["segments"][0] == {
        "start": "2026-01-11T23:00:00Z", "end": "2026-01-12T00:00:00Z", "stage": "light",
    }
    assert result["totals"] == {
        "tst_min": 130, "light_min": 60, "wake_min": 6, "deep_min": 40, "rem_min": 30, "unobserved_min": 5,
    }
    assert result["wake_count"] == 3
    assert decode(call("sleep_night", date="2026-01-10"))["wake_count"] == 0
    assert decode(call("sleep_night", date="2026-01-11"))["segments"] == []


def test_workouts_default_and_inclusive_custom_ranges(populated_store):
    result = decode(call("workouts"))
    assert result["as_of"] == AS_OF
    assert result["workouts"] == [
        {
            "date": "2026-01-14", "type": "strength", "source": "band", "duration_min": 45, "calories": 120.1,
            "zone_min": {"z1": 5, "z3": 7},
        },
        {
            "date": "2026-01-12", "type": "running", "source": "band", "duration_min": 30, "strain": 9.8,
            "calories": 250.1, "max_hr": 172, "zone_min": {"z1": 10, "z2": 4},
        },
    ]
    assert result["totals"] == {
        "count": 2, "duration_min": 76, "calories": 370.3, "zone_min": {"z1": 16, "z2": 4, "z3": 7},
    }
    inclusive = decode(call("workouts", **{"from": "2026-01-11", "to": "2026-01-12"}))
    assert [row["type"] for row in inclusive["workouts"]] == ["running", "rowing"]
    assert decode(call("workouts", **{"from": "2026-01-11"}))["totals"]["count"] == 3
    assert decode(call("workouts", to="2026-01-12"))["totals"]["count"] == 1


def test_workouts_cursor_pages_stably_with_full_range_totals(empty_store):
    with closing(connect()) as db, db:
        db.executemany(
            """INSERT INTO workout (id, date, start, type, duration_min, calories, zone_min_json,
                                     algo_version, received_at)
               VALUES (?, '2026-01-14', ?, ?, 2.5, 10, '{"z2": 1.5}', 70, ?)""",
            [
                (f"synthetic-{index:02}", "2026-01-14T11:00:00Z", f"workout-{index}", RECEIVED)
                for index in range(50)
            ] + [("synthetic-fraction", "2026-01-14T11:00:00.000001Z", "newest", RECEIVED)],
        )

    first = decode(call("workouts"))
    assert len(first["workouts"]) == 50
    assert [row["type"] for row in first["workouts"]] == [
        "newest", *(f"workout-{index}" for index in range(49, 0, -1)),
    ]
    last = decode(call("workouts", limit=1, cursor=first["next_cursor"]))
    assert [row["type"] for row in last["workouts"]] == ["workout-0"]
    assert "next_cursor" not in last
    assert first["totals"] == last["totals"] == {
        "count": 51, "duration_min": 128, "calories": 510, "zone_min": {"z2": 76},
    }


def test_workouts_order_by_utc_start_not_local_date(empty_store):
    # A later local date can start earlier in UTC (phone crossed time zones).
    with closing(connect()) as db, db:
        db.executemany(
            """INSERT INTO workout (id, date, start, type, algo_version, received_at)
               VALUES (?, ?, ?, ?, 70, ?)""",
            [
                ("synthetic-east", "2026-01-14", "2026-01-13T20:00:00Z", "earlier-utc", RECEIVED),
                ("synthetic-west", "2026-01-13", "2026-01-14T10:00:00Z", "later-utc", RECEIVED),
            ],
        )
    page = decode(call("workouts", **{"from": "2026-01-13", "to": "2026-01-14"}, limit=1))
    assert [row["type"] for row in page["workouts"]] == ["later-utc"]
    rest = decode(call("workouts", **{"from": "2026-01-13", "to": "2026-01-14"}, cursor=page["next_cursor"]))
    assert [row["type"] for row in rest["workouts"]] == ["earlier-utc"]


def test_default_week_switches_at_local_monday(populated_store, monkeypatch):
    freeze_clock(monkeypatch, datetime(2026, 1, 11, 23, 30, tzinfo=UTC))
    sunday = decode(call("workouts"))
    assert [row["date"] for row in sunday["workouts"]] == ["2026-01-11", "2026-01-09"]
    assert decode(call("sleep_summary", days=1))["nights"][0]["date"] == "2026-01-11"

    monkeypatch.setenv("HUB_TZ", "Asia/Tokyo")
    monday = decode(call("workouts"))
    assert [row["date"] for row in monday["workouts"]] == ["2026-01-12"]
    assert decode(call("sleep_summary", days=1))["nights"][0]["date"] == "2026-01-12"


def test_week_defaults_to_hub_timezone(populated_store, monkeypatch):
    class HubClock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is None
            return datetime(2026, 1, 12, 8, 30)

    monkeypatch.delenv("HUB_TZ")
    monkeypatch.setattr(server, "datetime", HubClock)
    assert decode(call("workouts"))["totals"]["count"] == 1


def test_readiness_uses_matching_day_and_30_preceding_days(populated_store):
    assert decode(call("readiness_today")) == {
        "as_of": AS_OF,
        "days": [
            {
                "date": "2026-01-14", "source": "band", "readiness": 78, "rmssd": 60, "rhr": 52,
                "baseline_30d": {"readiness": 65, "rmssd": 44, "rhr": 60},
                "change": {"readiness": 13, "rmssd": 16, "rhr": -8},
            },
        ],
        "strain_yesterday": 8.5,
        "strain_yesterday_source": "band",
        "source": "band",
    }
    with closing(connect()) as db, db:
        db.execute("DELETE FROM day_metric WHERE date = '2026-01-14'")

    fallback = decode(call("readiness_today"))
    assert fallback["days"] == [
        {"date": "2026-01-13", "source": "band", "rmssd": 55, "baseline_30d": {"readiness": 45, "rmssd": 41.2, "rhr": 88},
         "change": {"rmssd": 13.8}},
    ]
    assert fallback["strain_yesterday"] == 8.5


def test_metric_trend_defaults_and_arbitrary_stored_keys(populated_store):
    assert decode(call("metric_trend", key="rhr")) == {
        "as_of": AS_OF,
        "key": "rhr",
        "source": "band",
        "values": [
            {"date": "2025-12-16", "value": 60, "source": "band"},
            {"date": "2026-01-11", "value": 59, "source": "band"},
            {"date": "2026-01-12", "value": 56, "source": "band"},
            {"date": "2026-01-14", "value": 52, "source": "band"},
        ],
        "mean": 56.8, "min": 52, "max": 60,
    }
    assert decode(call("metric_trend", key="custom_metric", days=1)) == {
        "as_of": AS_OF, "key": "custom_metric", "source": "band",
        "values": [{"date": "2026-01-14", "value": 2.345, "source": "band"}],
        "mean": 2.345, "min": 2.345, "max": 2.345,
    }
    assert decode(call("metric_trend", key="tst_min", days=1))["mean"] == 451
    assert decode(call("metric_trend", key="rhr", days=365))["max"] == 200
    assert decode(call("metric_trend", key="rhr' OR 1=1 --"))["values"] == []


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("sleep_summary", {"days": 0}),
        ("sleep_summary", {"days": 91}),
        ("sleep_summary", {"days": 1.5}),
        ("metric_trend", {"key": "rmssd", "days": 0}),
        ("metric_trend", {"key": "rmssd", "days": 366}),
        ("metric_trend", {"key": "rmssd", "days": True}),
        ("metric_trend", {"days": 7}),
        ("sleep_night", {}),
        ("sleep_night", {"date": "2026-1-12"}),
        ("sleep_night", {"date": "2026-02-30"}),
        ("workouts", {"from": "2026-01-13", "to": "2026-01-12"}),
        ("workouts", {"from": "20260112"}),
        ("workouts", {"to": "2026-11-31"}),
        ("workouts", {"limit": 0}),
        ("workouts", {"limit": 101}),
        ("workouts", {"cursor": "bad cursor"}),
    ],
)
def test_invalid_arguments_return_tool_errors(empty_store, name, arguments):
    assert call(name, **arguments).is_error


@pytest.mark.parametrize("with_freshness", [False, True])
def test_empty_store_returns_empty_results_with_freshness(empty_store, with_freshness):
    if with_freshness:
        with closing(connect()) as db, db:
            db.execute(
                "INSERT INTO digest_log (received_at, last_band_record_at, source) VALUES (?, ?, 'push')",
                (RECEIVED, AS_OF),
            )

    async def invoke():
        async with Client(server.mcp) as client:
            return {name: decode(await client.call_tool(name, args)) for name, args in EXAMPLE_CALLS.items()}

    results = asyncio.run(invoke())
    assert all(result["as_of"] == (AS_OF if with_freshness else None) for result in results.values())
    assert results["data_freshness"]["days_stored"] == 0
    assert results["data_freshness"]["newest_day"] is None
    assert results["sleep_summary"]["nights"] == []
    assert results["sleep_summary"]["averages"] == {}
    assert results["sleep_night"]["segments"] == []
    assert results["sleep_night"]["wake_count"] is None
    assert results["workouts"]["workouts"] == []
    assert results["workouts"]["totals"] == {}
    assert results["readiness_today"]["days"] == []
    assert results["readiness_today"]["strain_yesterday"] is None
    assert results["metric_trend"]["values"] == []
    assert "mean" not in results["metric_trend"]


def test_every_tool_opens_store_read_only(populated_store, monkeypatch):
    opened = []

    def observed_connect(path, *, read_only):
        assert read_only is True
        db = connect(path, read_only=read_only)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("INSERT INTO meta VALUES ('probe', 'probe')")
        db.rollback()
        opened.append(db)
        return db

    monkeypatch.setattr(server, "connect", observed_connect)

    async def invoke():
        async with Client(server.mcp) as client:
            for name, args in EXAMPLE_CALLS.items():
                decode(await client.call_tool(name, args))

    before = populated_store.read_bytes()
    asyncio.run(invoke())
    assert len(opened) == len(EXAMPLE_CALLS)
    assert populated_store.read_bytes() == before


def test_database_path_with_uri_characters_never_creates_a_database(tmp_path, monkeypatch):
    path = tmp_path / "hub?mode=rwc&flag=x#v.db"
    monkeypatch.setenv("HUB_DB_PATH", str(path))
    result = call("data_freshness")
    assert result.is_error
    assert "Check HUB_DB_PATH" in result.content[0].text
    assert list(tmp_path.iterdir()) == []

    connect().close()
    assert decode(call("data_freshness"))["days_stored"] == 0
    assert path.is_file()
    assert not (tmp_path / "hub").exists()


def test_sdk_lists_exact_tool_names_and_input_schemas():
    async def list_tools():
        async with Client(server.mcp, mode="legacy") as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(list_tools())
    assert {tool.name for tool in tools} == set(EXAMPLE_CALLS)
    assert len(tools) == len(EXAMPLE_CALLS)
    assert all(tool.description and tool.annotations.read_only_hint for tool in tools)
    schemas = {tool.name: tool.input_schema for tool in tools}
    for name in ("data_freshness", "readiness_today"):
        assert schemas[name]["properties"] == {}

    for name, default, maximum in (("sleep_summary", 7, 90), ("metric_trend", 30, 365)):
        days = schemas[name]["properties"]["days"]
        assert (days["type"], days["minimum"], days["maximum"], days["default"]) == ("integer", 1, maximum, default)
    assert set(schemas["sleep_summary"]["properties"]) == {"days"}
    assert set(schemas["metric_trend"]["properties"]) == {"key", "days"}
    assert schemas["metric_trend"]["required"] == ["key"]
    assert schemas["metric_trend"]["properties"]["key"]["type"] == "string"
    for name in ("sleep_night", "strain_day", "sleep_detail"):
        assert set(schemas[name]["properties"]) == {"date"}
        assert schemas[name]["required"] == ["date"]
        assert schemas[name]["properties"]["date"]["type"] == "string"
    assert set(schemas["workouts"]["properties"]) == {"from", "to", "limit", "cursor"}
    assert schemas["workouts"].get("required", []) == []
    for name in ("from", "to", "cursor"):
        field = schemas["workouts"]["properties"][name]
        assert field["default"] is None
        assert {arm["type"] for arm in field["anyOf"]} == {"string", "null"}
    limit = schemas["workouts"]["properties"]["limit"]
    assert (limit["type"], limit["minimum"], limit["maximum"], limit["default"]) == ("integer", 1, 100, 50)


def test_pre_cloud_v2_store_remains_readable_without_migration(tmp_path, monkeypatch):
    from strap_hub.store.db import SCHEMA

    db_path = tmp_path / "v2.db"
    monkeypatch.setenv("HUB_DB_PATH", str(db_path))
    freeze_clock(monkeypatch, datetime(2026, 1, 14, 12, tzinfo=UTC))
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(SCHEMA)
        db.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('schema_version', '2');
            INSERT INTO day_metric (date, key, value, algo_version, received_at)
            VALUES ('2026-01-12', 'rhr', 52, 1, '2026-01-12T12:00:00Z');
        """)
    before = db_path.read_bytes()
    for name, args in EXAMPLE_CALLS.items():
        assert decode(call(name, **args))["source"] == "band"
    assert db_path.read_bytes() == before
