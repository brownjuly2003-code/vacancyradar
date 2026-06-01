-- VacancyRadar /api/search backend on Neon Postgres.
-- Applied via `vradar publish neon --init`. Idempotent (IF NOT EXISTS).
--
-- Index plan derived from preflight (.tmp/search-preflight/report.md):
--   - pg_trgm GIN on title + description_teaser → ILIKE substring matching
--   - BTREE on facet columns → equality WHERE
--   - BTREE on salary_rub_min/max → range WHERE
--   - GIN on skills (text[]) → array containment for skills filter
--   - BTREE DESC on last_seen_at → ORDER BY recency
--
-- Storage estimate (67k IT corpus, 1.6× index overhead): ~200 MB on 512 MB free tier.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS vacancies (
    vacancy_id          text PRIMARY KEY,
    title               text NOT NULL,
    employer_id         text,
    employer_name       text,
    salary_rub_min      bigint,
    salary_rub_max      bigint,
    salary_currency     text,
    salary_disclosed    boolean,
    city                text,
    region              text,
    remote_type         text NOT NULL DEFAULT 'unknown',
    seniority           text NOT NULL DEFAULT 'unknown',
    description_teaser  text,
    skills              text[] NOT NULL DEFAULT ARRAY[]::text[],
    source              text NOT NULL,
    market_scope        text,
    professional_role_id text,
    source_url          text,
    first_seen_at       timestamptz NOT NULL,
    last_seen_at        timestamptz NOT NULL,
    posted_at           timestamptz
);

CREATE INDEX IF NOT EXISTS idx_vacancies_title_trgm
    ON vacancies USING gin (title gin_trgm_ops);

CREATE INDEX IF NOT EXISTS idx_vacancies_teaser_trgm
    ON vacancies USING gin (description_teaser gin_trgm_ops);

-- Russian FTS over title + teaser. Stemmer catches морфологические варианты
-- ('масштабирование' ↔ 'масштабировать' ↔ 'масштабируется', 'data engineer'
-- ↔ 'data engineering'), которые pg_trgm ILIKE-substring пропускает.
--
-- Evidence (session 14, 67k IT corpus, EXPLAIN ANALYZE):
--   "data engineer":   FTS 1410 matches vs ILIKE 958 (+47%), 11.4ms vs 254ms
--   "масштабирование": FTS  412 matches vs ILIKE 215 (+92%), <10ms vs >100ms
--   "тестировщик":     FTS 1028 matches vs ILIKE 1152, 1.4ms vs 95.2ms (68×)
-- Index size 21 MB. Single-token substring queries (python3, rust-lang)
-- ILIKE catches лучше — FTS применяется ТОЛЬКО для multi-word terms
-- в web/lib/search-neon.ts (OR-union с ILIKE сохраняет recall ⊇ ILIKE-only).
CREATE INDEX IF NOT EXISTS idx_vacancies_fts_ru
    ON vacancies USING gin (
        to_tsvector('russian', title || ' ' || COALESCE(description_teaser, ''))
    );

CREATE INDEX IF NOT EXISTS idx_vacancies_skills
    ON vacancies USING gin (skills);

CREATE INDEX IF NOT EXISTS idx_vacancies_city           ON vacancies (city);
CREATE INDEX IF NOT EXISTS idx_vacancies_employer       ON vacancies (employer_name);
CREATE INDEX IF NOT EXISTS idx_vacancies_remote_type    ON vacancies (remote_type);
CREATE INDEX IF NOT EXISTS idx_vacancies_seniority      ON vacancies (seniority);
CREATE INDEX IF NOT EXISTS idx_vacancies_source         ON vacancies (source);
CREATE INDEX IF NOT EXISTS idx_vacancies_salary_min     ON vacancies (salary_rub_min);
CREATE INDEX IF NOT EXISTS idx_vacancies_salary_max     ON vacancies (salary_rub_max);
CREATE INDEX IF NOT EXISTS idx_vacancies_last_seen_desc ON vacancies (last_seen_at DESC);

-- Pre-aggregated snapshots for /api/facets and /api/trends/*. Each row stores
-- one JSON payload — same shape the route returns — so the trends path works
-- even when Vercel Blob is suspended (Neon-only resilience).
--
-- schema_version: payload shape version. Bumped whenever route response shape
-- changes (renaming fields, new required keys, etc.). Routes ignore rows
-- whose version doesn't match CURRENT_AGGREGATE_SCHEMA_VERSION and fall
-- through to the next layer (live recompute or DuckDB). Without this, a
-- mid-deploy version skew would serve stale-shape JSON to a route expecting
-- the new shape. CX audit 2026-05-17 P2.
CREATE TABLE IF NOT EXISTS aggregates (
    name           text PRIMARY KEY,
    payload        jsonb NOT NULL,
    schema_version int NOT NULL DEFAULT 1,
    refreshed_at   timestamptz NOT NULL DEFAULT now()
);

-- Idempotent column add for tables that pre-date the schema_version column
-- (existing prod table has no version). NOT VALID skip needed — column has
-- a default, so backfill is instant.
ALTER TABLE aggregates ADD COLUMN IF NOT EXISTS schema_version int NOT NULL DEFAULT 1;
