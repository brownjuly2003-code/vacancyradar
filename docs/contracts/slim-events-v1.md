# Contract: `slim/events_30d/` — v1

**Цель:** события вакансий за последние 30 дней для UI ribbon «новое / закрылось / зарплата изменилась». Daily overwrite (rolling window). Hive partitioned.

**Storage:** Vercel Blob, prefix `slim/events_30d/year=YYYY/month=MM/day=DD/events.parquet`.

## Schema

| Колонка | Type | Nullable | Заметка |
|---|---|---|---|
| `event_id` | `String` (UUID4) | NOT NULL | PK |
| `vacancy_id` | `String` | NOT NULL | `<source>:<id>` |
| `employer_id` | `String` | NULL | |
| `ts` | `Datetime` | NOT NULL | UTC, момент detect события |
| `type` | `String` | NOT NULL | enum (см. ниже) |
| `payload` | `String` (JSON) | NULL | type-specific |
| `source` | `String` | NOT NULL | `hh` / `telegram` |

## Event types

| `type` | Когда эмитится | `payload` |
|---|---|---|
| `appeared` | vacancy_id есть в current snapshot, нет в previous | null |
| `closed` | vacancy_id был в previous, нет в current | null |
| `salary_changed` | content_hash отличается + `salary` field в raw_json различается | `{"old": {...}, "new": {...}}` (полные salary objects) |
| `desc_changed` | content_hash отличается, но salary неизменна | null (раньше `{"prev_hash": ..., "new_hash": ...}`, дропнуто 2026-05-18 — никто не читал, +12 MB/мес в events.duckdb) |
| `seen` | (опционально) каждое появление в snapshot — для оценки uptime вакансии | null |
| `republished` | `appeared` на vacancy_id который раньше получил `closed` (cross-day correlation; **не реализовано в Phase 1**) | `{"closed_at": "..."}` |

## Partitioning

```
slim/events_30d/
├── year=2026/
│   ├── month=03/
│   │   ├── day=27/events.parquet
│   │   ├── day=28/events.parquet
│   │   └── ...
│   └── month=04/
│       ├── day=01/events.parquet
│       └── ...
```

Daily publish переписывает соответствующий `day=` partition. Партиции старше 30 дней удаляются (TTL).

## Invariants

1. `ts` ∈ [now() - 30 days, now()].
2. `event_id` уникален глобально.
3. Один `vacancy_id` может иметь много событий разных типов в одном dump.
4. Sort внутри файла: `ts ASC, type, vacancy_id` (для browser-friendly streaming).

## DuckDB примеры

Топ работодателей по найму за неделю:
```sql
SELECT employer_id, COUNT(*) AS new_jobs
FROM read_parquet('https://<store>.public.blob.vercel-storage.com/slim/events_30d/**/*.parquet')
WHERE type = 'appeared' AND ts > now() - INTERVAL 7 DAY
GROUP BY employer_id ORDER BY new_jobs DESC LIMIT 20;
```

Salary-trend для одной вакансии (если был `salary_changed`):
```sql
SELECT ts, payload
FROM read_parquet('https://.../slim/events_30d/**/*.parquet')
WHERE vacancy_id = 'hh:12345' AND type = 'salary_changed'
ORDER BY ts;
```
