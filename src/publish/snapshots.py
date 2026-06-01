"""Pre-aggregated JSON snapshots for /api/facets and /api/trends/*.

Cuts Vercel Blob egress ~400x: each cold-start currently reads the full
12 MB slim/active.parquet via DuckDB+httpfs just to compute aggregates
that don't change between daily refreshes. Replacing those queries with
a single fetch of a 20-50 KB JSON file makes the dashboard survive on
the Hobby plan free egress quota.

Output layout in Vercel Blob:
    slim/snapshots/facets.json
    slim/snapshots/trends/market_pulse.json
    slim/snapshots/trends/employer_top.json
    slim/snapshots/trends/skill_velocity.json
    slim/snapshots/trends/role_salary.json

Each snapshot mirrors the existing /api/<route> response body byte-for-byte
so the route handler is a thin pass-through (fetch + cache, no shape
translation). When a snapshot is missing the route falls back to the
DuckDB+httpfs path so we never have a hard outage during deploy.
"""
from __future__ import annotations

import datetime as _dt
import json
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import polars as pl


SENIORITY_VALUES: tuple[str, ...] = (
    "intern",
    "junior",
    "middle",
    "senior",
    "lead",
    "principal",
    "unknown",
)

REMOTE_VALUES: tuple[str, ...] = ("office", "hybrid", "remote", "unknown")

SOURCE_KEYS: tuple[str, ...] = ("hh", "telegram")

CITY_FACET_LIMIT = 50
EMPLOYER_FACET_LIMIT = 30
SKILL_FACET_LIMIT = 50

EMPLOYER_TOP_LIMIT = 25
SKILL_VELOCITY_LIMIT = 30
ROLE_SALARY_LIMIT = 200

# Bump when changing the on-the-wire shape of any snapshot payload
# (renaming fields, removing required keys, schema migrations). Routes
# compare against this version when reading Neon `aggregates` and skip
# rows that don't match, falling through to the next degradation layer.
# CX audit 2026-05-17 P2: aggregates without version → mid-deploy shape
# skew would serve stale payload to a route expecting the new shape.
CURRENT_AGGREGATE_SCHEMA_VERSION = 1


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _count_by_value(df: pl.DataFrame, column: str, limit: int | None = None) -> list[dict[str, Any]]:
    if df.is_empty() or column not in df.columns:
        return []
    series = (
        df.select(pl.col(column).alias("value"))
        .filter(pl.col("value").is_not_null() & (pl.col("value") != ""))
        .group_by("value")
        .agg(pl.len().alias("count"))
        .sort(by=["count", "value"], descending=[True, False])
    )
    if limit is not None:
        series = series.head(limit)
    return [
        {"value": str(row["value"]), "count": int(row["count"])}
        for row in series.to_dicts()
    ]


def _enum_counts(df: pl.DataFrame, column: str, allowed: Sequence[str]) -> list[dict[str, Any]]:
    """Counts for an enum column, exposing every allowed value (zero-filled)."""
    if column not in df.columns or df.is_empty():
        return [{"value": v, "count": 0} for v in allowed]
    real = (
        df.select(pl.col(column).alias("value"))
        .group_by("value")
        .agg(pl.len().alias("count"))
    )
    lookup = {str(row["value"]): int(row["count"]) for row in real.to_dicts()}
    return [{"value": v, "count": lookup.get(v, 0)} for v in allowed]


def _skill_counts(df: pl.DataFrame, limit: int) -> list[dict[str, Any]]:
    if df.is_empty() or "skills" not in df.columns:
        return []
    exploded = (
        df.select(pl.col("skills").alias("skill"))
        .explode("skill")
        .filter(pl.col("skill").is_not_null() & (pl.col("skill") != ""))
        .group_by("skill")
        .agg(pl.len().alias("count"))
        .sort(by=["count", "skill"], descending=[True, False])
        .head(limit)
    )
    return [
        {"value": str(row["skill"]), "count": int(row["count"])}
        for row in exploded.to_dicts()
    ]


def _source_breakdown(df: pl.DataFrame) -> dict[str, int]:
    if df.is_empty() or "source" not in df.columns:
        return {key: 0 for key in SOURCE_KEYS}
    counts = (
        df.group_by("source")
        .agg(pl.len().alias("count"))
        .to_dicts()
    )
    lookup = {str(row["source"]): int(row["count"]) for row in counts}
    return {key: lookup.get(key, 0) for key in SOURCE_KEYS}


