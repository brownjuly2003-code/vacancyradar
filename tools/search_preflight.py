"""Preflight estimator for /api/search backend migration.

Measures derived/slim_active.parquet against:
- Upstash Redis Free (256 MB data, 500k monthly commands, ~10 GB/mo bandwidth)
- Neon Postgres Free (0.5 GB storage)

Output: .tmp/search-preflight/report.json + report.md with decision.

Run:
    D:/Python/Python312/python.exe -m tools.search_preflight
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent.parent
SLIM = ROOT / "derived" / "slim_active.parquet"
OUT_DIR = ROOT / ".tmp" / "search-preflight"

UPSTASH_DATA_BYTES = 256 * 1024 * 1024
UPSTASH_MONTHLY_COMMANDS = 500_000
UPSTASH_MONTHLY_BANDWIDTH = 10 * 1024 * 1024 * 1024

NEON_STORAGE_BYTES = 512 * 1024 * 1024

ASSUMED_MONTHLY_SEARCHES = 10_000
ASSUMED_DAILY_REFRESHES = 30
TOKEN_MIN_LEN = 3
PG_INDEX_OVERHEAD_FACTOR = 1.6

TOKEN_SPLIT = re.compile(r"[^\wЀ-ӿ]+", re.UNICODE)


@dataclass
class Report:
    rows: int
    parquet_bytes: int
    compact_row_avg_bytes: float
    compact_row_p95_bytes: int
    compact_row_total_bytes: int
    unique_word_tokens: int
    avg_postings_per_token: float
    total_word_postings: int
    facet_summary: dict
    skill_postings_total: int
    redis_hash_bytes: int
    redis_inverted_index_bytes: int
    redis_total_bytes: int
    redis_refresh_commands: int
    redis_monthly_search_commands: int
    redis_monthly_bandwidth: int
    pg_table_bytes: int
    pg_indexes_bytes: int
    pg_total_bytes: int
    quotas: dict
    fit: dict
    decision: str
    rationale: list[str]


def _compact_row_bytes(row: dict) -> int:
    payload = {
        "vacancy_id": row.get("vacancy_id"),
        "title": row.get("title"),
        "employer_name": row.get("employer_name"),
        "salary_rub_min": row.get("salary_rub_min"),
        "salary_rub_max": row.get("salary_rub_max"),
        "salary_currency": row.get("salary_currency"),
        "city": row.get("city"),
        "region": row.get("region"),
        "remote_type": row.get("remote_type"),
        "seniority": row.get("seniority"),
        "description_teaser": row.get("description_teaser"),
        "skills": list(row.get("skills") or []),
        "source": row.get("source"),
        "source_url": row.get("source_url"),
        "posted_at": str(row.get("posted_at")) if row.get("posted_at") else None,
        "first_seen_at": str(row.get("first_seen_at")) if row.get("first_seen_at") else None,
        "last_seen_at": str(row.get("last_seen_at")) if row.get("last_seen_at") else None,
    }
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    tokens = TOKEN_SPLIT.split(text.lower())
    return {t for t in tokens if len(t) >= TOKEN_MIN_LEN}


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    values_sorted = sorted(values)
    k = int(len(values_sorted) * pct)
    return values_sorted[min(k, len(values_sorted) - 1)]


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def measure(df: pl.DataFrame) -> Report:
    rows = df.height
    compact_sizes: list[int] = []
    word_token_postings: dict[str, int] = {}
    facets: dict[str, dict[str, int]] = {
        "city": {},
        "employer_name": {},
        "remote_type": {},
        "seniority": {},
        "source": {},
    }
    skill_postings_total = 0

    for r in df.iter_rows(named=True):
        compact_sizes.append(_compact_row_bytes(r))

        tokens = _tokenize(r.get("title")) | _tokenize(r.get("description_teaser"))
        for t in tokens:
            word_token_postings[t] = word_token_postings.get(t, 0) + 1

        for facet in facets:
            v = r.get(facet)
            if v:
                facets[facet][v] = facets[facet].get(v, 0) + 1

        for s in (r.get("skills") or []):
            skill_postings_total += 1

    total_compact = sum(compact_sizes)
    avg_compact = total_compact / rows
    p95_compact = _percentile(compact_sizes, 0.95)

    unique_tokens = len(word_token_postings)
    total_postings = sum(word_token_postings.values())
    avg_postings = total_postings / unique_tokens if unique_tokens else 0

    facet_summary = {
        name: {
            "unique_values": len(values),
            "total_postings": sum(values.values()),
        }
        for name, values in facets.items()
    }

    redis_set_overhead_per_member = 60
    redis_hash_overhead_per_row = 200

    redis_hash_bytes = total_compact + rows * redis_hash_overhead_per_row

    facet_postings_sum = sum(s["total_postings"] for s in facet_summary.values())
    redis_inverted_index_bytes = (
        total_postings * redis_set_overhead_per_member
        + facet_postings_sum * redis_set_overhead_per_member
        + skill_postings_total * redis_set_overhead_per_member
    )

    redis_total_bytes = redis_hash_bytes + redis_inverted_index_bytes

    redis_refresh_commands = (
        rows
        + total_postings
        + facet_postings_sum
        + skill_postings_total
    )

    redis_monthly_search_commands = ASSUMED_MONTHLY_SEARCHES * 5
    redis_monthly_bandwidth = ASSUMED_MONTHLY_SEARCHES * int(avg_compact) * 50

    pg_table_bytes = total_compact
    pg_indexes_bytes = int(total_compact * PG_INDEX_OVERHEAD_FACTOR)
    pg_total_bytes = pg_table_bytes + pg_indexes_bytes

    quotas = {
        "upstash_data": UPSTASH_DATA_BYTES,
        "upstash_monthly_commands": UPSTASH_MONTHLY_COMMANDS,
        "upstash_monthly_bandwidth": UPSTASH_MONTHLY_BANDWIDTH,
        "neon_storage": NEON_STORAGE_BYTES,
    }

    monthly_refresh_commands = redis_refresh_commands * ASSUMED_DAILY_REFRESHES
    total_monthly_redis_commands = monthly_refresh_commands + redis_monthly_search_commands

    fit = {
        "redis_data_pct": round(redis_total_bytes / UPSTASH_DATA_BYTES * 100, 1),
        "redis_monthly_commands_pct": round(total_monthly_redis_commands / UPSTASH_MONTHLY_COMMANDS * 100, 1),
        "redis_monthly_bandwidth_pct": round(redis_monthly_bandwidth / UPSTASH_MONTHLY_BANDWIDTH * 100, 1),
        "neon_storage_pct": round(pg_total_bytes / NEON_STORAGE_BYTES * 100, 1),
        "monthly_refresh_commands": monthly_refresh_commands,
        "monthly_search_commands": redis_monthly_search_commands,
    }

    rationale: list[str] = []

    redis_data_ok = redis_total_bytes < int(UPSTASH_DATA_BYTES * 0.7)
    redis_commands_ok = total_monthly_redis_commands < int(UPSTASH_MONTHLY_COMMANDS * 0.7)
    redis_bandwidth_ok = redis_monthly_bandwidth < int(UPSTASH_MONTHLY_BANDWIDTH * 0.7)
    redis_substring_ok = False

    neon_storage_ok = pg_total_bytes < int(NEON_STORAGE_BYTES * 0.7)

    rationale.append(
        f"Redis data {_format_bytes(redis_total_bytes)} = {fit['redis_data_pct']}% of 256 MB free tier "
        f"({'OK' if redis_data_ok else 'TIGHT'})"
    )
    rationale.append(
        f"Redis monthly commands ~{total_monthly_redis_commands:,} = {fit['redis_monthly_commands_pct']}% "
        f"of 500k free tier ({'OK' if redis_commands_ok else 'TIGHT'})"
    )
    rationale.append(
        f"Redis monthly bandwidth ~{_format_bytes(redis_monthly_bandwidth)} = {fit['redis_monthly_bandwidth_pct']}% "
        f"of 10 GB free tier ({'OK' if redis_bandwidth_ok else 'TIGHT'})"
    )
    rationale.append(
        "Redis cannot do ILIKE %substring% — would need word-level inverted index (loses substring match like "
        "'питон' → 'пайтон-разработчик') OR trigram inverted index (3-5x data size)."
    )
    rationale.append(
        f"Neon storage {_format_bytes(pg_total_bytes)} = {fit['neon_storage_pct']}% of 512 MB free tier "
        f"({'OK' if neon_storage_ok else 'TIGHT'})"
    )
    rationale.append(
        "Neon Postgres natively supports ILIKE via pg_trgm GIN index — preserves /api/search semantics "
        "(title+teaser substring) byte-for-byte."
    )

    if redis_data_ok and redis_commands_ok and redis_bandwidth_ok and redis_substring_ok:
        decision = "upstash_redis"
    elif neon_storage_ok:
        decision = "neon_postgres"
    elif redis_data_ok and redis_commands_ok and redis_bandwidth_ok:
        decision = "neon_postgres"
        rationale.append(
            "Decision: Neon, not Redis — Redis quotas fit but cannot preserve ILIKE substring semantics."
        )
    else:
        decision = "keep_duckdb"
        rationale.append("Both backends exceed comfort — keep DuckDB+httpfs and rely on storage mirror.")

    return Report(
        rows=rows,
        parquet_bytes=SLIM.stat().st_size,
        compact_row_avg_bytes=round(avg_compact, 1),
        compact_row_p95_bytes=p95_compact,
        compact_row_total_bytes=total_compact,
        unique_word_tokens=unique_tokens,
        avg_postings_per_token=round(avg_postings, 2),
        total_word_postings=total_postings,
        facet_summary=facet_summary,
        skill_postings_total=skill_postings_total,
        redis_hash_bytes=redis_hash_bytes,
        redis_inverted_index_bytes=redis_inverted_index_bytes,
        redis_total_bytes=redis_total_bytes,
        redis_refresh_commands=redis_refresh_commands,
        redis_monthly_search_commands=redis_monthly_search_commands,
        redis_monthly_bandwidth=redis_monthly_bandwidth,
        pg_table_bytes=pg_table_bytes,
        pg_indexes_bytes=pg_indexes_bytes,
        pg_total_bytes=pg_total_bytes,
        quotas=quotas,
        fit=fit,
        decision=decision,
        rationale=rationale,
    )


def render_markdown(rep: Report) -> str:
    facets_md = "\n".join(
        f"| {name} | {s['unique_values']:,} | {s['total_postings']:,} |"
        for name, s in rep.facet_summary.items()
    )
    rationale_md = "\n".join(f"- {line}" for line in rep.rationale)
    return f"""# /api/search preflight estimator

