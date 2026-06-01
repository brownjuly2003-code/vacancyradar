# Contract: `agg/weekly_*.parquet` — v1

**Цель:** pre-computed агрегаты для History дашборда (`/trends`). Weekly overwrite. Малый объём (<50 MB суммарно).

**Storage:** Vercel Blob, prefix `agg/`.

## Файлы

### `agg/weekly_role_salary.parquet`

Медианные зарплаты по роли × неделе × городу.

| Колонка | Type | Заметка |
|---|---|---|
| `week_start` | `Date` | понедельник недели (ISO) |
| `role_canonical` | `String` | normalized role from title (e.g. "data_analyst", "backend_engineer") |
| `seniority` | `String` | enum как в slim/active |
| `city` | `String` | `null` для national-level rollup |
| `n_vacancies` | `Int64` | размер выборки |
| `salary_rub_p25` | `Int64` | 25th percentile |
| `salary_rub_median` | `Int64` | |
| `salary_rub_p75` | `Int64` | |

### `agg/weekly_skill_velocity.parquet`

Top movers скиллов: % изменения упоминаемости неделя-к-неделе.

| Колонка | Type | Заметка |
|---|---|---|
| `week_start` | `Date` | |
| `skill` | `String` | lowercase tag (e.g. "polars", "duckdb", "kubernetes") |
| `mentions_this_week` | `Int64` | |
| `mentions_prev_week` | `Int64` | |
| `delta_pct` | `Float64` | (this - prev) / prev * 100 |
| `rank_this_week` | `Int32` | 1-based rank по mentions |

### `agg/weekly_employer_top.parquet`

Top hirers недели.

| Колонка | Type | Заметка |
|---|---|---|
| `week_start` | `Date` | |
| `employer_id` | `String` | |
| `employer_name` | `String` | |
| `new_vacancies` | `Int64` | events `appeared` за неделю |
| `closed_vacancies` | `Int64` | events `closed` за неделю |
| `disclosure_rate` | `Float64` | % новых с указанной зарплатой |
| `median_time_to_close_days` | `Float64` | по vacancy_id'ам которые `closed` за неделю |

### `agg/weekly_market_pulse.parquet`

Daily counts на week-resolution + market-wide trends.

| Колонка | Type | Заметка |
|---|---|---|
| `date` | `Date` | daily, не weekly |
| `total_active` | `Int64` | size of snapshot at end-of-day |
| `new_vacancies` | `Int64` | вакансии по `slim_active.posted_at` за день; `events.appeared` не используется, чтобы initial/backfill crawl не выглядел как рыночный приток |
| `closed_vacancies` | `Int64` | events `closed` за день |
| `salary_disclosure_rate` | `Float64` | % active с указанной зарплатой |
| `median_active_age_days` | `Float64` | (date - first_seen_at) для всех active |

## Refresh schedule

```
Sunday 06:00 local cron:
  python -m src.cli publish weekly
```

Это пересчитывает все 4 файла из master event store + raw lake и пушит в Vercel Blob.

## DuckDB примеры

Salary heatmap (роль × месяц × медиана) для frontend Plotly:
```sql
SELECT week_start, role_canonical, seniority, salary_rub_median
FROM read_parquet('https://<store>.public.blob.vercel-storage.com/agg/weekly_role_salary.parquet')
WHERE city IS NULL  -- national rollup
  AND week_start > now() - INTERVAL 6 MONTH
ORDER BY role_canonical, seniority, week_start;
```

Top growing skills:
```sql
SELECT skill, delta_pct, mentions_this_week
FROM read_parquet('https://<store>.public.blob.vercel-storage.com/agg/weekly_skill_velocity.parquet')
WHERE week_start = (SELECT max(week_start) FROM read_parquet('https://<store>.../agg/weekly_skill_velocity.parquet'))
  AND mentions_this_week >= 50  -- noise filter
ORDER BY delta_pct DESC LIMIT 25;
```

## Invariants

1. `week_start` всегда понедельник (ISO weekday 1).
2. `salary_rub_p25 <= salary_rub_median <= salary_rub_p75`.
3. `mentions_prev_week == 0` → `delta_pct = NULL` (не Inf).
4. `disclosure_rate ∈ [0.0, 1.0]`.
5. Поскольку это derived — пересоздание из master всегда даёт идентичный результат на той же date.