def _latest_seen_at(df: pl.DataFrame) -> str | None:
    if df.is_empty() or "last_seen_at" not in df.columns:
        return None
    value = df.select(pl.col("last_seen_at").max()).item()
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return str(value)


def _salary_range(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"min": None, "max": None, "p50": None, "p90": None, "with_salary_pct": 0.0}
    salary_min = df.select(pl.col("salary_rub_min").min()).item()
    salary_max = df.select(pl.col("salary_rub_max").max()).item()
    p50 = df.select(pl.col("salary_rub_min").quantile(0.5, interpolation="linear")).item()
    p90 = df.select(pl.col("salary_rub_min").quantile(0.9, interpolation="linear")).item()
    total = df.height
    disclosed = (
        df.filter(pl.col("salary_disclosed") == True).height  # noqa: E712
        if "salary_disclosed" in df.columns
        else 0
    )
    with_salary_pct = (disclosed * 100.0 / total) if total > 0 else 0.0
    return {
        "min": int(salary_min) if salary_min is not None else None,
        "max": int(salary_max) if salary_max is not None else None,
        "p50": int(p50) if p50 is not None else None,
        "p90": int(p90) if p90 is not None else None,
        "with_salary_pct": float(with_salary_pct),
    }


def _count_distinct_non_null(df: pl.DataFrame, column: str) -> int:
    """count(DISTINCT col) semantics — NULL не учитывается (как в DuckDB)."""
    if df.is_empty() or column not in df.columns:
        return 0
    return int(df.filter(pl.col(column).is_not_null()).select(pl.col(column).n_unique()).item())


def build_facets_snapshot(slim: pl.DataFrame) -> dict[str, Any]:
    """Mirror /api/facets response shape — single fetch replaces 8 DuckDB queries."""
    source_facet = _count_by_value(slim, "source")
    return {
        "summary": {
            "total_vacancies": slim.height,
            "unique_cities": _count_distinct_non_null(slim, "city"),
            "unique_employers": _count_distinct_non_null(slim, "employer_name"),
            "unique_skills": _unique_skill_count(slim),
            "latest_seen_at": _latest_seen_at(slim),
            "source_breakdown": _source_breakdown(slim),
        },
        "facets": {
            "city": _count_by_value(slim, "city", CITY_FACET_LIMIT),
            "employer_name": _count_by_value(slim, "employer_name", EMPLOYER_FACET_LIMIT),
            "remote_type": _enum_counts(slim, "remote_type", REMOTE_VALUES),
            "seniority": _enum_counts(slim, "seniority", SENIORITY_VALUES),
            "source": source_facet,
            "skills": _skill_counts(slim, SKILL_FACET_LIMIT),
            "salary_range": _salary_range(slim),
        },
        "refreshed_at": _utc_now_iso(),
    }


def _unique_skill_count(df: pl.DataFrame) -> int:
    if df.is_empty() or "skills" not in df.columns:
        return 0
    return int(
        df.select(pl.col("skills"))
        .explode("skills")
        .filter(pl.col("skills").is_not_null() & (pl.col("skills") != ""))
        .select(pl.col("skills").n_unique())
        .item()
    )


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    return str(value)


def _int_or_none(v: Any) -> int | None:
    return int(v) if v is not None else None


def _float_or_none(v: Any) -> float | None:
    return float(v) if v is not None else None


def build_market_pulse_snapshot(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"rows": [], "refreshed_at": _utc_now_iso()}
    df = df.sort("date")
    rows = [
        {
            "date": _fmt_date(row["date"]),
            "total_active": _int_or_none(row["total_active"]),
            "new_vacancies": _int_or_none(row["new_vacancies"]),
            "closed_vacancies": _int_or_none(row["closed_vacancies"]),
            "salary_disclosure_rate": _float_or_none(row["salary_disclosure_rate"]),
            "median_active_age_days": _float_or_none(row["median_active_age_days"]),
        }
        for row in df.to_dicts()
    ]
    return {"rows": rows, "refreshed_at": _utc_now_iso()}


def _latest_week_filter(df: pl.DataFrame) -> pl.DataFrame:
    if df.is_empty() or "week_start" not in df.columns:
        return df
    latest = df.select(pl.col("week_start").max()).item()
    return df.filter(pl.col("week_start") == latest)


