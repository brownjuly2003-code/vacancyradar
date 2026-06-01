from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import polars as pl

from src.transform.slim_events import (
    SLIM_EVENTS_SCHEMA,
    build_slim_events_30d,
    list_partition_uploads,
    write_slim_events_partitioned,
)


def _seed_events(db_path: Path, rows: list[tuple]) -> None:
    """rows: (event_id, vacancy_id, employer_id, ts_naive, type, payload, source)"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id VARCHAR PRIMARY KEY,
            vacancy_id VARCHAR NOT NULL,
            employer_id VARCHAR,
            ts TIMESTAMP NOT NULL,
            type VARCHAR NOT NULL,
            payload VARCHAR,
            source VARCHAR NOT NULL
        )
        """
    )
    if rows:
        con.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()


def test_missing_db_yields_empty_frame_with_schema(tmp_path: Path):
    df = build_slim_events_30d(tmp_path / "missing.duckdb")
    assert df.is_empty()
    assert dict(df.schema) == SLIM_EVENTS_SCHEMA


def test_empty_table_yields_empty_frame_with_schema(tmp_path: Path):
    db = tmp_path / "events.duckdb"
    _seed_events(db, [])
    df = build_slim_events_30d(db)
    assert df.is_empty()
    assert dict(df.schema) == SLIM_EVENTS_SCHEMA


def test_filters_events_older_than_30_days(tmp_path: Path):
    now = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    fresh_ts = now - timedelta(days=5)
    stale_ts = now - timedelta(days=31)
    db = tmp_path / "events.duckdb"
    _seed_events(
        db,
        [
            ("e1", "hh:1", "100", fresh_ts.replace(tzinfo=None), "appeared", None, "hh"),
            ("e2", "hh:2", "100", stale_ts.replace(tzinfo=None), "appeared", None, "hh"),
        ],
    )
    df = build_slim_events_30d(db, now_utc=now)
    assert df.height == 1
    assert df["event_id"][0] == "e1"


def test_employer_id_namespaced(tmp_path: Path):
    now = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).replace(tzinfo=None)
    db = tmp_path / "events.duckdb"
    _seed_events(
        db,
        [
            ("e1", "hh:1", "100", ts, "appeared", None, "hh"),
            ("e2", "hh:2", None, ts, "appeared", None, "hh"),
        ],
    )
    df = build_slim_events_30d(db, now_utc=now).sort("event_id")
    assert df["employer_id"].to_list() == ["hh:100", None]


def test_ts_is_utc_timezone_aware(tmp_path: Path):
    now = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).replace(tzinfo=None)
    db = tmp_path / "events.duckdb"
    _seed_events(db, [("e1", "hh:1", "100", ts, "appeared", None, "hh")])
    df = build_slim_events_30d(db, now_utc=now)
    assert df.schema["ts"] == pl.Datetime("us", "UTC")
    assert df["ts"][0].tzinfo is not None


def test_sort_order_within_result(tmp_path: Path):
    now = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    base = now - timedelta(hours=2)
    db = tmp_path / "events.duckdb"
    _seed_events(
        db,
        [
            ("e3", "hh:3", "1", (base + timedelta(seconds=2)).replace(tzinfo=None), "appeared", None, "hh"),
            ("e1", "hh:1", "1", base.replace(tzinfo=None), "appeared", None, "hh"),
            ("e2", "hh:2", "1", base.replace(tzinfo=None), "closed", None, "hh"),
            ("e0", "hh:0", "1", base.replace(tzinfo=None), "appeared", None, "hh"),
        ],
    )
    df = build_slim_events_30d(db, now_utc=now)
    assert df["event_id"].to_list() == ["e0", "e1", "e2", "e3"]


def test_write_partitioned_creates_hive_layout(tmp_path: Path):
    now = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    db = tmp_path / "events.duckdb"
    _seed_events(
        db,
        [
            ("e1", "hh:1", "100", datetime(2026, 4, 25, 12, 0), "appeared", None, "hh"),
            ("e2", "hh:2", "100", datetime(2026, 4, 26, 9, 0), "closed", None, "hh"),
            ("e3", "hh:3", "100", datetime(2026, 4, 26, 18, 0), "appeared", None, "hh"),
        ],
    )
    df = build_slim_events_30d(db, now_utc=now)

    out_root = tmp_path / "slim_events_30d"
    written = write_slim_events_partitioned(df, out_root)

    expected = {
        out_root / "year=2026" / "month=04" / "day=25" / "events.parquet",
        out_root / "year=2026" / "month=04" / "day=26" / "events.parquet",
    }
    assert set(written) == expected
    for p in written:
        assert p.exists()

    # Per-day file row count
    apr25 = pl.read_parquet(out_root / "year=2026" / "month=04" / "day=25" / "events.parquet")
    apr26 = pl.read_parquet(out_root / "year=2026" / "month=04" / "day=26" / "events.parquet")
    assert apr25.height == 1
    assert apr26.height == 2


def test_write_partitioned_handles_empty_frame(tmp_path: Path):
    df = pl.DataFrame(schema=SLIM_EVENTS_SCHEMA)
    written = write_slim_events_partitioned(df, tmp_path / "out")
    assert written == []


def test_list_partition_uploads_maps_to_blob_paths(tmp_path: Path):
    out_root = tmp_path / "slim_events_30d"
    (out_root / "year=2026" / "month=04" / "day=25").mkdir(parents=True)
    (out_root / "year=2026" / "month=04" / "day=25" / "events.parquet").write_bytes(b"x")
    (out_root / "year=2026" / "month=04" / "day=27").mkdir(parents=True)
    (out_root / "year=2026" / "month=04" / "day=27" / "events.parquet").write_bytes(b"y")

    uploads = list_partition_uploads(out_root)
    pathnames = [p for _, p in uploads]
    assert pathnames == [
        "slim/events_30d/year=2026/month=04/day=25/events.parquet",
        "slim/events_30d/year=2026/month=04/day=27/events.parquet",
    ]
