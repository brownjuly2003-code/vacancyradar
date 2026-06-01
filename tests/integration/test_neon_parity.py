"""DuckDB vs Neon /api/search parity.

For each of 12 representative query shapes, run the same logical query against
both backends and assert that:
  - total row count matches exactly
  - the first `top_k` vacancy_ids (in result order) match

The DuckDB side reads `derived/slim_active.parquet` directly to mirror what
the live route does via httpfs over Vercel Blob — the data is the same file.

Skipped when NEON_DATABASE_URL is not set (CI / pre-provision environments).
"""
from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("NEON_DATABASE_URL"),
    reason="NEON_DATABASE_URL not set",
)

psycopg = pytest.importorskip("psycopg")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SLIM = PROJECT_ROOT / "derived" / "slim_active.parquet"
TOP_K = 50


def _duckdb_query(where_clauses: list[str], params: list, *, order_by: str, score_expr: str, limit: int = TOP_K) -> tuple[int, list[str]]:
    """Run a DuckDB query against the local slim parquet."""
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    con = duckdb.connect(":memory:")
    try:
        count_sql = f"""
            SELECT COUNT(*)::BIGINT FROM read_parquet(?)
            {where_sql}
        """
        total = con.execute(count_sql, [str(SLIM), *params]).fetchone()[0]

        data_sql = f"""
            SELECT vacancy_id, {score_expr} AS score
            FROM read_parquet(?)
            {where_sql}
            {order_by}
            LIMIT {limit}
        """
        ids = [r[0] for r in con.execute(data_sql, [str(SLIM), *params]).fetchall()]
        return int(total), ids
    finally:
        con.close()


def _to_pg(sql: str) -> str:
    """Translate DuckDB-flavoured SQL to Postgres.

    - `%` literals → `%%` (psycopg client-side parameter parser is greedy on %)
    - `?` placeholders → `%s` (psycopg style — done after %% escape)
    - `list_contains(skills, %s)` → `skills @> ARRAY[%s]::text[]`
    """
    import re

    s = sql.replace("%", "%%")
    s = s.replace("?", "%s")
    s = re.sub(
        r"list_contains\(skills,\s*%s\)",
        "skills @> ARRAY[%s]::text[]",
        s,
    )
    return s


def _neon_query(where_clauses: list[str], params: list, *, order_by: str, score_expr: str, limit: int = TOP_K) -> tuple[int, list[str]]:
    """Run a Postgres query against the Neon vacancies table."""
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    where_pg = _to_pg(where_sql)
    score_pg = _to_pg(score_expr)

    count_sql = f"SELECT COUNT(*)::int FROM vacancies {where_pg}"
    data_sql = f"""
        SELECT vacancy_id, {score_pg} AS score
        FROM vacancies
        {where_pg}
        {order_by}
        LIMIT {limit}
    """

    url = os.environ["NEON_DATABASE_URL"]
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()[0]
        cur.execute(data_sql, params)
        ids = [r[0] for r in cur.fetchall()]
    return int(total), ids


# Parity cases. Each is (case_id, where_clauses, params, order_by, score_expr).
# DuckDB and Neon receive the SAME logical filter, just translated for dialect.
# Score expression matches what the route builds:
#   - q empty → NULL score, ORDER BY last_seen_at DESC
#   - q present → (3 if title hit) + (1 if teaser hit), ORDER BY score DESC, last_seen_at DESC

ORDER_RECENCY = "ORDER BY last_seen_at DESC, first_seen_at DESC, vacancy_id DESC"
ORDER_SCORE = "ORDER BY score DESC NULLS LAST, last_seen_at DESC, first_seen_at DESC, vacancy_id DESC"

