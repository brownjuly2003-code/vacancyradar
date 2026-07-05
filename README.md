# VacancyRadar

Дашборд и аналитика IT-рынка вакансий РФ. Python-конвейер собирает данные с hh.ru
(IT professional roles) + curated публичных Telegram-каналов, накапливает event
store + raw lake и публикует компактные артефакты в публичный Hugging Face
Dataset. Витрина — **статический no-DB storefront** на HF Space (две страницы:
поиск + аналитический дашборд), который сам пересобирается в облаке по крону.

**Live:** https://liovina-vacancyradar.static.hf.space (+ `/dashboard.html`)

## Состояние

| | |
|---|---|
| Продукт | Статическая витрина на ките «Чистый лист»: `index.html` (поиск) + `dashboard.html` (15 графиков: heatmap город×грейд, Lorenz-кривая работодателей, slopegraph зарплат, приток вакансий и др.), всё client-side из `data.json.gz` (~2.3 МБ), без сервера и БД |
| Сбор | iMac (чистый IP), launchd `com.vradar.collect` @ 07:00/17:00 МСК: `scripts/run_collect.sh` — full-sweep hh + Telegram (через локальный AdGuard VPN SOCKS) + publish HF mirror |
| Данные | Публичный HF Dataset [`liovina/vacancyradar-data`](https://huggingface.co/datasets/liovina/vacancyradar-data): `slim/active.parquet` + `slim/events_30d/` + `agg/weekly_*.parquet` |
| Self-refresh | GH Actions `refresh-storefront.yml` (cron 15:37 UTC): пересборка `data.json.gz`/`trends.json.gz` из HF mirror + деплой на Space. Не зависит от домашних машин |
| Мониторинг | GH Actions `healthcheck.yml` 2×/день: свежесть HF dataset (<30ч), row floor (≥20k), тренд корпуса (падение >15% = алерт), живость Space; провал → GitHub issue (email) |
| Tests | `ruff check src tests`, `mypy src`, `pytest -q` (CI coverage-гейт `--cov-fail-under=97`) |

## Архитектура

```
hh.ru shards ─┐  iMac: launchd 07:00/17:00 МСК          ОБЛАКО
              ├→ DuckDB master + raw lake ─→ HF Dataset liovina/vacancyradar-data
Telegram ─────┘  (event store, локально)      │  slim/active.parquet + events + weekly
 (через локальный                             ▼  cron 15:37 UTC
  AdGuard VPN SOCKS)              GH Actions refresh-storefront.yml
                                              │  build data.json.gz + deploy
                                              ▼
                                  HF Space liovina/vacancyradar  ← ПРОДУКТ
```

- **Локальный pipeline = source of truth**, облако = read-replica. Master хранит
  события (`appeared/seen/closed/salary_changed/desc_changed`), не снапшоты.
- **Telegram-egress**: линия iMac блокирует Telegram DC, поэтому Telethon ходит
  через AdGuard VPN CLI в SOCKS-режиме (`127.0.0.1:1080`) — туннелируется только
  Telethon, hh-трафик не затронут. Прокси поднимается перед tg-шагом и
  гарантированно гасится в конце прогона (trap).
- **Бэкап**: master lake → приватный HF dataset, еженедельно (iMac launchd).

Neon Postgres, Vercel (Next.js app + Blob) и Windows-контур (Scheduled Tasks,
Grafana/Alloy) **удалены 2026-07-06** — статическая витрина закрыла их роль без
quota-классов инцидентов. История — в git до коммита этой чистки.

## Quickstart (локальный pipeline)

```bash
# 1. Установка core deps (Python 3.12 — pinned)
make install
make install-ml          # NER + embeddings (~2 GB; локальный ML, в облако не идёт)

# 2. Smoke ingest — IT scope, без аутентификации
python -m src.cli ingest hh --scope it --pages 1 --per-page 50 --area 113

# 3. Полный цикл вручную (штатно его гоняет iMac scripts/run_collect.sh)
python -m src.cli ingest cbr                      # ЦБ rates → master/ref/cbr_rates.parquet
python -m src.cli ingest hh --scope it --full-sweep --detect-closed --per-page 100
python -m src.cli ingest telegram --scope it      # IT Telegram channels (нужен TG_PROXY, см. .env.example)
python -m src.cli publish slim --scope it --strict --active-days 14
python -m src.cli publish events                  # → derived/slim_events_30d/
python -m src.cli publish weekly --strict         # → 4 файла agg/weekly_*.parquet
python -m src.cli publish hf-mirror               # → HF Dataset (единственный внешний upload)
python -m src.cli enrich hh-details --rate 1.0    # description_teaser (некритичный хвост)

# 4. Витрина локально
cd static-proto && python build_artifact.py ../derived/slim_active.parquet . \
  && python -m http.server 8848                   # http://127.0.0.1:8848/ (file:// не работает)

# 5. Reports (Quarto + Python kernel)
python -m src.cli report monthly --month 2026-04  # → derived/reports/monthly_digest.html

# 6. Тесты
make test
```

OAuth-путь к `api.hh.ru` доступен через `--transport api` + `vradar auth hh ...` —
fallback на случай, если shards endpoint закроют.

### Initial full ingest

Для первого полного снимка активных вакансий hh.ru: `python -m src.cli refdata roles --refresh`,
`python -m src.cli refdata areas --refresh`, `python -m src.cli ingest hh-crawl --root area=113
--max-depth 4 --rate 1.0 --max-vacancies 2000000`. Crawler resume'ится из
`master/crawl_progress.json`. Полный проход — research/backfill path, не ежедневный live path.

**Closed detection — opt-in.** `ingest hh` без флага не эмитит `closed` (partial sweep даст
false positives). `--detect-closed` валиден только вместе с `--full-sweep`.

## Структура

См. `CLAUDE.md` § Структура для полного дерева, или `docs/architecture.md`.
Ключевое: `src/` (ingest/transform/enrich/publish/reports) · `static-proto/`
(витрина: `build_artifact.py` + 2 HTML) · `scripts/run_collect.sh` (iMac runner) ·
`.github/workflows/` (refresh-storefront + healthcheck + ci) · `docs/contracts/`
(Parquet-схемы slim/events/weekly).

## Команды

```bash
make install              # core + dev + reports
make install-ml           # +spacy +sentence-transformers +pylance (~2 GB, local-only)
make test                 # pytest -q (addopts in pyproject)
make lint                 # ruff check src tests

python -m src.cli publish slim --scope it   # локальный slim artifact
python -m src.cli publish hf-mirror         # публикация артефактов в HF Dataset
python -m src.cli enrich embeddings         # sentence-transformers → Lance (local-only)

# Витрина: ручной redeploy
gh workflow run refresh-storefront.yml -R brownjuly2003-code/vacancyradar-private

# Verify публичных артефактов через DuckDB httpfs
duckdb -c "INSTALL httpfs; LOAD httpfs; SELECT count(*) FROM \
read_parquet('https://huggingface.co/datasets/liovina/vacancyradar-data/resolve/main/slim/active.parquet');"
```

## Принципы

- Локальный pipeline = source of truth; cloud = read-replica
- Master хранит events, не snapshots (масштабируется)
- Raw API JSON хранится всегда — для re-parsing при изменении логики
- Витрина статическая: нет серверов, БД и quota-классов инцидентов;
  единственный внешний publish — публичный HF Dataset
- «Активно» = подтверждено sweep'ом за 14 дней, не накопленный корпус
- Полный текст вакансии не хранится в паблике — короткий teaser + ссылка
- Честность данных важнее полноты графиков: нечестные серии (кумулятивный
  closed, однословный шум рангов) на витрину не выводятся

## Лицензия

Личный проект, без открытой лицензии.
