"""Build slim/events_30d/ Hive-partitioned Parquet from master/events.duckdb.

Contract: docs/contracts/slim-events-v1.md (v1).

Layout:
  derived/slim_events_30d/year=YYYY/month=MM/day=DD/events.parquet

Sort within each file: ts ASC, type, vacancy_id (browser-friendly streaming).
employer_id is namespaced to "<source>:<id>" to match the v1 contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


SLIM_EVENTS_SCHEMA: dict[str, Any] = {
    "event_id": pl.String,
    "vacancy_id": pl.String,
    "employer_id": pl.String,
    "ts": pl.Datetime("us", "UTC"),
    "type": pl.String,
    "payload": pl.String,
    "source": pl.String,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def build_slim_events_30d(
    events_db: Path,
    *,
    now_utc: datetime | None = None,
) -> pl.DataFrame:
    """Read events.duckdb, return DataFrame for last 30 days, sorted, namespaced."""
    if not events_db.exists():
        return pl.DataFrame(schema=SLIM_EVENTS_SCHEMA)
    now = now_utc or _utc_now()
    cutoff = now - timedelta(days=30)
    con = duckdb.connect(str(events_db), read_only=True)
    try:
        arrow = con.execute(
            """
            SELECT event_id, vacancy_id, employer_id, ts, type, payload, source
            FROM events
            WHERE ts >= ?
            ORDER BY ts ASC, type, vacancy_id
            """,
            [cutoff.replace(tzinfo=None)],
        ).fetch_arrow_table()
    finally:
        con.close()
    pdf = pl.from_arrow(arrow)
    assert isinstance(pdf, pl.DataFrame)  # multi-column query → DataFrame, не Series
    if pdf.is_empty():
        return pl.DataFrame(schema=SLIM_EVENTS_SCHEMA)
    pdf = pdf.with_columns(
        pl.when(pl.col("employer_id").is_not_null())
        .then(pl.col("source") + pl.lit(":") + pl.col("employer_id"))
        .otherwise(None)
        .alias("employer_id"),
        pl.col("ts").dt.replace_time_zone("UTC"),
    )
    return pdf.select(list(SLIM_EVENTS_SCHEMA.keys())).cast(SLIM_EVENTS_SCHEMA)  # type: ignore[arg-type]


def write_slim_events_partitioned(df: pl.DataFrame, out_root: Path) -> list[Path]:
    """Write Hive-partitioned by year/month/day. Returns list of files written."""
    if df.is_empty():
        return []
    out_root.mkdir(parents=True, exist_ok=True)
    tagged = df.with_columns(
        pl.col("ts").dt.year().alias("_y"),
        pl.col("ts").dt.month().alias("_m"),
        pl.col("ts").dt.day().alias("_d"),
    )
    written: list[Path] = []
    for (y, m, d), part in tagged.group_by(["_y", "_m", "_d"], maintain_order=True):
        path = out_root / f"year={y}" / f"month={m:02d}" / f"day={d:02d}" / "events.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        part.drop(["_y", "_m", "_d"]).write_parquet(
            path, compression="zstd", compression_level=3
        )
        written.append(path)
    return written


def list_partition_uploads(out_root: Path) -> list[tuple[Path, str]]:
    """Map local partition paths → blob pathnames for upload.

    Returns list of (local_path, blob_pathname) where blob_pathname is the
    relative path under slim/events_30d/.
    """
    uploads: list[tuple[Path, str]] = []
    for p in sorted(out_root.rglob("events.parquet")):
        rel = p.relative_to(out_root).as_posix()
        uploads.append((p, f"slim/events_30d/{rel}"))
    return uploads
