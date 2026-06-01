# Architecture

## Overview

VacancyRadar это **3-tier system по SLA**, где данные перетекают из локального master в cloud-read-replica только в нужном объёме.

**Целевой продукт с 2026-05-13:** cloud/live слой сужается до IT-рынка. Полный рынок РФ остаётся в локальном master и используется для monthly/ad-hoc research reports. Причина практическая: Turso free write quota уже стала hard cap для full-market daily sync, а качество enrichment/history важнее ширины live-корпуса.

```
┌──────────────────────────────────────────────────────────────────┐
│  LOCAL MACHINE (source of truth, без лимитов по объёму)         │
│                                                                  │
│  ┌────────────┐    ┌──────────────────┐                          │
│  │ hh.ru API  │───▶│ src/ingest/      │                          │
│  └────────────┘    │   hh_api.py      │                          │
│                    │   raw_lake.py    │                          │
│  ┌────────────┐    │ (Hive Parquet)   │                          │
│  │ Telegram   │───▶│                  │                          │
│  └────────────┘    └────────┬─────────┘                          │
│                             │                                    │
│                             ▼                                    │
│                    ┌──────────────────┐                          │
│                    │ master/          │                          │
│                    │  events.duckdb   │  ← event store           │
│                    │  vacancies_raw/  │  ← raw lake (full JSON)  │
│                    │  embeddings.lance│  ← Phase 5               │
│                    │  ref/            │  ← ЦБ rates, geo         │
│                    └────────┬─────────┘                          │
│                             │                                    │
│                             ▼                                    │
│                    ┌──────────────────┐                          │
│                    │ src/transform/   │                          │
│                    │  events_deriv    │   slim_export            │
│                    │  weekly_agg      │   (regenerable, в .gi)   │
│                    └────────┬─────────┘                          │
│                             │                                    │
│                             ▼                                    │
│                    ┌──────────────────┐                          │
│                    │ derived/         │                          │
│                    │  slim_active.parquet                        │
│                    │  slim_events_30d/                           │
│                    │  agg/weekly_*.parquet                       │
│                    └────────┬─────────┘                          │
└─────────────────────────────┼────────────────────────────────────┘
                              │
                              ▼ src/publish/hf_mirror.py
                              │ (HF-primary public artifact mirror)
┌─────────────────────────────┼────────────────────────────────────┐
│  PUBLIC ARTIFACT MIRROR + VERCEL CLOUD READ PATH                 │
│                             │                                    │
│                             ▼                                    │
│                    ┌──────────────────┐                          │
│                    │ Hugging Face     │                          │
│                    │  Dataset mirror  │                          │
│                    │  (public access) │                          │
│                    └────────┬─────────┘                          │
│                             │                                    │
│                             ▼                                    │
│                    ┌──────────────────┐                          │
│                    │ Next.js 15 app   │                          │
│                    │  app/page.tsx    │ ← live UI                │
│                    │  app/trends/     │ ← weekly history         │
│                    │  app/reports/    │ ← static HTML            │
│                    │                  │                          │
│                    │ @duckdb/node-api │ ← httpfs reads           │
│                    │  + Plotly        │   Parquet from mirror    │
│                    └──────────────────┘                          │
└──────────────────────────────────────────────────────────────────┘
```

## Product boundary

| Layer | Scope | Storage | Cadence | Purpose |
|---|---|---|---|---|
| Live IT | hh.ru professional roles из категории `Информационные технологии` + curated IT Telegram | Neon read model + HF public artifacts | Daily | Search, facets, similar vacancies, short history |
| IT history | IT events and aggregates | Local DuckDB master + HF/Neon aggregates | Daily/weekly | Trends, closed/open dynamics, salary/skills movement |
| Full-market research | Wide hh.ru + Telegram | Local raw lake + DuckDB + Quarto HTML | Monthly/ad-hoc | Market snapshots and reports, no daily Turso writes |

This boundary is intentional: live UI optimizes for freshness, latency, and write budget; research optimizes for breadth.

## Принципы

### 1. Master = source of truth, никогда не source

