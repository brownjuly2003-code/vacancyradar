"""Build agg/weekly_*.parquet от events.duckdb + slim_active.parquet.

Контракт: docs/contracts/weekly-aggregates-v1.md (v1).

4 артефакта:
  - weekly_role_salary  — медианы по role × seniority × week × city
  - weekly_skill_velocity — top movers скиллов (% delta WoW)
  - weekly_employer_top — top hirers (new/closed per week)
  - weekly_market_pulse — daily counts (window: 90 дней)

Публичный entrypoint — `build_all_weekly(...)` + `write_weekly_aggregates(...)`.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import polars as pl


WEEKLY_ROLE_SALARY_SCHEMA: dict[str, Any] = {
    "week_start": pl.Date,
    "role_canonical": pl.String,
    "seniority": pl.String,
    "city": pl.String,
    "n_vacancies": pl.Int64,
    "salary_rub_p25": pl.Int64,
    "salary_rub_median": pl.Int64,
    "salary_rub_p75": pl.Int64,
}

WEEKLY_SKILL_VELOCITY_SCHEMA: dict[str, Any] = {
    "week_start": pl.Date,
    "skill": pl.String,
    "mentions_this_week": pl.Int64,
    "mentions_prev_week": pl.Int64,
    "delta_pct": pl.Float64,
    "rank_this_week": pl.Int32,
}

WEEKLY_EMPLOYER_TOP_SCHEMA: dict[str, Any] = {
    "week_start": pl.Date,
    "employer_id": pl.String,
    "employer_name": pl.String,
    "new_vacancies": pl.Int64,
    "closed_vacancies": pl.Int64,
    "disclosure_rate": pl.Float64,
    "median_time_to_close_days": pl.Float64,
}

WEEKLY_MARKET_PULSE_SCHEMA: dict[str, Any] = {
    "date": pl.Date,
    "total_active": pl.Int64,
    "new_vacancies": pl.Int64,
    "closed_vacancies": pl.Int64,
    "salary_disclosure_rate": pl.Float64,
    "median_active_age_days": pl.Float64,
}

DAILY_MARKET_COUNTS_SCHEMA: dict[str, Any] = {
    "date": pl.Date,
    "new_vacancies": pl.Int64,
    "closed_vacancies": pl.Int64,
}


# Простая role-classification по title — rule-based для MVP.
# Order важен: первый match выигрывает (специфичные раньше общих).
_ROLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(data\s*scientist|дата\s*сайентист)\b", re.IGNORECASE), "data_scientist"),
    (re.compile(r"\b(data\s*analyst|дата\s*аналитик|аналитик\s*данных)\b", re.IGNORECASE), "data_analyst"),
    (re.compile(r"\b(data\s*engineer|инженер\s*данных|дата\s*инженер)\b", re.IGNORECASE), "data_engineer"),
    (re.compile(r"\b(ml\s*engineer|machine\s*learning|мл\s*инженер|ml-?разраб)\b", re.IGNORECASE), "ml_engineer"),
    (re.compile(r"\b(devops|сре|sre|platform\s*engineer)\b", re.IGNORECASE), "devops"),
    (re.compile(r"\b(qa|тестировщик|test\s*engineer|sdet)\b", re.IGNORECASE), "qa_engineer"),
    (re.compile(r"\b(frontend|фронт[- ]?енд|front[- ]?end|фронтенд)\b", re.IGNORECASE), "frontend_engineer"),
    (re.compile(r"\b(backend|бэк[- ]?енд|back[- ]?end|бэкенд|серверн)\b", re.IGNORECASE), "backend_engineer"),
    (re.compile(r"\b(fullstack|full[- ]?stack|фулл[- ]?стек)\b", re.IGNORECASE), "fullstack_engineer"),
    (re.compile(r"\b(mobile|ios|android|мобильн)\b", re.IGNORECASE), "mobile_engineer"),
    (re.compile(r"\b(product\s*manager|продукт\s*менеджер|product\s*owner|пм)\b", re.IGNORECASE), "product_manager"),
    (re.compile(r"\b(project\s*manager|руководитель\s*проект)\b", re.IGNORECASE), "project_manager"),
    (re.compile(r"\b(designer|дизайнер|ux|ui)\b", re.IGNORECASE), "designer"),
    (re.compile(r"\b(analyst|аналитик)\b", re.IGNORECASE), "analyst"),
    (re.compile(r"\b(developer|разработчик|программист|engineer)\b", re.IGNORECASE), "engineer"),
)


def parse_role(title: str | None) -> str:
    """Title → canonical role. 'other' для непокрытых (продавец, водитель...)."""
    if not title:
        return "other"
    for pattern, role in _ROLE_PATTERNS:
        if pattern.search(title):
            return role
    return "other"


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ============================================================================
# market_pulse
# ============================================================================

def build_weekly_market_pulse(
    events_db: Path,
    slim_active: pl.DataFrame,
    *,
    today: date | None = None,
    window_days: int = 90,
) -> pl.DataFrame:
    """Daily counts за последние `window_days` дней.

    new — по `posted_at` из slim_active, чтобы backfill/initial crawl не
    выглядели как рыночный приток; closed — из events.duckdb. total_active/
    disclosure/age — из slim_active на сегодняшнюю дату (daily history требует
    daily snapshots в lake, для MVP показываем today-only metric с null для
    прошлых дней).
    """
    today = today or _utc_today()
    cutoff = today - timedelta(days=window_days)

    daily_counts = _daily_market_counts(events_db, slim_active, cutoff, today)
    if daily_counts.is_empty():
        return pl.DataFrame(schema=WEEKLY_MARKET_PULSE_SCHEMA)

    if slim_active.is_empty():
        total_active = 0
        disclosure_rate = 0.0
        median_age = 0.0
    else:
        total_active = slim_active.height
        # disclosure_rate = «вакансии с используемой зарплатой»:
        # `salary_disclosed=True` AND хотя бы одна из granica salary_rub_*
        # не null. Outlier-policy [10k, 5M] обнуляет crazy values (→ None),
        # оставляя salary_disclosed=True как индикатор employer-side
        # transparency. Раньше rate считался по голому флагу — был выше,
        # чем доля вакансий реально участвующих в salary quantiles
        # (KM audit 2026-05-17 P2).
        disclosed = slim_active.filter(
            pl.col("salary_disclosed")
            & (pl.col("salary_rub_min").is_not_null() | pl.col("salary_rub_max").is_not_null())
        ).height
        disclosure_rate = float(disclosed) / total_active if total_active else 0.0
        ages = (
            slim_active.with_columns(
                ((pl.lit(today) - pl.col("first_seen_at").cast(pl.Date)).dt.total_days())
                .alias("_age_days")
            )["_age_days"]
        )
        median = ages.median() if not ages.is_empty() else None
        median_age = float(median) if isinstance(median, (int, float)) else 0.0

    return daily_counts.with_columns(
        pl.when(pl.col("date") == today).then(total_active).otherwise(None).cast(pl.Int64).alias("total_active"),
        pl.when(pl.col("date") == today).then(disclosure_rate).otherwise(None).cast(pl.Float64).alias("salary_disclosure_rate"),
        pl.when(pl.col("date") == today).then(median_age).otherwise(None).cast(pl.Float64).alias("median_active_age_days"),
    ).select(list(WEEKLY_MARKET_PULSE_SCHEMA.keys())).cast(WEEKLY_MARKET_PULSE_SCHEMA)  # type: ignore[arg-type]


def _daily_market_counts(
    events_db: Path,
    slim_active: pl.DataFrame,
    cutoff: date,
    today: date,
) -> pl.DataFrame:
    posted = _daily_posted_counts(slim_active, cutoff, today)
    closed = _daily_closed_counts(events_db, cutoff, today)
    today_row = pl.DataFrame(
        [{"date": today, "new_vacancies": 0, "closed_vacancies": 0}],
        schema=DAILY_MARKET_COUNTS_SCHEMA,
        orient="row",
    )
    parts = [posted, closed]
    if not slim_active.is_empty():
        parts.append(today_row)
    counts = pl.concat(parts)
    if counts.is_empty():
        return pl.DataFrame(schema=DAILY_MARKET_COUNTS_SCHEMA)
    return (
        counts.group_by("date")
        .agg(
            pl.col("new_vacancies").sum(),
            pl.col("closed_vacancies").sum(),
        )
        .sort("date")
        .cast(DAILY_MARKET_COUNTS_SCHEMA)  # type: ignore[arg-type]
    )


def _daily_posted_counts(slim_active: pl.DataFrame, cutoff: date, today: date) -> pl.DataFrame:
    """Count vacancies by market publication date, not ingestion/backfill date."""
    if slim_active.is_empty() or "posted_at" not in slim_active.columns:
        return pl.DataFrame(schema=DAILY_MARKET_COUNTS_SCHEMA)
    df = (
        slim_active.lazy()
        .filter(pl.col("posted_at").is_not_null())
        .with_columns(pl.col("posted_at").cast(pl.Date).alias("date"))
        .filter((pl.col("date") >= cutoff) & (pl.col("date") <= today))
        .group_by("date")
        .agg(pl.len().cast(pl.Int64).alias("new_vacancies"))
        .with_columns(pl.lit(0).cast(pl.Int64).alias("closed_vacancies"))
        .select(list(DAILY_MARKET_COUNTS_SCHEMA.keys()))
        .collect()
    )
    if df.is_empty():
        return pl.DataFrame(schema=DAILY_MARKET_COUNTS_SCHEMA)
    return df.cast(DAILY_MARKET_COUNTS_SCHEMA)  # type: ignore[arg-type]


def _daily_closed_counts(events_db: Path, cutoff: date, today: date) -> pl.DataFrame:
    """SELECT day, count(closed) AS closed FROM events GROUP BY day."""
    if not events_db.exists():
        return pl.DataFrame(schema=DAILY_MARKET_COUNTS_SCHEMA)
    con = duckdb.connect(str(events_db), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT
                CAST(ts AS DATE) AS day,
                COUNT(*) AS closed_vacancies
            FROM events
            WHERE ts >= ? AND ts < ?
              AND type='closed'
            GROUP BY day
            ORDER BY day
            """,
            [cutoff, today + timedelta(days=1)],
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return pl.DataFrame(schema=DAILY_MARKET_COUNTS_SCHEMA)
    df = pl.DataFrame(
        rows,
        schema={"date": pl.Date, "closed_vacancies": pl.Int64},
        orient="row",
    )
    return (
        df.with_columns(pl.lit(0).cast(pl.Int64).alias("new_vacancies"))
        .select(list(DAILY_MARKET_COUNTS_SCHEMA.keys()))
        .cast(DAILY_MARKET_COUNTS_SCHEMA)  # type: ignore[arg-type]
    )


