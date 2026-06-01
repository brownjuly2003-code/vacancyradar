"""`vradar prune {events}` impl. Extracted from src/cli.py (Kimi P1-1)."""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _prune(args: argparse.Namespace) -> int:
    if args.target == "events":
        return _prune_events(args)
    return 1


def _prune_events(args: argparse.Namespace) -> int:
    """Drop events.duckdb rows with ts < now - --older-than-days.

    Consumers in this codebase look at these windows:
      - slim_events.py:    30 days  (slim/events_30d/* parquet, web)
      - market_pulse:      90 days  (daily new/closed time-series)
      - employer_top:     ~84 days  (12 weeks aggregate)
      - skill_velocity:   reads slim_active, no direct events scan
      - role_salary:      reads slim_active, no direct events scan
      - monthly_digest.qmd: ad-hoc month range, unbounded scan per pick
      - employer_profile.qmd: unbounded per-employer history

    Default 180 days gives `monthly_digest` 6mo of selectable months while
    leaving a 90-day safety margin above the strictest web consumer.

    Older rows are unused by /trends and /api/* — they only matter for
    ad-hoc reports. The raw lake parquet is the source of truth and is
    untouched; events.duckdb is regenerable from it via
    `tools/backfill_events.py`.
    """
    import duckdb

    events_db = Path("master/events.duckdb")
    if not events_db.exists():
        print(f"[err] {events_db} does not exist", file=sys.stderr)
        return 2

    days = args.older_than_days
    if days < 1:
        print(f"[err] --older-than-days must be >= 1, got {days}", file=sys.stderr)
        return 2

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    size_before = events_db.stat().st_size

    con = duckdb.connect(str(events_db), read_only=args.dry)
    try:
        affected_row = con.execute(
            "SELECT count(*) FROM events WHERE CAST(ts AS DATE) < ?",
            [cutoff],
        ).fetchone()
        total_row = con.execute("SELECT count(*) FROM events").fetchone()
        # COUNT(*) always returns exactly one row; assert for type-narrowing.
        assert affected_row is not None and total_row is not None
        affected = affected_row[0]
        total = total_row[0]

        if args.dry:
            print(
                f"[prune dry-run] events: would delete {affected} of {total} rows "
                f"(ts < {cutoff})"
            )
            return 0

        if affected == 0:
            print(
                f"[prune] events: nothing to delete "
                f"(0 of {total} rows older than {cutoff})"
            )
            return 0

        con.execute("DELETE FROM events WHERE CAST(ts AS DATE) < ?", [cutoff])
        if args.vacuum:
            con.execute("CHECKPOINT")
    finally:
        con.close()

    size_after = events_db.stat().st_size
    delta_mb = (size_before - size_after) / 1024 / 1024
    print(
        f"[prune] events: deleted {affected} rows (ts < {cutoff}). "
        f"DB size {size_before / 1024 / 1024:.1f} → {size_after / 1024 / 1024:.1f} MB"
    )
    if not args.vacuum and delta_mb < 1.0 and affected > 100:
        print(
            "[prune] hint: pass --vacuum to compact and reclaim disk space "
            "(DuckDB marks rows as deleted but does not shrink the file "
            "until CHECKPOINT)",
            file=sys.stderr,
        )
    return 0