**Source:** `derived/slim_active.parquet`
**Rows:** {rep.rows:,}
**Parquet on disk:** {_format_bytes(rep.parquet_bytes)}

## Row payload

| metric | value |
|---|---:|
| compact row JSON avg | {rep.compact_row_avg_bytes:.0f} B |
| compact row JSON p95 | {rep.compact_row_p95_bytes:,} B |
| total compact JSON | {_format_bytes(rep.compact_row_total_bytes)} |

## Token inverted index (title + description_teaser, len≥{TOKEN_MIN_LEN}, word-level)

| metric | value |
|---|---:|
| unique word tokens | {rep.unique_word_tokens:,} |
| total word postings | {rep.total_word_postings:,} |
| avg postings / token | {rep.avg_postings_per_token} |

## Facet postings

| facet | unique values | total postings |
|---|---:|---:|
{facets_md}
| skills (LIST) | — | {rep.skill_postings_total:,} |

## Upstash Redis Free fit

| budget | usage | quota | % |
|---|---:|---:|---:|
| data | {_format_bytes(rep.redis_total_bytes)} | 256 MB | {rep.fit['redis_data_pct']}% |
| monthly commands | {rep.fit['monthly_refresh_commands'] + rep.fit['monthly_search_commands']:,} | 500,000 | {rep.fit['redis_monthly_commands_pct']}% |
| monthly bandwidth | {_format_bytes(rep.redis_monthly_bandwidth)} | 10 GB | {rep.fit['redis_monthly_bandwidth_pct']}% |

