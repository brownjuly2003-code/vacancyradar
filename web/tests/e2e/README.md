# Playwright e2e tests

Happy-path regression tests for the dashboard. Не запускаются в default
`npm run lint` (= tsc) — opt-in, требуют живого backend.

## Setup (один раз)

```bash
cd web
npm install                      # подтянет @playwright/test
npx playwright install chromium  # ~150 MB Chromium
```

## Run

```bash
cd web
npm run test:e2e
```

Конфиг (`playwright.config.ts`) поднимает `next dev` на :3000
(или reuse если уже запущен) и гоняет тесты в Chromium 1440×900.

`fullyParallel: false` — DuckDB cold-start не любит конкурентные
запросы на холодный кэш, тесты идут sequential.

## Что покрыто

`home.spec.ts`:
- facets загружаются + header показывает количество
- search input фильтрует результаты
- table ↔ cards toggle
- click card → slide-over detail panel + Esc closes
- tab nav → /trends

Это net-net regression catch для UI рефакторов. Глубокого покрытия
business logic нет — для этого pytest на backend.