# ============================================================================
# employer_top
# ============================================================================

def build_weekly_employer_top(
    events_db: Path,
    slim_active: pl.DataFrame,
    *,
    today: date | None = None,
    window_weeks: int = 12,
    top_n_per_week: int = 50,
) -> pl.DataFrame:
    """Top hirers по неделям. Disclosure rate берётся из slim_active текущего."""
    today = today or _utc_today()
    cutoff_date = _monday_of(today) - timedelta(weeks=window_weeks)
    cutoff = datetime.combine(cutoff_date, datetime.min.time())

    if not events_db.exists():
        return pl.DataFrame(schema=WEEKLY_EMPLOYER_TOP_SCHEMA)

    con = duckdb.connect(str(events_db), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT
                date_trunc('week', ts) AS week_start,
                employer_id,
                SUM(CASE WHEN type='appeared' THEN 1 ELSE 0 END) AS new_vacancies,
                SUM(CASE WHEN type='closed' THEN 1 ELSE 0 END) AS closed_vacancies
            FROM events
            WHERE ts >= ? AND employer_id IS NOT NULL
            GROUP BY week_start, employer_id
            HAVING SUM(CASE WHEN type='appeared' THEN 1 ELSE 0 END) > 0
                OR SUM(CASE WHEN type='closed' THEN 1 ELSE 0 END) > 0
            """,
            [cutoff],
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return pl.DataFrame(schema=WEEKLY_EMPLOYER_TOP_SCHEMA)

    normalized_rows = [
        (week_start.date() if isinstance(week_start, datetime) else week_start, *rest)
        for week_start, *rest in rows
    ]
    df = pl.DataFrame(
        normalized_rows,
        schema={
            "week_start": pl.Date,
            "employer_id_raw": pl.String,
            "new_vacancies": pl.Int64,
            "closed_vacancies": pl.Int64,
        },
        orient="row",
    )

    # employer_id в events.duckdb хранится bare (без префикса), а в slim_active —
    # как `<source>:<id>`. Нормализуем для join.
    df = df.with_columns(
        (pl.lit("hh:") + pl.col("employer_id_raw")).alias("employer_id"),
    )

    if slim_active.is_empty():
        df = df.with_columns(
            pl.lit(None).cast(pl.String).alias("employer_name"),
            pl.lit(0.0).cast(pl.Float64).alias("disclosure_rate"),
        )
    else:
        # disclosure_rate per employer — % vacancies с используемой
        # зарплатой (salary_disclosed AND salary_rub_*  не null).
        # Outlier-policy [10k, 5M] нулит crazy values, оставляя
        # disclosed=True для employer-transparency. Без AND-условия rate
        # завышен (KM audit 2026-05-17 P2).
        emp_stats = (
            slim_active.with_columns(
                (
                    pl.col("salary_disclosed")
                    & (
                        pl.col("salary_rub_min").is_not_null()
                        | pl.col("salary_rub_max").is_not_null()
                    )
                ).alias("_salary_usable")
            )
            .group_by("employer_id")
            .agg(
                pl.col("employer_name").first(),
                pl.col("_salary_usable").mean().cast(pl.Float64).alias("disclosure_rate"),
            )
        )
        df = df.join(emp_stats, on="employer_id", how="left").with_columns(
            pl.col("disclosure_rate").fill_null(0.0),
        )

    df = df.with_columns(
        pl.lit(None).cast(pl.Float64).alias("median_time_to_close_days"),
    )

    df = (
        df.sort(["week_start", "new_vacancies"], descending=[False, True])
        .group_by("week_start", maintain_order=True)
        .head(top_n_per_week)
    )

    return df.select(list(WEEKLY_EMPLOYER_TOP_SCHEMA.keys())).cast(WEEKLY_EMPLOYER_TOP_SCHEMA)  # type: ignore[arg-type]


# ============================================================================
# skill_velocity
# ============================================================================

def build_weekly_skill_velocity(
    slim_active: pl.DataFrame,
    *,
    today: date | None = None,
    min_mentions: int = 1,
) -> pl.DataFrame:
    """skill × week → mentions count + WoW delta_pct + rank.

    Источник недели: `first_seen_at` каждой вакансии (week когда вакансия
    появилась). slim_active нужен с непустыми skills и first_seen_at.
    """
    today = today or _utc_today()
    if slim_active.is_empty() or "skills" not in slim_active.columns:
        return pl.DataFrame(schema=WEEKLY_SKILL_VELOCITY_SCHEMA)

    exploded = (
        slim_active.lazy()
        .filter(pl.col("skills").list.len() > 0)
        .filter(pl.col("first_seen_at").is_not_null())
        .with_columns(
            pl.col("first_seen_at").cast(pl.Date).alias("_date"),
        )
        .with_columns(
            (pl.col("_date") - pl.duration(days=pl.col("_date").dt.weekday() - 1))
            .cast(pl.Date)
            .alias("week_start"),
        )
        .explode("skills")
        .rename({"skills": "skill"})
        .filter(pl.col("skill").is_not_null())
        .group_by(["week_start", "skill"])
        .agg(pl.len().alias("mentions_this_week"))
        .filter(pl.col("mentions_this_week") >= min_mentions)
        .sort(["skill", "week_start"])
        .collect()
    )

    if exploded.is_empty():
        return pl.DataFrame(schema=WEEKLY_SKILL_VELOCITY_SCHEMA)

    with_lag = exploded.with_columns(
        pl.col("mentions_this_week")
        .shift(1)
        .over("skill")
        .fill_null(0)
        .cast(pl.Int64)
        .alias("mentions_prev_week"),
    ).with_columns(
        pl.when(pl.col("mentions_prev_week") == 0)
        .then(None)
        .otherwise(
            ((pl.col("mentions_this_week") - pl.col("mentions_prev_week")).cast(pl.Float64)
             / pl.col("mentions_prev_week") * 100.0)
        )
        .alias("delta_pct"),
    )

    ranked = with_lag.with_columns(
        pl.col("mentions_this_week")
        .rank(method="ordinal", descending=True)
        .over("week_start")
        .cast(pl.Int32)
        .alias("rank_this_week"),
    )

    return ranked.select(list(WEEKLY_SKILL_VELOCITY_SCHEMA.keys())).cast(WEEKLY_SKILL_VELOCITY_SCHEMA)  # type: ignore[arg-type]


# ============================================================================
# role_salary
# ============================================================================

def build_weekly_role_salary(
    slim_active: pl.DataFrame,
    *,
    today: date | None = None,
    min_sample: int = 3,
) -> pl.DataFrame:
    """Median salary по role × seniority × week × city. Окно — все недели в slim."""
    today = today or _utc_today()
    if slim_active.is_empty():
        return pl.DataFrame(schema=WEEKLY_ROLE_SALARY_SCHEMA)

    # role из title (rule-based parser), week из first_seen_at, salary через
    # midpoint(min, max) — fallback на min/max если другой None.
    base = (
        slim_active.lazy()
        .filter(pl.col("first_seen_at").is_not_null())
        .filter(pl.col("salary_rub_min").is_not_null() | pl.col("salary_rub_max").is_not_null())
        .with_columns(
            pl.col("title")
            .map_elements(parse_role, return_dtype=pl.String)
            .alias("role_canonical"),
            pl.col("first_seen_at").cast(pl.Date).alias("_date"),
        )
        .with_columns(
            (pl.col("_date") - pl.duration(days=pl.col("_date").dt.weekday() - 1))
            .cast(pl.Date)
            .alias("week_start"),
            pl.coalesce([
                ((pl.col("salary_rub_min") + pl.col("salary_rub_max")) / 2).cast(pl.Int64),
                pl.col("salary_rub_min"),
                pl.col("salary_rub_max"),
            ]).alias("_salary"),
        )
    )

    group_cols = ["week_start", "role_canonical", "seniority"]
    salary_aggs = [
        pl.len().alias("n_vacancies"),
        pl.col("_salary").quantile(0.25).cast(pl.Int64).alias("salary_rub_p25"),
        pl.col("_salary").quantile(0.5).cast(pl.Int64).alias("salary_rub_median"),
        pl.col("_salary").quantile(0.75).cast(pl.Int64).alias("salary_rub_p75"),
    ]

    national = (
        base.group_by(group_cols)
        .agg(
            salary_aggs,
        )
        .filter(pl.col("n_vacancies") >= min_sample)
        .with_columns(pl.lit(None).cast(pl.String).alias("city"))
        .select(list(WEEKLY_ROLE_SALARY_SCHEMA.keys()))
    )

    city_level = (
        base.filter(pl.col("city").is_not_null() & (pl.col("city") != ""))
        .group_by([*group_cols, "city"])
        .agg(
            salary_aggs,
        )
        .filter(pl.col("n_vacancies") >= min_sample)
        .select(list(WEEKLY_ROLE_SALARY_SCHEMA.keys()))
    )

    grouped = (
        pl.concat([national, city_level])
        .sort(["week_start", "role_canonical", "seniority", "city"])
        .collect()
    )

    if grouped.is_empty():
        return pl.DataFrame(schema=WEEKLY_ROLE_SALARY_SCHEMA)

    return grouped.select(list(WEEKLY_ROLE_SALARY_SCHEMA.keys())).cast(WEEKLY_ROLE_SALARY_SCHEMA)  # type: ignore[arg-type]


# ============================================================================
# Public entrypoint + writer
# ============================================================================

def build_all_weekly(
    events_db: Path,
    slim_active: pl.DataFrame,
    *,
    today: date | None = None,
) -> dict[str, pl.DataFrame]:
    return {
        "weekly_market_pulse": build_weekly_market_pulse(events_db, slim_active, today=today),
        "weekly_employer_top": build_weekly_employer_top(events_db, slim_active, today=today),
        "weekly_skill_velocity": build_weekly_skill_velocity(slim_active, today=today),
        "weekly_role_salary": build_weekly_role_salary(slim_active, today=today),
    }


def write_weekly_aggregates(
    aggregates: dict[str, pl.DataFrame],
    out_dir: Path,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, df in aggregates.items():
        path = out_dir / f"{name}.parquet"
        df.write_parquet(path, compression="zstd", compression_level=3)
        written.append(path)
    return written
