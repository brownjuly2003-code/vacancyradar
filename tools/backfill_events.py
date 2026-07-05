"""One-shot backfill: derive events for vacancy_ids that exist in raw lake but
not yet in master/events.duckdb. Closes gap from HH crawler / TG ingest которые
не вызывали events_derivation per-batch.

Чисто idempotent: только vacancy_ids БЕЗ записей в events.duckdb. Reruns —
no-op после первого прохода.

Stream через DuckDB scan_parquet (по vacancy_id partition), Polars для diff.
Source field derive из vacancy_id префикса (hh: / tg:).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import duckdb
import polars as pl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.transform.events_derivation import (  # noqa: E402
    _classify_change,
    _events_schema,
    _make_event,
    append_events,
)

RAW_GLOB = PROJECT_ROOT / "master" / "vacancies_raw.parquet" / "**" / "*.parquet"
EVENTS_DB = PROJECT_ROOT / "master" / "events.duckdb"
BATCH_SIZE = 10_000  # vacancies per batch (controls peak memory)


def _source_from_id(vid: str) -> str:
    if vid.startswith("tg:"):
        return "tg"
    return "hh"


def main() -> int:
    print(f"[backfill] raw glob: {RAW_GLOB}")
    print(f"[backfill] events db: {EVENTS_DB}")

    # 1. Existing vacancy_ids в events
    ec = duckdb.connect(str(EVENTS_DB), read_only=True)
    have_ids = {r[0] for r in ec.execute("SELECT DISTINCT vacancy_id FROM events").fetchall()}
    ec.close()
    print(f"[backfill] existing events cover {len(have_ids):,} vacancy_ids")

    # 2. All vacancy_ids в raw lake (id + count snapshots)
    rc = duckdb.connect(":memory:")
    rc.execute("INSTALL httpfs; LOAD httpfs;")
    all_ids_df = rc.execute(
        f"""
        SELECT vacancy_id, COUNT(*) AS n_snapshots, MIN(fetched_at) AS first_seen
        FROM read_parquet('{RAW_GLOB.as_posix()}', hive_partitioning=true)
        GROUP BY vacancy_id
        """
    ).pl()
    print(f"[backfill] raw lake distinct vacancy_ids: {all_ids_df.height:,}")

    todo_df = all_ids_df.filter(~pl.col("vacancy_id").is_in(list(have_ids))).sort("first_seen")
    todo_ids = todo_df["vacancy_id"].to_list()
    print(f"[backfill] backfill todo: {len(todo_ids):,} vacancy_ids")
    if not todo_ids:
        print("[backfill] no work — events fully cover raw lake")
        return 0

    total_emitted = 0
    t0 = time.time()
    n_batches = (len(todo_ids) + BATCH_SIZE - 1) // BATCH_SIZE

    for batch_i in range(n_batches):
        batch_ids = todo_ids[batch_i * BATCH_SIZE : (batch_i + 1) * BATCH_SIZE]
        # Read snapshots для этой batch'и
        # DuckDB IN list для 10k IDs работает, но через VALUES table быстрее
        ids_table = pl.DataFrame({"vacancy_id": batch_ids})
        rc.register("batch_ids", ids_table.to_arrow())
        snaps_df = rc.execute(
            f"""
            SELECT s.vacancy_id, s.employer_id, s.content_hash, s.raw_json,
                   s.fetched_at, s.source
            FROM read_parquet('{RAW_GLOB.as_posix()}', hive_partitioning=true) s
            JOIN batch_ids b USING (vacancy_id)
            ORDER BY s.vacancy_id, s.fetched_at
            """
        ).pl()
        rc.unregister("batch_ids")

        # Iterate per-vacancy, derive events
        events: list[dict] = []
        cur_vid = None
        cur_snaps: list[dict] = []

        def flush():
            nonlocal events
            if not cur_snaps:
                return
            vid = cur_snaps[0]["vacancy_id"]
            src = _source_from_id(vid)
            employer_id = cur_snaps[0].get("employer_id")
            first_ts = cur_snaps[0]["fetched_at"]
            ev = _make_event("appeared", vid, employer_id, first_ts, source=src)
            events.append(ev)
            for prev, curr in zip(cur_snaps, cur_snaps[1:]):
                if prev["content_hash"] == curr["content_hash"]:
                    continue
                event_type, payload = _classify_change(prev, curr)
                ev = _make_event(
                    event_type,
                    vid,
                    curr.get("employer_id") or employer_id,
                    curr["fetched_at"],
                    payload=payload,
                    source=src,
                )
                events.append(ev)

        for r in snaps_df.iter_rows(named=True):
            if r["vacancy_id"] != cur_vid:
                flush()
                cur_snaps = []
                cur_vid = r["vacancy_id"]
            cur_snaps.append(r)
        flush()

        if events:
            evdf = pl.DataFrame(events, schema=_events_schema())
            n_written = append_events(evdf, EVENTS_DB)
            total_emitted += n_written
        elapsed = time.time() - t0
        rate = (batch_i + 1) / elapsed if elapsed else 0
        eta = (n_batches - batch_i - 1) / rate if rate else 0
        print(
            f"[backfill] batch {batch_i+1}/{n_batches} "
            f"({len(batch_ids)} vids, +{len(events) if events else 0} events) "
            f"total={total_emitted:,} "
            f"elapsed={elapsed:.0f}s eta={eta:.0f}s",
            flush=True,
        )

    rc.close()
    print(f"[backfill] DONE: +{total_emitted:,} events for {len(todo_ids):,} vacancies in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
