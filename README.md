# VacancyRadar

Dashboard and analytics for the Russian IT job market. A Python pipeline ingests
data from hh.ru (IT professional roles) and a curated set of public Telegram
channels, accumulates an event store + raw lake, publishes an IT-scoped
slim/aggregates artifact set to a Hugging Face Dataset mirror, and syncs a live
read model into Neon Postgres. The frontend is a Next.js 15 executive dashboard
with a Neon-first search/facets path and JSON-snapshot / DuckDB+httpfs fallbacks.

**Live demo:** https://vacancyradar.vercel.app

## Highlights

- **No-auth hh.ru ingest** via the public `hh.ru/shards/vacancy/search` endpoint
  using `curl_cffi` Chrome JA3 impersonation (the API is Cloudflare/JA3-gated), with
  resilient 403/transient backoff. OAuth `--transport api` is kept as a fallback.
- **Event-sourced history** — the master store keeps `appeared/seen/closed/
  salary_changed/desc_changed` events (not daily snapshots), so history scales.
- **Cheap cloud surface** — pre-aggregated `slim/snapshots/*.json` (~52 KB total)
  replace a 12 MB parquet cold-read on the dashboard hot path (~225× egress
  reduction). Neon backs live search/facets; Hugging Face hosts public parquet
  for a DuckDB+httpfs fallback.
- **Enrichment** — CBR FX normalization to RUB, regex + Aho-Corasick skills
  taxonomy, spaCy `ru_core_news_sm` lemma matcher, seniority/remote inference,
  and sentence-transformers mpnet embeddings (local-only Lance store).
- **Quality gates** — backend `ruff` + `mypy` + `pytest` (coverage gate
  `--cov-fail-under=97`); frontend `vitest` unit + `playwright` e2e + `tsc` + build.

## Stack

- **Backend (Python 3.12):** DuckDB, Polars, pandas, `curl_cffi`, tenacity,
  Telethon, `pyahocorasick`. ML opt-in: spaCy, sentence-transformers, Lance.
- **Cloud:** Hugging Face Dataset (public artifact mirror) + Neon Postgres
  (live read model). Vercel Blob remains a legacy fallback.
- **Frontend:** Next.js 15 + React 19 + vanilla CSS + Recharts; Neon-first
  API routes with JSON-snapshot and DuckDB+httpfs fallbacks.

## Architecture

```
hh.ru shards ─┐                            ┌─→ Neon vacancies + aggregates ─→ /api/search, /api/facets
              ├→ DuckDB master + raw lake ─┤
Telegram ─────┘                            ├─→ HF slim/active.parquet (12 MB)  ─→ DuckDB+httpfs fallback
                                           ├─→ HF agg/weekly_*.parquet (~50 KB) ─→ /api/trends/* fallback
                                           └─→ HF slim/snapshots/*.json (~52 KB) ─→ facets/trends fast path
```

3-tier-by-SLA design:

- **Live IT** — Neon `vacancies` + `aggregates` for `/api/search` and
  `/api/facets`; HF `slim/active.parquet` is the fallback/export surface.
- **JSON snapshots** — pre-aggregated `slim/snapshots/{facets,trends/*}.json`
  cache facets/trends; routes fall back to Neon aggregates or DuckDB+httpfs.
- **IT history** — weekly aggregates in the HF mirror, surfaced on `/trends`.
- **Long master** — DuckDB event store + raw Parquet lake + (local-only) Lance
  embeddings.
- **Full-market reports** — Quarto static HTML, generated on demand, off-cloud.

The local pipeline is the source of truth; the cloud is a read replica. Any
logic change can rebuild `derived/` without losing history.

## Quickstart

```bash
# 1. Install (Python 3.12)
make install
make install-ml          # NER + embeddings (~2 GB; Visual C++ Redist on Windows)

# 2. Smoke ingest — IT scope, no auth
python -m src.cli ingest hh --scope it --pages 1 --per-page 50 --area 113

# 3. Daily pipeline
python -m src.cli ingest cbr                      # CBR FX rates
python -m src.cli ingest hh --scope it            # HH IT roles
python -m src.cli ingest telegram --scope it      # IT Telegram channels
python -m src.cli enrich hh-details --rate 1.0    # description teaser/FTS
python -m src.cli publish slim --scope it         # → derived/slim_active.parquet
python -m src.cli publish events                  # → derived/slim_events_30d/
python -m src.cli publish weekly                  # → agg/weekly_*.parquet
python -m src.cli publish snapshots               # → slim/snapshots/*.json
python -m src.cli publish neon                    # → Neon read model
python -m src.cli publish hf-mirror               # → Hugging Face public artifacts

# 4. Frontend
cd web && npm install && npm run dev              # / dashboard + /trends

# 5. Reports (Quarto + Python kernel)
python -m src.cli report monthly --month 2026-04
python -m src.cli report skill
python -m src.cli report employer --employer hh:1373

# 6. Tests
make test
```

Configuration is environment-driven — copy `.env.example` to `.env` and fill in
the Hugging Face / Neon / hh.ru / Telegram values.

### Initial full ingest

For the first full snapshot of active hh.ru vacancies:
`refdata roles --refresh`, `refdata areas --refresh`, then
`ingest hh-crawl --root area=113 --max-depth 4 --rate 1.0 --max-vacancies 2000000`.
The crawler resumes from `master/crawl_progress.json`. This is a research/backfill
path, not the daily live path.

### Daily refresh

The daily pipeline (`ingest cbr → ingest hh --scope it → ingest telegram
--scope it → enrich hh-details → publish slim/events/weekly/snapshots/neon/
hf-mirror`) is intended to run once a day via a scheduler (cron / Task
Scheduler). HH/TG ingest failures block publish unless
`VRADAR_ALLOW_STALE_PUBLISH=1` is explicitly set.

**Closed detection is opt-in.** `ingest hh` without a flag does not emit
`closed` (a partial sweep would produce false positives). `ingest hh --scope it
--detect-closed` is only valid when a run starts at page 1 and reaches the last
hh page for every IT professional role.

## Testing

```bash
ruff check src tests
mypy src
pytest -q -p no:schemathesis        # backend (coverage gate 97%)

cd web
npm run test:unit                   # vitest
npm run lint                        # tsc --noEmit
npm run test:e2e                    # playwright
npm run build
```

## Documentation

- `docs/architecture.md` — architectural rationale.
- `docs/contracts/` — Parquet schemas for slim / events / weekly aggregates.

## License

Personal project, no open license.
