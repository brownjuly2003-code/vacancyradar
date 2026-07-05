# VacancyRadar — статическая витрина (no-DB)

Витрина рынка вакансий, которая ищет, фильтрует и строит аналитику **полностью в
браузере**, без серверной базы данных. Решает класс проблем «бесплатный тариф БД
упёрся в лимит» (Vercel Blob egress → Turso writes → Neon compute): в горячем пути
запроса нет ни одного метрического сервиса — только статические файлы на CDN и
вычисления в JS.

## Из чего состоит

| Файл | Что это |
|---|---|
| `index.html` | поиск: загрузка, q + фасеты, KPI, таблица. Один файл, vanilla JS, без зависимостей. |
| `dashboard.html` | аналитика: 6 KPI + 8 срезов (города/навыки/зарплаты/работодатели/округа/распределение) с cross-filter по клику + секция **«Динамика рынка во времени»** (медиана з/п по неделям с полосой p25–p75, медиана по грейдам, навыки в движении). ECharts SVG на ките «Чистый лист» (`house-style/`). |
| `data.json.gz` | срез активных вакансий (~2.6 МБ gzip / ~53k строк). Браузер качает, распаковывает `DecompressionStream` и держит в памяти. |
| `trends.json.gz` | компактные недельные агрегаты для динамики (медиана з/п, з/п по грейдам, движение навыков). Несколько КБ. Только для `dashboard.html`. |
| `build_artifact.py` | строит оба `*.json.gz` из `slim_active.parquet` + недельных `agg/weekly_*.parquet` (локальный путь или HF-зеркало). Чистит markdown/эмодзи в заголовках, режет teaser, убирает пустые строки. **Floor-guard**: отказывается писать артефакт ниже `VR_MIN_ROWS` (по умолчанию 20000), чтобы усечённый upstream не подменил витрину пустой страницей. Сбой сборки трендов не валит основной артефакт (best-effort). |

## Запуск локально

`file://` не подойдёт (нужен fetch + DecompressionStream). Поднять http-сервер:

```bash
cd D:/VacancyRadar/static-proto
D:/Python/Python312/python.exe build_artifact.py ../derived/slim_active.parquet .   # собрать артефакты
D:/Python/Python312/python.exe -m http.server 8848
# открыть http://127.0.0.1:8848/  (поиск)  и  /dashboard.html  (аналитика)
```

## Обновление данных — самонастраиваемое (GitHub Actions)

Витрина обновляется в облаке, независимо от домашних машин:
`.github/workflows/refresh-storefront.yml` по cron (15:37 UTC, после публикации HF-зеркала)
тянет `build_artifact.py` из HF-датасета, проверяет размер артефакта и деплоит папку на
HF-Space `liovina/vacancyradar`. Ручной прогон — `gh workflow run refresh-storefront.yml`.

Локально вручную:

```bash
D:/Python/Python312/python.exe build_artifact.py            # с HF-зеркала (по умолчанию)
D:/Python/Python312/python.exe build_artifact.py path/to/slim_active.parquet .   # из локального parquet
```

## Деплой (любой статический хост, без карты)

Отдаётся вся папка (`index.html`, `dashboard.html`, `*.json.gz`, `house-style/`). Сейчас
живёт на **Hugging Face static Space** — https://liovina-vacancyradar.static.hf.space.
Альтернативы без карты: GitHub Pages, Vercel static. CDN сам отдаёт `*.json.gz` с
`application/gzip`; лимита класса «compute-hours / writes» здесь нет — только bandwidth.

## Проверено (2026-06-25, Playwright + screenshot_check)

53 416 активных вакансий · 0 ошибок в консоли · 0 горизонтального overflow · поиск и
фасеты пересчитываются по выборке · cross-filter сходится · `screenshot_check.mjs
--all-charts` → 11/11 графиков чисто (exit 0) · секция динамики на честных недельных
данных (market_pulse исключён: `total_active`/`disclosure`/`age` заполнены только на
последнем прогоне, `closed` кумулятивен).