def build_employer_top_snapshot(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"rows": [], "refreshed_at": _utc_now_iso()}
    latest = _latest_week_filter(df).sort("new_vacancies", descending=True).head(EMPLOYER_TOP_LIMIT)
    rows = [
        {
            "week_start": _fmt_date(row["week_start"]),
            "employer_id": row.get("employer_id"),
            "employer_name": row.get("employer_name"),
            "new_vacancies": _int_or_none(row["new_vacancies"]),
            "closed_vacancies": _int_or_none(row["closed_vacancies"]),
            "disclosure_rate": _float_or_none(row["disclosure_rate"]),
        }
        for row in latest.to_dicts()
    ]
    return {"rows": rows, "refreshed_at": _utc_now_iso()}


def build_skill_velocity_snapshot(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"rows": [], "refreshed_at": _utc_now_iso()}
    latest = (
        _latest_week_filter(df)
        .sort("rank_this_week")
        .head(SKILL_VELOCITY_LIMIT)
    )
    rows = [
        {
            "week_start": _fmt_date(row["week_start"]),
            "skill": row.get("skill"),
            "mentions_this_week": _int_or_none(row["mentions_this_week"]),
            "mentions_prev_week": _int_or_none(row["mentions_prev_week"]),
            "delta_pct": _float_or_none(row["delta_pct"]),
            "rank_this_week": _int_or_none(row["rank_this_week"]),
        }
        for row in latest.to_dicts()
    ]
    return {"rows": rows, "refreshed_at": _utc_now_iso()}


def build_role_salary_snapshot(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"rows": [], "refreshed_at": _utc_now_iso()}
    national = (
        df.filter(pl.col("city").is_null())
        .sort(by=["week_start", "salary_rub_median"], descending=[True, True])
        .head(ROLE_SALARY_LIMIT)
    )
    rows = [
        {
            "week_start": _fmt_date(row["week_start"]),
            "role_canonical": row.get("role_canonical"),
            "seniority": row.get("seniority"),
            "city": None,
            "n_vacancies": _int_or_none(row["n_vacancies"]),
            "salary_rub_p25": _int_or_none(row["salary_rub_p25"]),
            "salary_rub_median": _int_or_none(row["salary_rub_median"]),
            "salary_rub_p75": _int_or_none(row["salary_rub_p75"]),
        }
        for row in national.to_dicts()
    ]
    return {"rows": rows, "refreshed_at": _utc_now_iso()}


def write_snapshot(payload: dict[str, Any], out_path: Path) -> int:
    """Write JSON payload, return file size in bytes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    out_path.write_text(text, encoding="utf-8")
    return out_path.stat().st_size


def build_snapshots(
    slim: pl.DataFrame,
    weekly: dict[str, pl.DataFrame],
    out_dir: Path,
) -> dict[str, Path]:
    """Build every snapshot, write to out_dir, return {logical_name: path}."""
    written: dict[str, Path] = {}

    facets_path = out_dir / "snapshots" / "facets.json"
    write_snapshot(build_facets_snapshot(slim), facets_path)
    written["facets"] = facets_path

    trend_builders: dict[str, tuple[str, Callable[[pl.DataFrame], dict[str, Any]]]] = {
        "market_pulse": ("weekly_market_pulse", build_market_pulse_snapshot),
        "employer_top": ("weekly_employer_top", build_employer_top_snapshot),
        "skill_velocity": ("weekly_skill_velocity", build_skill_velocity_snapshot),
        "role_salary": ("weekly_role_salary", build_role_salary_snapshot),
    }
    for name, (weekly_key, builder) in trend_builders.items():
        df = weekly.get(weekly_key, pl.DataFrame())
        path = out_dir / "snapshots" / "trends" / f"{name}.json"
        write_snapshot(builder(df), path)
        written[f"trends/{name}"] = path

    return written


def iter_blob_paths(
    local_dir: Path,
    blob_prefix: str = "slim/snapshots",
) -> Iterable[tuple[Path, str]]:
    """Walk local snapshot dir, yield (local_path, blob_pathname) pairs.

    `local_dir` is the directory containing `facets.json` and `trends/*.json`.
    `blob_prefix` is prepended to every relative path, default `slim/snapshots`.
    """
    prefix = blob_prefix.strip("/")
    for path in sorted(local_dir.rglob("*.json")):
        rel = path.relative_to(local_dir).as_posix()
        yield path, f"{prefix}/{rel}" if prefix else rel