Refresh assumption: {ASSUMED_DAILY_REFRESHES}/mo full rebuilds × ({rep.rows:,} HSET + {rep.total_word_postings:,} word SADD + facet SADD + skill SADD) = {rep.fit['monthly_refresh_commands']:,} commands/mo.

Search assumption: {ASSUMED_MONTHLY_SEARCHES:,}/mo searches × 5 commands each ({rep.fit['monthly_search_commands']:,}/mo) + payload bandwidth ({_format_bytes(rep.redis_monthly_bandwidth)}/mo @ 50-row pages).

## Neon Postgres Free fit

| budget | usage | quota | % |
|---|---:|---:|---:|
| storage (table + indexes) | {_format_bytes(rep.pg_total_bytes)} | 512 MB | {rep.fit['neon_storage_pct']}% |

Index overhead factor: {PG_INDEX_OVERHEAD_FACTOR}× table (pg_trgm GIN on title+teaser + BTREE on city/employer/remote/seniority/source/salary + GIN on skills + BTREE on last_seen_at).

## Rationale

{rationale_md}

## Decision

**`{rep.decision}`**

| Result | Backend |
|---|---|
| Redis estimate <180 MB data AND refresh <150k commands AND substring search preserved | Upstash Redis |
| Redis quotas fit but substring search not preserved | Neon Postgres |
| Redis storage >180 MB OR commands >150k | Neon Postgres |
| Neon storage >358 MB | Keep DuckDB |

