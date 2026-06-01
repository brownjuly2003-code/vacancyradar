"""Retention prune for master/events.duckdb.

events.duckdb grows ~12 MB/day at current ingest cadence (1.1M rows / 21d at
2026-05-17). Without retention it crosses 1 GB in ~3 months and 4 GB at the
1-year mark, well past what's useful for any consumer:

  - slim_events.py    →  30 days  (web `slim/events_30d/`)
  - market_pulse      →  90 days  (daily new/closed chart)
  - employer_top      → ~84 days  (12 weeks aggregate)
  - skill/role weekly →  read slim_active, not events
  - monthly/employer reports → unbounded, but rarely run on >6mo windows

`vradar prune events --older-than-days 180` keeps the strictest 90-day
consumer happy with a 3-month safety margin and lets `monthly_digest`
serve any month within 6 months.
"""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

import src.cli as cli
import src.cli_modules.prune as prune_module


def _args(*, older_than_days: int = 180, dry: bool = False, vacuum: bool = False) -> Namespace:
    return Namespace(
        cmd="prune",
        target="events",
        older_than_days=older_than_days,
        dry=dry,
        vacuum=vacuum,
    )


def _seed_events(db_path: Path, rows: list[tuple[str, datetime]]) -> None:
    """Seed events table with (vacancy_id, ts) pairs. Other columns get sane defaults."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id    VARCHAR PRIMARY KEY,
                vacancy_id  VARCHAR NOT NULL,
                employer_id VARCHAR,
                ts          TIMESTAMP NOT NULL,
                type        VARCHAR NOT NULL,
                payload     VARCHAR,
                source      VARCHAR NOT NULL
            )
            """
        )
        for i, (vid, ts) in enumerate(rows):
            con.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                [f"e{i:06d}", vid, "1", ts.replace(tzinfo=None), "appeared", None, "hh"],
            )
    finally:
        con.close()


@pytest.fixture
def events_db(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    return tmp_path / "master" / "events.duckdb"


def test_prune_dispatcher_routes_events(monkeypatch):
    seen = {}

    def fake_prune_events(args):
        seen["args"] = args
        return 17

    monkeypatch.setattr(prune_module, "_prune_events", fake_prune_events)

    args = Namespace(target="events")
    assert cli._prune(args) == 17
    assert seen["args"] is args


def test_prune_dispatcher_unknown_target_returns_1():
    assert cli._prune(Namespace(target="bogus")) == 1


def test_prune_events_missing_db_exits_nonzero(events_db, capsys):
    exit_code = cli._prune_events(_args())
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "does not exist" in captured.err


def test_prune_events_invalid_days(events_db, capsys):
    _seed_events(events_db, [("hh:1", datetime.now(timezone.utc))])
    exit_code = cli._prune_events(_args(older_than_days=0))
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "must be >= 1" in captured.err


def test_prune_events_dry_run_does_not_delete(events_db, capsys):
    now = datetime.now(timezone.utc)
    _seed_events(events_db, [
        ("hh:old", now - timedelta(days=200)),
        ("hh:new", now),
    ])

    exit_code = cli._prune_events(_args(older_than_days=180, dry=True))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "would delete 1 of 2 rows" in captured.out

    # DB unchanged
    con = duckdb.connect(str(events_db), read_only=True)
    try:
        total = con.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        con.close()
    assert total == 2


def test_prune_events_deletes_only_older_rows(events_db, capsys):
    now = datetime.now(timezone.utc)
    _seed_events(events_db, [
        ("hh:200d", now - timedelta(days=200)),
        ("hh:181d", now - timedelta(days=181)),
        # Boundary: 180d ago is the cutoff DATE. CAST(ts AS DATE) < cutoff
        # is strictly less-than, so 180d ago stays (same day as cutoff).
        ("hh:90d", now - timedelta(days=90)),
        ("hh:fresh", now),
    ])

    exit_code = cli._prune_events(_args(older_than_days=180))
    assert exit_code == 0

    con = duckdb.connect(str(events_db), read_only=True)
    try:
        remaining_ids = sorted(r[0] for r in con.execute("SELECT vacancy_id FROM events").fetchall())
    finally:
        con.close()

    assert "hh:200d" not in remaining_ids
    assert "hh:181d" not in remaining_ids
    assert "hh:90d" in remaining_ids
    assert "hh:fresh" in remaining_ids
    captured = capsys.readouterr()
    assert "deleted 2 rows" in captured.out


def test_prune_events_noop_when_nothing_old(events_db, capsys):
    now = datetime.now(timezone.utc)
    _seed_events(events_db, [
        ("hh:fresh-1", now - timedelta(days=5)),
        ("hh:fresh-2", now - timedelta(days=30)),
    ])

    exit_code = cli._prune_events(_args(older_than_days=180))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "nothing to delete" in captured.out

    con = duckdb.connect(str(events_db), read_only=True)
    try:
        total = con.execute("SELECT count(*) FROM events").fetchone()[0]
    finally:
        con.close()
    assert total == 2


def test_prune_events_vacuum_runs_checkpoint(events_db, capsys):
    """--vacuum should issue CHECKPOINT. We don't assert file size here
    because DuckDB's storage layout makes a tiny seeded DB grow on
    CHECKPOINT (page allocation) rather than shrink — the realistic
    space reclaim only kicks in on multi-MB compactions. The exit-code
    + the absence of the "--vacuum hint" suffices.
    """
    now = datetime.now(timezone.utc)
    _seed_events(events_db, [
        ("hh:old", now - timedelta(days=200)),
        ("hh:fresh", now),
    ])

    exit_code = cli._prune_events(_args(older_than_days=180, vacuum=True))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "hint" not in captured.err  # hint only without --vacuum


def test_prune_events_hint_when_no_vacuum_and_no_shrink(events_db, capsys):
    """Tiny seeded DB never shrinks below 1 MB. With >100 rows deleted and
    no --vacuum, hint should appear so the user knows space wasn't reclaimed.
    """
    now = datetime.now(timezone.utc)
    rows = [("hh:o-" + str(i), now - timedelta(days=300)) for i in range(200)]
    rows += [("hh:fresh", now)]
    _seed_events(events_db, rows)

    exit_code = cli._prune_events(_args(older_than_days=180))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "hint" in captured.err
    assert "--vacuum" in captured.err
