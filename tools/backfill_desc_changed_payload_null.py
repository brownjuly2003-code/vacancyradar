"""One-time backfill: NULL out payload column for existing desc_changed events.

Run-once script for the 2026-05-18 (session 19) `desc_changed` payload drop.
Forward-going writes from `events_derivation._classify_change` already emit
NULL for the desc_changed payload (commit body explains the rationale: no
consumer reads the {prev_hash, new_hash} dict, ~28 MB / 430k rows of dead
storage in events.duckdb, +12 MB/month growth before this change).

After running, drop this file. The codebase has no on-going dependency.

Usage:
    D:/Python/Python312/python.exe -m tools.backfill_desc_changed_payload_null --dry
    D:/Python/Python312/python.exe -m tools.backfill_desc_changed_payload_null
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="master/events.duckdb", help="Path to events.duckdb")
    p.add_argument("--dry", action="store_true", help="Show counts, don't modify")
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[err] {db_path} not found", file=sys.stderr)
        return 2

    size_before = db_path.stat().st_size
    con = duckdb.connect(str(db_path), read_only=args.dry)
    try:
        n_affected = con.execute(
            "SELECT count(*) FROM events WHERE type='desc_changed' AND payload IS NOT NULL"
        ).fetchone()[0]
        payload_bytes = con.execute(
            "SELECT COALESCE(SUM(LENGTH(payload)), 0) FROM events "
            "WHERE type='desc_changed' AND payload IS NOT NULL"
        ).fetchone()[0]

        if args.dry:
            print(
                f"[dry] would NULL payload on {n_affected} desc_changed rows "
                f"({payload_bytes:,} bytes of JSON, ~{payload_bytes/1024/1024:.2f} MB)"
            )
            return 0

        if n_affected == 0:
            print("[done] nothing to backfill — all desc_changed rows already NULL")
            return 0

        print(
            f"[backfill] NULLing payload on {n_affected} desc_changed rows "
            f"({payload_bytes:,} bytes, ~{payload_bytes/1024/1024:.2f} MB of JSON)"
        )
        con.execute(
            "UPDATE events SET payload=NULL WHERE type='desc_changed' AND payload IS NOT NULL"
        )
        con.execute("CHECKPOINT")
    finally:
        con.close()

    if not args.dry:
        size_after = db_path.stat().st_size
        delta_mb = (size_before - size_after) / 1024 / 1024
        print(
            f"[backfill] events.duckdb: {size_before/1024/1024:.1f} MB → "
            f"{size_after/1024/1024:.1f} MB (reclaimed {delta_mb:.1f} MB)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