## Caveats

- ILIKE substring search is the make-or-break constraint. Redis Free does not bundle RediSearch FT modules on Upstash — only word-level inverted index is feasible there, which loses partial-substring recall.
- Redis storage estimate uses 60 B per posting (Upstash compresses SET members on the wire but billable counts are post-compression).
- Neon index overhead factor {PG_INDEX_OVERHEAD_FACTOR}× is conservative — actual ratio for text-heavy tables with pg_trgm is typically 1.3–1.6.
- Bandwidth estimate assumes 50-row pages × {ASSUMED_MONTHLY_SEARCHES:,} searches/mo. Real traffic to vradar-six.vercel.app is currently lower; this is a comfort upper bound.
- Refresh frequency = 1/day. Faster refresh (e.g. hourly) multiplies command count linearly.

Generated by `tools/search_preflight.py`.
"""


def main() -> int:
    if not SLIM.exists():
        print(f"missing {SLIM}", file=sys.stderr)
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = pl.read_parquet(SLIM)
    rep = measure(df)

    json_path = OUT_DIR / "report.json"
    md_path = OUT_DIR / "report.md"
    json_path.write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(rep), encoding="utf-8")

    print(f"rows: {rep.rows:,}")
    print(f"compact total: {_format_bytes(rep.compact_row_total_bytes)}")
    print(f"redis total: {_format_bytes(rep.redis_total_bytes)} ({rep.fit['redis_data_pct']}% of 256 MB)")
    print(f"neon total:  {_format_bytes(rep.pg_total_bytes)} ({rep.fit['neon_storage_pct']}% of 512 MB)")
    print(f"decision: {rep.decision}")
    print(f"report: {md_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
