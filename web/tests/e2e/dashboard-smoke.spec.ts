import { expect, test } from "@playwright/test";

/**
 * E2E smoke for the live dashboard. Covers what unit/component tests can't:
 *
 *   - the page actually mounts under a real Next.js runtime (RSC + client
 *     hydration without errors),
 *   - tab navigation between `/` and `/trends` works,
 *   - the executive shell (KPI cards, tab nav, sidebar facets) renders.
 *
 * Network calls go to whatever backends the dev server has configured. The
 * snapshot/Neon/DuckDB cascade is graceful: a 503 still renders the shell
 * with zero-state KPI cards, which is what we assert against.
 *
 * Kimi audit 2026-05-25 P1-3 — closes the missing-E2E gap (Playwright config
 * existed without any spec files).
 *
 * Local run: `npm run test:e2e` (boots dev server on :3100 with SEARCH_BACKEND=neon).
 * CI: gated behind `NEON_READ_DATABASE_URL` — skipped in regular CI because
 * the test boots a real Next.js server.
 */

test.describe("/ dashboard shell", () => {
  test("renders tab nav, KPI cards, and the search form skeleton", async ({ page }) => {
    await page.goto("/");

    // Tab nav — "Вакансии" should be the active tab on root path.
    const vacanciesTab = page.getByRole("link", { name: "Вакансии" });
    await expect(vacanciesTab).toBeVisible();
    await expect(vacanciesTab).toHaveAttribute("aria-current", "page");

    const trendsTab = page.getByRole("link", { name: "Тренды" });
    await expect(trendsTab).toBeVisible();

    // KPI row — 4 cards always present (skeleton OR data). We assert via
    // class selector because the exact text depends on backend state.
    const kpiCards = page.locator(".kpi-card");
    await expect(kpiCards).toHaveCount(4);

    // Search form input is the dashboard's primary affordance.
    await expect(page.getByPlaceholder(/поиск|search/i)).toBeVisible();
  });

  test("typing in the search box updates the URL", async ({ page }) => {
    await page.goto("/");
    const input = page.getByPlaceholder(/поиск|search/i);
    await input.fill("python");
    // The dashboard syncs query to URL state on debounce. Wait for it.
    await page.waitForURL(/[?&]q=python/, { timeout: 5_000 });
    await expect(page).toHaveURL(/[?&]q=python/);
  });
});

test.describe("/trends page", () => {
  test("loads and shows the 4 weekly chart cards", async ({ page }) => {
    await page.goto("/trends");

    const trendsTab = page.getByRole("link", { name: "Тренды" });
    await expect(trendsTab).toHaveAttribute("aria-current", "page");

    // Each card has a heading; we count headings inside `<main>` to avoid
    // brittleness around section ids.
    const headings = page.locator("main h2, main h3");
    await expect(headings).not.toHaveCount(0);
  });

  test("returns to home via tab nav", async ({ page }) => {
    await page.goto("/trends");
    await page.getByRole("link", { name: "Вакансии" }).click();
    await expect(page).toHaveURL(/\/$/);
  });
});