Любая cloud-derived таблица регенерируема. Если завтра поменяется логика NER скиллов — пересобираем `derived/` и пушим заново. Архив raw JSON в `master/vacancies_raw/` гарантирует что старые данные можно реинтерпретировать.

### 2. Event-sourcing вместо snapshots

`master/events.duckdb` хранит **6 типов событий**:
- `appeared` — первое появление вакансии
- `seen` — каждое появление в snapshot (опционально, может быть skipped для экономии)
- `closed` — вакансия исчезла. **Opt-in через `ingest hh --detect-closed`**: partial daily sweep видит лишь часть active set, без флага все вне scope ложно становилось бы `closed`. Для IT-scope detection разрешён только когда текущий запуск стартует с page 1 и доходит до последней hh page для каждой IT professional role; full-market closed detection остаётся research/backfill operation.
- `salary_changed` — payload `{old, new}` (shape-aware: shards `compensation` / api `salary`)
- `desc_changed` — payload null (раньше `{prev_hash, new_hash}`, дропнуто 2026-05-18 session 19; type row сам по себе считается /trends, а raw lake `content_hash` остаётся источником истины для диффа — payload было dead storage, ~12 MB/мес роста)
- `republished` — вернулась после `closed` (cross-day correlation, не в текущем коде)

Полные snapshot'ы daily = 1.5 TB/год. Events — 10–30 GB/год. В Neon/HF публикуется только read-optimized IT subset; long history остаётся в local master.

### 3. Raw lake — append-only

`master/vacancies_raw.parquet/year=YYYY/month=MM/source=hh/fetched_<ts>_<uuid>.parquet` с zstd compression. Каждый ingest run пишет новый файл, ничего не перезаписывает. Содержимое: `vacancy_id, source, fetched_at, posted_at, employer_id, content_hash, raw_json` (полный API response) плюс audit columns `market_scope` и `professional_role_id` для scoped live runs. Старые parquet-файлы без этих колонок читаются как legacy rows с `NULL` scope.

### 4. Slim slice = тонкий read view

В Neon + public artifact mirror идёт только то что нужно для live UI:
- `description_teaser` — 500 chars для карточек
- НЕТ полного описания (оно в master, или подгружается on-demand с hh.ru)

До сужения scope это давало ~700 MB на 700k активных вакансий и укладывалось в Vercel Blob Hobby, но Turso write budget стал главным лимитом. После Vercel Blob `store_suspended` operational path перешёл на Hugging Face Dataset mirror: `BLOB_PUBLIC_BASE_URL` указывает на HF `resolve/<revision>`, а legacy Blob PUT остаётся отключаемым fallback.

Implementation status: `build_slim_active(..., market_scope="it")` and `publish slim --scope it` now filter by `market_scope`. `publish hf-mirror` uploads the public parquet/json artifacts, while `publish neon` keeps the primary live read model fresh. Slim rows carry `market_scope` and `professional_role_id`, so the UI/read-replica can audit IT membership without title heuristics.

### 5. Heavy compute локально

NER скиллов через spaCy, embeddings через sentence-transformers (~768-dim mpnet), LSH dedup — всё локально. Никаких ML-deps в Vercel deployment, frontend bundle минимальный.

## Storage decision log

**Юзер не вводит карту нигде** (security concern) → отвергнуто:
- Cloudflare R2 (требует card)
- Yandex Object Storage (требует card)
- Selectel S3 (требует card)
- AWS / Backblaze (карта)
- Hetzner / Aeza VPS (карта)

**Hugging Face Datasets** — originally fallback, now primary public artifact mirror after Vercel Blob suspended writes. Frontend still reads it through the existing `BLOB_PUBLIC_BASE_URL` abstraction.

**Vercel Blob** — retained as legacy fallback. Hobby tier free без card, but the current store has returned `403 store_suspended`; upload commands skip Blob when `BLOB_PUBLIC_BASE_URL` points to Hugging Face.

**Turso libSQL** — выбран для no-cold-start UI search/history, но это не archival store. Инцидент 2026-05-12: incremental full-market sync получил `SQL write operations are forbidden`, поэтому daily writes должны быть ограничены IT-live delta и IT events.

## Vercel Blob: API quirks

This path is no longer the primary public artifact host, but the implementation remains for fallback/smoke usage.

