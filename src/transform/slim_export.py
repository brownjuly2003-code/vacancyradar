"""Build slim/active.parquet from master/vacancies_raw.parquet/.

Contract: docs/contracts/slim-active-v1.md (v1).

Enrichment-heavy fields (skills) populated в Phase 5+;
salary_rub_min/max — Phase 5: ЦБ rate normalisation (см. extract_salary_rub).
Full FTS-text (`description_fts`) is read from the hh_details cache during
build solely to feed `skills_match.extract_skills` — it is **not** persisted
to the slim parquet anymore (dropped 2026-05-16 to halve Blob egress; Turso
FTS5 path is dead, /api/search reads only title+teaser).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from src.config import load_settings
from src.enrich.region import region_for_city
from src.enrich.salary_norm import extract_salary_rub
from src.enrich.skills_match import extract_skills
from src.ingest.cbr_rates import load_rates_for, utc_today
from src.ingest.hh_detail import read_details_cache
from src.ingest.tg_parse import parse_remote_type, parse_seniority
from src.transform.dedup import (
    DEFAULT_THRESHOLD,
    DuplicatePair,
    VacancyForDedup,
    find_duplicates,
)


CBR_RATES_PATH = Path("master/ref/cbr_rates.parquet")


def _salary_bounds() -> tuple[int, int]:
    """Read outlier thresholds from config.yaml (cached via load_settings)."""
    settings = load_settings()
    return settings.salary.outlier_floor, settings.salary.outlier_ceiling

# Salary outlier thresholds (monthly RUB) — defaults are hardcoded constants
# pinned to historical values, but Settings.salary overrides at runtime. Anything
# outside [floor, ceiling] is treated as data error: typos, daily/hourly rates
# parsed as monthly, marketing placeholders like 10_000_000. Justification:
#   - p10 of full IT corpus = 3 000 (junk: salary=1, salary=150 — typos)
#   - p99 of full IT corpus = 7 466 539 (junk: 10_000_000 TG placeholders)
#   - real C-level RU IT salaries (CEO/CTO at Yandex/Tinkoff) top out ~3.5–4M/mo,
#     so 5M is a generous ceiling that still drops obvious garbage.
# Outliers are nulled (not clamped) so they don't pollute aggregate medians/p90,
# AND don't poison salary_disclosed (we keep that flag as-is — disclosure is
# about employer transparency, not value sanity).
SALARY_OUTLIER_FLOOR = 10_000
SALARY_OUTLIER_CEILING = 5_000_000


def _clamp_salary_outliers(
    salary_min: int | None,
    salary_max: int | None,
    floor: int | None = None,
    ceiling: int | None = None,
) -> tuple[int | None, int | None]:
    """Null any salary value that falls outside [floor, ceiling] RUB/month.

    Bound applied to each side independently (min and max) — keeps the half
    that's plausible if the other is broken. Returns (None, None) when both
    bounds are out-of-range. When floor/ceiling are None, reads from config.yaml
    `salary:` section via cached `load_settings()`; explicit args override.
    """
    if floor is None or ceiling is None:
        cfg_floor, cfg_ceiling = _salary_bounds()
        floor = floor if floor is not None else cfg_floor
        ceiling = ceiling if ceiling is not None else cfg_ceiling

    def _bounded(value: int | None) -> int | None:
        if value is None:
            return None
        if value < floor or value > ceiling:
            return None
        return value

    return _bounded(salary_min), _bounded(salary_max)
HH_DETAILS_PATH = Path("master/hh_details.parquet")


SLIM_ACTIVE_SCHEMA: dict[str, Any] = {
    "vacancy_id": pl.String,
    "title": pl.String,
    "employer_id": pl.String,
    "employer_name": pl.String,
    "salary_rub_min": pl.Int64,
    "salary_rub_max": pl.Int64,
    "salary_currency": pl.String,
    "salary_disclosed": pl.Boolean,
    "city": pl.String,
    "region": pl.String,
    "remote_type": pl.String,
    "seniority": pl.String,
    "description_teaser": pl.String,
    "skills": pl.List(pl.String),
    "source": pl.String,
    "market_scope": pl.String,
    "professional_role_id": pl.Int64,
    "source_url": pl.String,
    "first_seen_at": pl.Datetime("us", "UTC"),
    "last_seen_at": pl.Datetime("us", "UTC"),
    "posted_at": pl.Datetime("us", "UTC"),
}


_HH_WORK_FORMAT_TO_REMOTE = {
    "REMOTE": "remote",
    "HYBRID": "hybrid",
    "MIXED": "hybrid",
    "ON_SITE": "office",
    "FIELD_WORK": "office",
    "OFFICE": "office",
}

# api.hh.ru schedule.id values (mapped to slim contract enum). Unknown ids → "unknown".
_HH_API_SCHEDULE_TO_REMOTE = {
    "remote": "remote",
    "fullDay": "office",
    "shift": "office",
    "flyInFlyOut": "office",
    "flexible": "office",
}


def _is_shards_shape(item: dict) -> bool:
    """Distinguish shards (hh.ru/shards/vacancy/search) from api.hh.ru shape.

    Shards: vacancyId, company, compensation, links, workFormats.
    API:    id, employer, salary, alternate_url, schedule.
    """
    return "vacancyId" in item or "compensation" in item or "workFormats" in item


# === shards-shape mappers ===

def _hh_remote_type(item: dict) -> str:
    """Derive remote_type from hh shards workFormats. Returns 'unknown' on miss."""
    fmts = item.get("workFormats") or []
    for entry in fmts:
        elements = (entry or {}).get("workFormatsElement") or []
        for el in elements:
            if isinstance(el, str) and el in _HH_WORK_FORMAT_TO_REMOTE:
                return _HH_WORK_FORMAT_TO_REMOTE[el]
    return "unknown"


def _hh_employer_name(item: dict) -> str | None:
    company = item.get("company") or {}
    return company.get("visibleName") or company.get("name")


def _hh_salary(item: dict) -> tuple[str | None, bool]:
    comp = item.get("compensation") or {}
    currency = comp.get("currencyCode")
    disclosed = bool(comp.get("from") or comp.get("to"))
    return currency, disclosed


def _hh_city(item: dict) -> str | None:
    addr = item.get("address") or {}
    if addr.get("city"):
        return addr["city"]
    area = item.get("area") or {}
    return area.get("name")


def _hh_source_url(item: dict) -> str | None:
    links = item.get("links") or {}
    return links.get("desktop") or links.get("mobile")


# === api.hh.ru-shape mappers ===

def _hh_api_remote_type(item: dict) -> str:
    schedule_id = (item.get("schedule") or {}).get("id")
    if not isinstance(schedule_id, str):
        return "unknown"
    return _HH_API_SCHEDULE_TO_REMOTE.get(schedule_id, "unknown")


def _hh_api_employer_name(item: dict) -> str | None:
    return (item.get("employer") or {}).get("name")


def _hh_api_salary(item: dict) -> tuple[str | None, bool]:
    sal = item.get("salary") or {}
    currency = sal.get("currency")
    disclosed = bool(sal.get("from") or sal.get("to"))
    return currency, disclosed


def _hh_api_city(item: dict) -> str | None:
    return (item.get("area") or {}).get("name")


def _hh_api_source_url(item: dict) -> str | None:
    return item.get("alternate_url")


def _hh_to_slim_row(
    record: dict,
    rates: dict[str, float],
    details: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict:
    """One lake record (with raw_json + agg fields) → one slim row.

    Dispatches on raw_json shape: shards (hh.ru/shards) vs api (api.hh.ru).
    Both shapes carry the same source='hh' but have divergent JSON keys.
    """
    item = json.loads(record["raw_json"])
    if _is_shards_shape(item):
        title = item.get("name") or ""
        employer_name = _hh_employer_name(item)
        currency, disclosed = _hh_salary(item)
        city = _hh_city(item)
        remote_type = _hh_remote_type(item)
        source_url = _hh_source_url(item)
    else:
        title = item.get("name") or ""
        employer_name = _hh_api_employer_name(item)
        currency, disclosed = _hh_api_salary(item)
        city = _hh_api_city(item)
        remote_type = _hh_api_remote_type(item)
        source_url = _hh_api_source_url(item)

    salary_rub_min, salary_rub_max = extract_salary_rub(item, rates)
    salary_rub_min, salary_rub_max = _clamp_salary_outliers(salary_rub_min, salary_rub_max)
    # Disclosed-consistency: clamp может обнулить обе границы (under-floor /
    # over-ceiling currency conversion). Если факт раскрытия не подтверждается
    # числом в slim — сбрасываем флаг (session 44).
    disclosed = disclosed and (salary_rub_min is not None or salary_rub_max is not None)

    details = details or {}
    teaser, fts = details.get(record["vacancy_id"], (None, None))

    enrich_text = " ".join(filter(None, [title, teaser]))
    # Title-priority seniority (session 30): position-markers
    # (Ведущий/Руководитель/Помощник) fire ТОЛЬКО на title — в body они
    # обычно ссылаются на нанимающего, не на роль. Typed tokens
    # (Senior/Middle/Lead) и experience-years scанятся в body как fallback.
    seniority_body = " ".join(filter(None, [teaser, fts]))
    seniority = parse_seniority(title or "", body=seniority_body)
    if remote_type == "unknown":
        remote_type = parse_remote_type(enrich_text)

    skills_text = " ".join(filter(None, [title, teaser, fts]))
    skills = extract_skills(skills_text)
    region = region_for_city(city)

    raw_employer_id = record.get("employer_id")
    namespaced_employer_id = (
        f"{record['source']}:{raw_employer_id}" if raw_employer_id else None
    )
    return {
        "vacancy_id": record["vacancy_id"],
        "title": title,
        "employer_id": namespaced_employer_id,
        "employer_name": employer_name,
        "salary_rub_min": salary_rub_min,
        "salary_rub_max": salary_rub_max,
        "salary_currency": currency,
        "salary_disclosed": disclosed,
        "city": city,
        "region": region,
        "remote_type": remote_type,
        "seniority": seniority,
        "description_teaser": teaser,
        "skills": skills,
        "source": record["source"],
        "market_scope": record.get("market_scope"),
        "professional_role_id": record.get("professional_role_id"),
        "source_url": source_url,
        "first_seen_at": record["first_seen_at"],
        "last_seen_at": record["last_seen_at"],
        "posted_at": record["posted_at"],
    }


def build_slim_active(
    lake_root: Path,
    *,
    limit: int | None = None,
    market_scope: str | None = None,
    rates: dict[str, float] | None = None,
    rates_path: Path = CBR_RATES_PATH,
    details: dict[str, tuple[str | None, str | None]] | None = None,
    details_path: Path = HH_DETAILS_PATH,
    active_window_days: int | None = None,
    now_utc: Any | None = None,
) -> pl.DataFrame:
    """Read raw lake, derive per-vacancy slim active snapshot.

    limit: cap active rows before JSON mapping/skill enrichment.
    rates: explicit rate map для тестов. Если None — читается из rates_path.
    details: explicit detail map vacancy_id → (teaser, fts). Если None —
    читается из details_path. Если файла нет — все detail-поля NULL.
    active_window_days: если задано, оставить только vacancies где
    last_seen_at >= now_utc - N days. None = no filter (legacy behavior).
    Закрывает P0 продуктовый риск: "active" должно означать active, а не
    accumulated corpus (см. audit_codex_11_05_26.md §4 P0.1).
    """
    if next(lake_root.rglob("*.parquet"), None) is None:
        return pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)
    if limit is not None and limit <= 0:
        return pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)
    if market_scope is not None and not _lake_has_column(lake_root, "market_scope"):
        return pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)

    if rates is None:
        rates = load_rates_for(rates_path, utc_today())

    if details is None:
        cache_df = read_details_cache(details_path)
        if cache_df.is_empty():
            details = {}
        else:
            details = {
                row["vacancy_id"]: (row["description_teaser"], row["description_fts"])
                for row in cache_df.iter_rows(named=True)
            }

    import duckdb

    duckdb_tmp = Path(".tmp") / "duckdb"
    duckdb_tmp.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as con:
        con.execute("SET TimeZone='UTC'")
        con.execute("SET memory_limit='4GB'")
        con.execute("SET threads=2")
        con.execute("SET preserve_insertion_order=false")
        con.execute("SET temp_directory=?", [str(duckdb_tmp)])
        params: list[str | int] = [str(lake_root / "**" / "*.parquet")]
        raw_where_clauses: list[str] = []
        if market_scope is not None:
            raw_where_clauses.append("market_scope = ?")
            params.append(market_scope)
        where_clauses: list[str] = []
        if active_window_days is not None:
            from datetime import datetime, timedelta, timezone

            cutoff_source = now_utc or datetime.now(timezone.utc)
            cutoff = cutoff_source - timedelta(days=active_window_days)
            where_clauses.append("last_seen_at >= ?")
            params.append(cutoff.isoformat())
        if limit is not None:
            params.append(limit)
        raw_where_clause = ("WHERE " + " AND ".join(raw_where_clauses)) if raw_where_clauses else ""
        where_clause = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        limit_clause = "LIMIT ?" if limit is not None else ""
        latest_meta = con.execute(
            f"""
            WITH latest_meta AS (
            SELECT
                vacancy_id,
                min(fetched_at) AS first_seen_at,
                max(fetched_at) AS last_seen_at,
                arg_max_null(filename, fetched_at) AS latest_file
            FROM read_parquet(?, hive_partitioning=false, union_by_name=true, filename=true)
            {raw_where_clause}
            GROUP BY vacancy_id
            )
            SELECT *
            FROM latest_meta
            {where_clause}
            ORDER BY last_seen_at DESC, vacancy_id
            {limit_clause}
            """,
            params,
        ).pl()

    rows: list[dict] = []
    remaining = set(latest_meta["vacancy_id"].to_list())
    for latest_file, meta in latest_meta.group_by("latest_file", maintain_order=True):
        meta = meta.rename({"last_seen_at": "fetched_at"})
        batch = _read_raw_batch(
            Path(latest_file[0]),
            columns=[
                "vacancy_id",
                "source",
                "employer_id",
                "posted_at",
                "raw_json",
                "fetched_at",
                "market_scope",
                "professional_role_id",
            ],
        )
        batch = batch.join(
            meta.select(["vacancy_id", "first_seen_at", "fetched_at"]),
            on=["vacancy_id", "fetched_at"],
            how="inner",
        )
        if batch.is_empty():
            continue

        batch = batch.rename({"fetched_at": "last_seen_at"})
        for record in batch.iter_rows(named=True):
            vacancy_id = record["vacancy_id"]
            if vacancy_id not in remaining:
                continue
            rows.append(
                _telegram_to_slim_row(record, rates)
                if record["source"] == "telegram"
                else _hh_to_slim_row(record, rates, details)
            )
            remaining.remove(vacancy_id)

    if not rows:
        return pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)

    return pl.DataFrame(rows, schema=SLIM_ACTIVE_SCHEMA)


def _telegram_to_slim_row(record: dict, rates: dict[str, float]) -> dict:
    """Telegram record → slim row.

    raw_json от RawRecord.from_telegram_message — {channel, message_id, date,
    text, views}. tg_parse даёт salary/city/remote/seniority. employer_name
    отсутствует (TG-каналы редко указывают компанию явно). source_url ведёт
    на t.me/{channel}/{id} — публично доступно.

    Salary в TG-тексте идёт в любой валюте ($/€/₽); parse_salary возвращает
    raw amount в обнаруженной валюте. CBR-rate конверсия в RUB обязательна
    ДО clamp'а — иначе $5000 (≈356k RUB) даст underflow vs RUB floor 10k
    и обнулится. salary_disclosed сбрасывается если после нормализации
    обе границы null (clamp removed both → факт раскрытия не доказан).
    """
    from src.enrich.salary_norm import normalize_to_rub
    from src.ingest.tg_parse import parse_city, parse_salary

    item = json.loads(record["raw_json"])
    text: str = item.get("text") or ""
    salary = parse_salary(text)
    city = parse_city(text)
    remote_type = parse_remote_type(text)
    seniority = parse_seniority(text)
    title = _extract_tg_title(text)
    skills = extract_skills(text)
    region = region_for_city(city)

    teaser = text[:500]

    channel = item.get("channel")
    message_id = item.get("message_id")
    source_url = f"https://t.me/{channel}/{message_id}" if channel and message_id else None

    salary_rub_min = normalize_to_rub(salary.min, salary.currency, rates)
    salary_rub_max = normalize_to_rub(salary.max, salary.currency, rates)
    tg_salary_min, tg_salary_max = _clamp_salary_outliers(salary_rub_min, salary_rub_max)
    disclosed = salary.disclosed and (tg_salary_min is not None or tg_salary_max is not None)
    return {
        "vacancy_id": record["vacancy_id"],
        "title": title,
        "employer_id": None,
        "employer_name": None,
        "salary_rub_min": tg_salary_min,
        "salary_rub_max": tg_salary_max,
        "salary_currency": salary.currency,
        "salary_disclosed": disclosed,
        "city": city,
        "region": region,
        "remote_type": remote_type,
        "seniority": seniority,
        "description_teaser": teaser,
        "skills": skills,
        "source": record["source"],
        "market_scope": record.get("market_scope"),
        "professional_role_id": record.get("professional_role_id"),
        "source_url": source_url,
        "first_seen_at": record["first_seen_at"],
        "last_seen_at": record["last_seen_at"],
        "posted_at": record["posted_at"],
    }


def _extract_tg_title(text: str) -> str:
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line[:200]
    return "(untitled)"


_RAW_BATCH_COLUMN_DTYPES = {
    "market_scope": pl.String,
    "professional_role_id": pl.Int64,
}


def _lake_has_column(lake_root: Path, column: str) -> bool:
    """Probe the newest parquet only — forward-only schema migrations.

    `market_scope` / `professional_role_id` were added 2026-05-09 to new
    writes; older partitions don't have them. Walking all 19k+ files
    cost ~44s on production profile; reading the newest file's schema
    is < 50ms regardless of lake size.
    """
    years = sorted(
        (p for p in lake_root.glob("year=*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    for year in years:
        months = sorted(
            (p for p in year.glob("month=*") if p.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        for month in months:
            files = list(month.rglob("*.parquet"))
            if files:
                newest = max(files, key=lambda p: p.stat().st_mtime)
                return column in pl.scan_parquet(newest).collect_schema().names()
    # Fallback for non-Hive layouts (ad-hoc fixtures): single rglob max.
    files = list(lake_root.rglob("*.parquet"))
    if not files:
        return False
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return column in pl.scan_parquet(newest).collect_schema().names()


def _read_raw_batch(path: Path, *, columns: list[str]) -> pl.DataFrame:
    available = set(pl.scan_parquet(path).collect_schema().names())
    read_columns = [column for column in columns if column in available]
    df = pl.read_parquet(path, columns=read_columns)
    missing = [column for column in columns if column not in df.columns]
    if missing:
        df = df.with_columns(
            pl.lit(None, dtype=_RAW_BATCH_COLUMN_DTYPES.get(column, pl.String)).alias(column)
            for column in missing
        )
    return df.select(columns)


_DEDUP_KEEP_PRIORITY = {"hh": 0, "telegram": 1}


def apply_cross_source_dedup(
    df: pl.DataFrame,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[pl.DataFrame, list[DuplicatePair]]:
    """Drop near-duplicate rows across sources (hh × telegram).

    Policy: keep the row with lowest `_DEDUP_KEEP_PRIORITY` (hh < telegram).
    Used for `publish slim --dedup`. Same-source rows are never collapsed —
    `find_duplicates(cross_source_only=True)`.

    Returns (filtered_df, pairs_found). pairs_found preserved for logging /
    side-table writes.
    """
    if df.is_empty():
        return df, []

    if df["source"].n_unique() < 2:
        return df, []

    items = [
        VacancyForDedup(
            vacancy_id=row["vacancy_id"],
            source=row["source"],
            title=row["title"],
            employer_name=row["employer_name"],
            city=row["city"],
            text=row["description_teaser"],
        )
        for row in df.iter_rows(named=True)
    ]
    pairs = find_duplicates(items, threshold=threshold, cross_source_only=True)
    if not pairs:
        return df, []

    drop_ids: set[str] = set()
    for p in pairs:
        prio_a = _DEDUP_KEEP_PRIORITY.get(p.source_a, 99)
        prio_b = _DEDUP_KEEP_PRIORITY.get(p.source_b, 99)
        loser = p.id_b if prio_a <= prio_b else p.id_a
        drop_ids.add(loser)

    filtered = df.filter(~pl.col("vacancy_id").is_in(list(drop_ids)))
    return filtered, pairs


def write_slim_active(df: pl.DataFrame, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out_path, compression="zstd", compression_level=3)
    return out_path