PARITY_CASES = [
    # Default: source=hh, no q.
    pytest.param(
        ["source IN (?)"], ["hh"], ORDER_RECENCY, "NULL",
        id="empty_default_hh",
    ),
    # Free-text: q=python (just lowercase ASCII, no synonym expansion needed for SQL).
    pytest.param(
        ["source IN (?)", "((title ILIKE ?) OR (description_teaser ILIKE ?))"],
        ["hh", "%python%", "%python%"],
        ORDER_SCORE,
        "(CASE WHEN title ILIKE '%python%' THEN 3 ELSE 0 END) + (CASE WHEN description_teaser ILIKE '%python%' THEN 1 ELSE 0 END)",
        id="q_python",
    ),
    # Cyrillic single-token. Note: synonym expansion is done in route JS BEFORE
    # the SQL — for parity we test the SQL-after-expansion, so feeding the
    # term verbatim is fine. The route uses skill-synonyms.json for expansion.
    pytest.param(
        ["source IN (?)", "((title ILIKE ?) OR (description_teaser ILIKE ?))"],
        ["hh", "%аналитик%", "%аналитик%"],
        ORDER_SCORE,
        "(CASE WHEN title ILIKE '%аналитик%' THEN 3 ELSE 0 END) + (CASE WHEN description_teaser ILIKE '%аналитик%' THEN 1 ELSE 0 END)",
        id="q_cyrillic_analyst",
    ),
    # Multi-word: phrase semantics, no expansion. ILIKE on the full phrase.
    pytest.param(
        ["source IN (?)", "((title ILIKE ?) OR (description_teaser ILIKE ?))"],
        ["hh", "%senior python%", "%senior python%"],
        ORDER_SCORE,
        "(CASE WHEN title ILIKE '%senior python%' THEN 3 ELSE 0 END) + (CASE WHEN description_teaser ILIKE '%senior python%' THEN 1 ELSE 0 END)",
        id="q_multi_word",
    ),
    # City filter.
    pytest.param(
        ["source IN (?)", "city IN (?)"], ["hh", "Москва"], ORDER_RECENCY, "NULL",
        id="city_moscow",
    ),
    # Employer filter.
    pytest.param(
        ["source IN (?)", "employer_name IN (?)"], ["hh", "Яндекс"], ORDER_RECENCY, "NULL",
        id="employer_yandex",
    ),
    # Salary lower bound.
    pytest.param(
        ["source IN (?)", "salary_rub_min >= ?"], ["hh", 200_000], ORDER_RECENCY, "NULL",
        id="salary_min_200k",
    ),
    # Seniority filter.
    pytest.param(
        ["source IN (?)", "seniority IN (?)"], ["hh", "senior"], ORDER_RECENCY, "NULL",
        id="seniority_senior",
    ),
    # Remote type filter.
    pytest.param(
        ["source IN (?)", "remote_type IN (?)"], ["hh", "remote"], ORDER_RECENCY, "NULL",
        id="remote_remote",
    ),
    # Source: hh (explicit).
    pytest.param(
        ["source IN (?)"], ["hh"], ORDER_RECENCY, "NULL",
        id="source_hh_explicit",
    ),
    # Source: telegram only.
    pytest.param(
        ["source IN (?)"], ["telegram"], ORDER_RECENCY, "NULL",
        id="source_telegram",
    ),
    # Skill array membership (Postgres skills @> ARRAY[?]).
    pytest.param(
        ["source IN (?)", "list_contains(skills, ?)"], ["hh", "Python"], ORDER_RECENCY, "NULL",
        id="skill_python",
    ),
]


@pytest.mark.parametrize("where_clauses,params,order_by,score_expr", PARITY_CASES)
def test_neon_parity(where_clauses: list[str], params: list, order_by: str, score_expr: str) -> None:
    duckdb_total, duckdb_ids = _duckdb_query(where_clauses, params, order_by=order_by, score_expr=score_expr)
    neon_total, neon_ids = _neon_query(where_clauses, params, order_by=order_by, score_expr=score_expr)

    assert neon_total == duckdb_total, (
        f"total mismatch: duckdb={duckdb_total} neon={neon_total}"
    )

    # With vacancy_id DESC as stable tie-breaker, ordering should match exactly.
    assert neon_ids == duckdb_ids, (
        f"top-{TOP_K} order mismatch\n  duckdb={duckdb_ids[:5]}...\n  neon={neon_ids[:5]}..."
    )