Vercel Blob — **не s3-compatible**. boto3 не работает.

**Write (PUT):**
```
POST https://blob.vercel-storage.com/<pathname>
Authorization: Bearer <BLOB_READ_WRITE_TOKEN>
Content-Type: application/octet-stream
Body: <bytes>
```
Default behaviour — Vercel добавляет случайный suffix к pathname для cache busting:
`smoke/phase0.txt` → `smoke/phase0-hkT9CJrtDabikvTuf54Tl5Vo7Nf5yU.txt`

Для idempotent overwrite slim/active.parquet (daily refresh) нужны **HTTP headers**: `x-add-random-suffix: 0` + `x-allow-overwrite: 1` + `x-content-type: application/octet-stream`. Query-string эквивалент `?addRandomSuffix=0` Vercel **молча игнорирует** — response.pathname возвращает чистое имя, но реальный объект сохраняется с суффиксом → public URL по чистому имени даёт 404. Зашито в `src/publish/blob_push.py` (Phase 2 closed).

**Read:**
- Public URL: `https://<store-id-lowercase>.public.blob.vercel-storage.com/<pathname>`
- Без auth (public access by default)
- DuckDB httpfs читает напрямую через `range requests`

## hh.ru API gotchas

### Edge filter

`api.hh.ru/vacancies` закрыт Cloudflare-фильтром. **Без registered app — 403 forbidden** независимо от UA или headers. Эмпирически (2026-04-26) проверено 8+ User-Agents:
- Chrome 130 + полные browser headers — 403
- Firefox + headers — 403
- `App/version (email)` patterns — 403
- Только `MyApp/1.0 (test@test.ru)` доходит до приложения и получает 400 `bad_user_agent: blacklisted`

Whitelist основан на TLS-fingerprint (JA3) и UAs зарегистрированных приложений на dev.hh.ru.

### Решение (Phase 1 part 4, 2026-04-27): shards transport

`hh.ru/shards/vacancy/search` — публичный endpoint фронта hh.ru. Не требует Bearer. Edge пропускает запрос только если TLS-fingerprint = real Chrome → используем `curl_cffi` с `impersonate="chrome"` (копирует JA3/JA4/H2). Реализация: `src/ingest/hh_shards.py`. CLI default: `--transport shards`.

OAuth остался как `--transport api` fallback на случай если shards endpoint когда-нибудь закроют:
1. dev.hh.ru → Создать приложение (тип «Для собственных нужд»)
2. Получить `client_id` + `client_secret`
3. `POST https://api.hh.ru/token` с `grant_type=client_credentials` → access_token (TTL 14 дней)
4. Все запросы с `Authorization: Bearer <token>`

**Pagination limit:** обе ветки ограничены 2000 results на одну query (api: 20 × 100, shards: 100 × 50 = 5000). Для full sweep по РФ нужна **сегментация** по `area` или `date_published` (день за днём). Это **Phase 1.5 / 2 task**.

### Rate limit

Безопасно ~10 req/s для api.hh.ru, ~2 req/s для shards (более консервативно — это веб-эндпоинт). Превышение → 429 с `Retry-After` header. Оба клиента уважают `Retry-After`, иначе exponential до `backoff_max`.

## DuckDB через httpfs (frontend)

Next.js server function использует `@duckdb/node-api`:
```ts
import { DuckDBInstance } from "@duckdb/node-api"

const db = await DuckDBInstance.create(":memory:")
const con = await db.connect()
await con.run("INSTALL httpfs; LOAD httpfs;")
const url = `${process.env.BLOB_PUBLIC_BASE_URL}/slim/active.parquet`
const result = await con.runAndReadAll(
  `SELECT * FROM read_parquet('${url}') WHERE title ILIKE ? OR description_teaser ILIKE ? LIMIT 50`,
  [`%${query}%`, `%${query}%`]
)
```

DuckDB делает **range requests** на Parquet файл — читает только нужные row groups. Это значит:
- 700 MB Parquet можно эффективно фильтровать (10–100 KB сетевого трафика на запрос)
- Поиск через `ILIKE` или DuckDB FTS extension работает без полной загрузки

