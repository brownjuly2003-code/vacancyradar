"""Sync derived/slim_active.parquet → Neon Postgres `vacancies` table.

Strategy: full-snapshot upsert via temp staging table.
1. CREATE TEMP TABLE stage_vacancies (LIKE vacancies INCLUDING DEFAULTS) ON COMMIT PRESERVE ROWS
2. COPY parquet rows → stage in bounded batches, committing between batches
3. **Shrinkage guard** — abort if staged rows < (current * (1 - SHRINKAGE_THRESHOLD))
   or staged max(last_seen_at) < current max(last_seen_at). Truncated/stale parquet
   used to silently wipe production search rows (audit 2026-05-17 confirmed by
   CX+KM). Bypass with force=True / `--force`.
4. INSERT ... SELECT ... FROM stage ... ON CONFLICT (vacancy_id) DO UPDATE
5. DELETE FROM vacancies WHERE vacancy_id NOT IN (SELECT vacancy_id FROM stage)
6. COMMIT

Idempotent. Snapshot semantics match slim_active.parquet — vacancies that drop
out of the IT scope or get closed disappear from Neon on the next run.

Connection: NEON_DATABASE_URL from env (postgresql://...sslmode=require).

CLI: `vradar publish neon` (full sync) or `vradar publish neon --init` (apply
schema first, then sync). Add `--force` to bypass the shrinkage guard.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "neon_schema.sql"

# Shrinkage guard threshold: abort if staged row count drops by more than 5%
# of the current production count without explicit force. A truncated parquet
# producing significant row loss is almost always a publisher bug; legitimate
# IT-scope drift is gradual (we see ±2-3% week-over-week in observed corpus).
SHRINKAGE_THRESHOLD = 0.05
COPY_BATCH_SIZE = 10_000


class ShrinkageGuardError(RuntimeError):
    """Staged snapshot would shrink production beyond SHRINKAGE_THRESHOLD or
    regress last_seen_at. Likely a truncated or stale slim_active.parquet."""

COLUMNS = (
    "vacancy_id",
    "title",
    "employer_id",
    "employer_name",
    "salary_rub_min",
    "salary_rub_max",
    "salary_currency",
    "salary_disclosed",
    "city",
    "region",
    "remote_type",
    "seniority",
    "description_teaser",
    "skills",
    "source",
    "market_scope",
    "professional_role_id",
    "source_url",
    "first_seen_at",
    "last_seen_at",
    "posted_at",
)


def apply_schema(conn: psycopg.Connection) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(schema)
    conn.commit()


def _pg_array_literal(values: list[str] | None) -> str:
    if not values:
        return "{}"
    escaped = []
    for v in values:
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{s}"')
    return "{" + ",".join(escaped) + "}"


def _row_to_copy_record(row: dict[str, Any]) -> tuple[Any, ...]:
    skills = row.get("skills")
    if skills is None:
        skills_pg = "{}"
    elif isinstance(skills, list):
        skills_pg = _pg_array_literal([str(s) for s in skills])
    else:
        skills_pg = _pg_array_literal(list(skills))
    return (
        row["vacancy_id"],
        row.get("title") or "",
        row.get("employer_id"),
        row.get("employer_name"),
        row.get("salary_rub_min"),
        row.get("salary_rub_max"),
        row.get("salary_currency"),
        row.get("salary_disclosed"),
        row.get("city"),
        row.get("region"),
        row.get("remote_type") or "unknown",
        row.get("seniority") or "unknown",
        row.get("description_teaser"),
        skills_pg,
        row.get("source") or "hh",
        row.get("market_scope"),
        row.get("professional_role_id"),
        row.get("source_url"),
        row.get("first_seen_at"),
        row.get("last_seen_at"),
        row.get("posted_at"),
    )


def check_shrinkage_guard(
    *,
    staged_count: int,
    current_count: int,
    staged_max_seen: _dt.datetime | None,
    current_max_seen: _dt.datetime | None,
    threshold: float = SHRINKAGE_THRESHOLD,
    force: bool = False,
) -> None:
    """Raise ShrinkageGuardError if staged snapshot looks dangerous.

    Two checks (both bypassed by force=True):
    1. Row count: staged_count < current_count * (1 - threshold).
    2. Recency: staged_max_seen < current_max_seen (regressed timestamps).

    No-ops on first run (current_count == 0). When force is True logs a warning
    and returns. Pure: caller queries current/staged counts and passes them in.
    """
    if force:
        logger.warning(
            "shrinkage guard bypassed (force=True): staged=%d current=%d",
            staged_count,
            current_count,
        )
        return
    if current_count == 0:
        return

    min_allowed = int(current_count * (1.0 - threshold))
    if staged_count < min_allowed:
        loss = current_count - staged_count
        pct = 100.0 * loss / current_count
        raise ShrinkageGuardError(
            f"staged snapshot would shrink vacancies by {loss} rows "
            f"({pct:.1f}%, threshold {threshold * 100:.0f}%): "
            f"staged={staged_count} current={current_count}. "
            f"Likely truncated parquet. Bypass: --force"
        )

    if (
        current_max_seen is not None
        and staged_max_seen is not None
        and staged_max_seen < current_max_seen
    ):
        raise ShrinkageGuardError(
            f"staged max(last_seen_at)={staged_max_seen!s} regressed below "
            f"current max(last_seen_at)={current_max_seen!s}. "
            f"Likely stale parquet. Bypass: --force"
        )


def sync_parquet_to_neon(
    parquet_path: Path,
    database_url: str,
    *,
    init_schema: bool = False,
    dry: bool = False,
    force: bool = False,
    copy_batch_size: int = COPY_BATCH_SIZE,
    max_attempts: int = 3,
    backoff_base: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    if not parquet_path.exists():
        raise FileNotFoundError(f"missing parquet: {parquet_path}")

    df = pl.read_parquet(parquet_path)
    rows = df.height
    logger.info("loading %d rows from %s", rows, parquet_path)

    if dry:
        return {"rows_read": rows, "rows_upserted": 0, "rows_deleted": 0}

    # Neon serverless occasionally drops idle/long-lived COPY connections
    # (`server closed the connection unexpectedly` on flush). Retry the whole
    # transaction on OperationalError — COMMIT is at the end, so a dropped
    # connection rolls back cleanly and a fresh attempt redoes COPY+upsert.
    # ShrinkageGuardError + other exceptions propagate without retry.
    upserted = 0
    deleted = 0
    last_op_exc: psycopg.OperationalError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            upserted, deleted = _run_sync_transaction(
                df=df,
                database_url=database_url,
                init_schema=init_schema,
                force=force,
                copy_batch_size=copy_batch_size,
            )
            break
        except psycopg.OperationalError as exc:
            last_op_exc = exc
            if attempt >= max_attempts:
                logger.error(
                    "neon sync exhausted %d attempts: %s", max_attempts, exc
                )
                raise
            delay = backoff_base * (3 ** (attempt - 1))
            logger.warning(
                "neon sync attempt %d/%d failed: %s; retrying in %.1fs",
                attempt,
                max_attempts,
                exc,
                delay,
            )
            sleep(delay)
    else:  # pragma: no cover — defensive, loop always breaks or raises
        if last_op_exc is not None:
            raise last_op_exc

    logger.info(
        "neon sync done: rows_read=%d upserted=%d deleted=%d", rows, upserted, deleted
    )
    return {"rows_read": rows, "rows_upserted": upserted, "rows_deleted": deleted}


def _run_sync_transaction(
    *,
    df: pl.DataFrame,
    database_url: str,
    init_schema: bool,
    force: bool,
    copy_batch_size: int,
) -> tuple[int, int]:
    """Run batched COPY + shrinkage guard + upsert + delete.

    Returns (upserted, deleted). Raises ShrinkageGuardError on guard trip and
    psycopg.OperationalError on transient connection drops — caller decides
    whether to retry.
    """
    with psycopg.connect(database_url) as conn:
        if init_schema:
            apply_schema(conn)
            logger.info("schema applied")

        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE stage_vacancies (LIKE vacancies INCLUDING DEFAULTS) ON COMMIT PRESERVE ROWS"
            )
            conn.commit()

            cols_ident = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
            copy_sql = sql.SQL(
                "COPY stage_vacancies ({}) FROM STDIN WITH (FORMAT csv, NULL '\\N')"
            ).format(cols_ident)

            # Keep each COPY small enough for Neon serverless. A failed batch
            # still aborts the current attempt; caller retries the whole sync
            # on a fresh connection, with the temp stage table gone.
            for batch in df.iter_slices(n_rows=copy_batch_size):
                with cur.copy(copy_sql) as copy:
                    for row in batch.iter_rows(named=True):
                        rec = _row_to_copy_record(row)
                        csv_fields = []
                        for v in rec:
                            if v is None:
                                csv_fields.append("\\N")
                            elif isinstance(v, bool):
                                csv_fields.append("t" if v else "f")
                            else:
                                s = str(v).replace('"', '""')
                                if "," in s or '"' in s or "\n" in s or "\r" in s:
                                    csv_fields.append(f'"{s}"')
                                else:
                                    csv_fields.append(s)
                        copy.write(",".join(csv_fields) + "\n")
                conn.commit()

            # Shrinkage guard: compare staged snapshot to current production
            # state BEFORE the destructive DELETE. Truncated or stale parquet
            # would otherwise silently wipe live search rows.
            cur.execute("SELECT COUNT(*) FROM stage_vacancies")
            staged_count_row = cur.fetchone()
            staged_count = int(staged_count_row[0]) if staged_count_row else 0
            cur.execute("SELECT MAX(last_seen_at) FROM stage_vacancies")
            staged_max_row = cur.fetchone()
            staged_max_seen = staged_max_row[0] if staged_max_row else None

            cur.execute("SELECT COUNT(*), MAX(last_seen_at) FROM vacancies")
            current_row = cur.fetchone()
            if current_row is None:
                current_count, current_max_seen = 0, None
            else:
                current_count = int(current_row[0])
                current_max_seen = current_row[1]

            check_shrinkage_guard(
                staged_count=staged_count,
                current_count=current_count,
                staged_max_seen=staged_max_seen,
                current_max_seen=current_max_seen,
                force=force,
            )

            update_assignments = sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                for c in COLUMNS
                if c != "vacancy_id"
            )
            upsert_sql = sql.SQL(
                "INSERT INTO vacancies ({cols}) "
                "SELECT {cols} FROM stage_vacancies "
                "ON CONFLICT (vacancy_id) DO UPDATE SET {assigns}"
            ).format(cols=cols_ident, assigns=update_assignments)
            cur.execute(upsert_sql)
            upserted = cur.rowcount

            cur.execute(
                "DELETE FROM vacancies WHERE vacancy_id NOT IN (SELECT vacancy_id FROM stage_vacancies)"
            )
            deleted = cur.rowcount

        conn.commit()

    return upserted, deleted


def main(*, init: bool = False, dry: bool = False, force: bool = False) -> int:
    parquet_path = Path("derived/slim_active.parquet")
    database_url = os.environ.get("NEON_DATABASE_URL")
    if not database_url:
        logger.error("NEON_DATABASE_URL not set")
        return 1
    try:
        stats = sync_parquet_to_neon(
            parquet_path, database_url, init_schema=init, dry=dry, force=force
        )
    except ShrinkageGuardError as exc:
        logger.error("shrinkage guard aborted sync: %s", exc)
        return 4
    print(
        f"rows_read={stats['rows_read']} "
        f"upserted={stats['rows_upserted']} "
        f"deleted={stats['rows_deleted']}"
    )
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="apply schema before sync")
    parser.add_argument("--dry", action="store_true", help="load parquet, no DB writes")
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass shrinkage guard (use only after manual review of slim parquet)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main(init=args.init, dry=args.dry, force=args.force))
