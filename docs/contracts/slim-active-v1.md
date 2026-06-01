# Contract: `slim/active.parquet` — v1

**Цель:** snapshot активных вакансий для Live дашборда. Daily overwrite. Single file (без партиций). После IT-market pivot live artifact публикуется через `publish slim --scope it`; full-market slim остаётся research/backfill artifact. Размер ≤700 MB при ~700k активных в РФ, но live IT-scope должен быть заметно меньше.

**Storage:** Vercel Blob, путь `slim/active.parquet`, public read.

## Schema

| Колонка | Polars / DuckDB | Nullable | Заметка |
|---|---|---|---|
| `vacancy_id` | `String` | NOT NULL | PK. Формат `<source>:<id>`, e.g. `hh:12345` |
| `title` | `String` | NOT NULL | |
| `employer_id` | `String` | NULL | `<source>:<id>` или null если не указан |
| `employer_name` | `String` | NULL | |
| `salary_rub_min` | `Int64` | NULL | normalized в RUB через daily ЦБ rate (см. enrich/salary_norm) |
| `salary_rub_max` | `Int64` | NULL | |
| `salary_currency` | `String` | NULL | original ISO 4217 (RUR/USD/EUR/...) |
| `salary_disclosed` | `Boolean` | NOT NULL | true если хотя бы одна граница указана |
| `city` | `String` | NULL | |
| `region` | `String` | NULL | |
| `remote_type` | `String` | NOT NULL | `office` / `hybrid` / `remote` / `unknown` |
| `seniority` | `String` | NOT NULL | `intern` / `junior` / `middle` / `senior` / `lead` / `principal` / `unknown` |
| `description_teaser` | `String` | NULL | первые 500 chars cleaned для UI карточек и `/api/search` ILIKE |
| `skills` | `List[String]` | NOT NULL | canonical-case tags из `data/skills_taxonomy.yaml` (regex baseline + spaCy lemma matching). Может быть пустым массивом `[]` если ни один паттерн не сматчился |
| `source` | `String` | NOT NULL | `hh` / `telegram` |
| `market_scope` | `String` | NULL | `it` для scoped live rows; NULL для legacy/unscoped raw rows |
| `professional_role_id` | `Int64` | NULL | HH `professional_role` id для scoped/auditable HH rows; NULL для Telegram и legacy rows |
| `source_url` | `String` | NULL | прямая ссылка на оригинал |
| `first_seen_at` | `Datetime` | NOT NULL | UTC, microsecond precision |
| `last_seen_at` | `Datetime` | NOT NULL | |
| `posted_at` | `Datetime` | NULL | дата публикации работодателем |

## Compression

`zstd` level 3 (default). Test: 100k вакансий ≈ 80 MB после zstd.

## Invariants

1. `salary_rub_min <= salary_rub_max` если оба заданы.
2. `first_seen_at <= last_seen_at <= now()`.
3. Все `skills` в canonical-case по `data/skills_taxonomy.yaml` (e.g. `Python`, `ClickHouse`, `C++`, `.NET`), без дубликатов внутри массива, отсортированы.
4. `vacancy_id` уникален (PRIMARY KEY-like).
5. Для live IT publish все HH rows должны иметь `market_scope='it'` и non-null `professional_role_id`. Telegram rows may have `professional_role_id=NULL`, but still carry `market_scope='it'` when ingested through `--scope it`.

> **v1.1 (2026-05-16):** `description_fts` column dropped. Originally it held
> cleaned 1.5 KB of vacancy description for Turso FTS5 BM25; with /api/search
> migrated to DuckDB+httpfs and Turso writes blocked, the column became dead
> weight that doubled Blob egress per cold-start. Skills extraction still
> reads the full description from the hh_details cache during slim build, so
> the on-disk skill set is unchanged. UI search now uses title + teaser only.

## Why no full description

Полное описание HH-вакансии бывает 5–20 KB HTML. На 700k вакансий это 3.5–14 GB Parquet — не лезет в Vercel Blob 1 GB. **Полное описание держим только в `master/vacancies_raw.parquet/`**, на UI подгружается:
- Phase 3 MVP: ссылка «Открыть на hh.ru» — ничего не подгружаем
- Phase 7+ (опционально): отдельный endpoint `/api/vacancy/<id>/full` который читает с локального master через тоннель или сохранённый snapshot в Blob

## DuckDB пример запросов

Полнотекстовый поиск + фасеты для UI:
```sql
SELECT vacancy_id, title, employer_name, salary_rub_min, city, seniority, description_teaser
FROM read_parquet('https://<store>.public.blob.vercel-storage.com/slim/active.parquet')
WHERE (title ILIKE '%polars%' OR description_teaser ILIKE '%polars%')
  AND seniority IN ('senior', 'lead')
  AND remote_type IN ('remote', 'hybrid')
  AND salary_rub_min >= 250000
ORDER BY first_seen_at DESC
LIMIT 50;
```

## Migration

При v2 — добавить `_v2` suffix в pathname (`slim/active_v2.parquet`), Vercel Blob позволяет coexistence. Старая версия живёт пока фронт не переключится.