## Pipeline orchestration

Сейчас — простой cron + Makefile. Если pipeline вырастет → Prefect/Dagster (Phase 5+ подумать).

Current daily refresh flow (работает через shards transport, OAuth не нужен):
```bash
python -m src.cli ingest cbr                # → master/ref/cbr_rates.parquet (для salary RUB)
python -m src.cli ingest hh --scope it      # → IT HH raw rows + events
python -m src.cli ingest telegram --scope it # → curated IT Telegram raw rows
python -m src.cli enrich hh-details         # → master/hh_details.parquet (description_teaser/fts)
python -m src.cli publish slim --scope it   # → derived/slim_active.parquet
python -m src.cli publish events            # → derived/slim_events_30d/
python -m src.cli publish weekly            # → derived/agg/weekly_*.parquet
python -m src.cli publish snapshots         # → derived/snapshots/*.json
python -m src.cli publish neon              # → Neon read model
python -m src.cli publish hf-mirror         # → HF public artifacts
```

Target split:
- **Daily IT pipeline:** IT-scoped HH ingest + curated IT Telegram, IT slim/events/weekly, Neon read-model sync, HF artifact mirror. Scoped closed detection is allowed only after a completed current IT sweep.
- **Monthly full-market research:** wide HH + Telegram collection, local enrichment, Quarto/static report, optional public artifact, no Neon/Turso sync.

Vercel Hobby Crons можно настроить чтобы dashboard сам инвалидировал кэш ISR — но это не критично, refresh timestamp видно в углу UI.

## Что НЕ автоматизировано

- Отказоустойчивость локального cron (если комп выключен — slice устаревает; daily refresh = Win Scheduled Task на one machine)
- Telegram enrichment/dedup beyond basic ingest (Phase 4 basic Telethon ingest уже активен; следующий слой — enrichment + cross-source dedup)
- HH access token refresh (TTL 14 дней; нужен `HH_REFRESH_TOKEN` flow в `hh_auth.py`) — нужен только для `--transport api`, default shards без OAuth
- Public Vercel deploy — `web/` залинкован с проектом, но `vercel deploy --prod` не запускался (см. план Phase 9)
- Hybrid search в UI — BM25 уже live в `/api/search`, embeddings есть в Lance, но cosine rerank ещё не подключён (см. план Phase 8)

## Модули (актуально 2026-05-13)

**ingest:**
- `hh_api.py` — api.hh.ru OAuth fallback, FakeSession-friendly
- `hh_shards.py` — default `hh.ru/shards/vacancy/search` через curl_cffi
- `hh_detail.py` — `hh.ru/vacancy/{id}` HTML scrape для description (Phase 5)
- `cbr_rates.py` — daily ЦБ XML → master/ref/cbr_rates.parquet (Phase 5)
- `hh_auth.py` — OAuth client_credentials helper
- `raw_lake.py` — Hive Parquet write/read с volatile-strip, content_hash, `market_scope`, `professional_role_id`
- `tg_parse.py` — regex extractors salary/city/remote_type/seniority для Telegram ingest

**transform:**
- `events_derivation.py` — diff snapshots → events DuckDB
- `slim_export.py` — raw lake → slim_active.parquet (с salary normalisation + description teaser/fts из cache; shape branching shards/api; optional `market_scope` filter)
- `slim_events.py` — events DuckDB → slim_events_30d Hive partitioned
- `dedup.py` — MinHash LSH cross-source dedup для hh + Telegram

**enrich:**
- `salary_norm.py` — `compensation`/`salary` → RUB через CBR rates (Phase 5)

**publish:**
- `blob_push.py` — Vercel Blob REST upload (с x-add-random-suffix=0 quirk)
- `blob_ttl.py` — list+delete партиций events_30d вне 30-day window (Phase 5 tech-debt #2)
- `hf_mirror.py` — Hugging Face Dataset artifact mirror for slim/events/agg/snapshots

**web/ (Phase 3 ✅):**
- Next.js 15 + DuckDB httpfs + Recharts
- `api/facets`, `api/search` (prepared statements, SQL injection safe)
- `app/page.tsx` — 7 фасетов, debounce, Cards/Table toggle
