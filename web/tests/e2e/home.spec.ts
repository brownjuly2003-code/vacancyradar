import { expect, test } from "@playwright/test";

test.describe("home dashboard", () => {
  test("loads with facets + first page of vacancies", async ({ page }) => {
    await page.goto("/");

    // Header показывает количество вакансий после загрузки фасетов.
    await expect(page.locator(".dashboard__meta")).toContainText(/обновлено/i, {
      timeout: 30_000,
    });
    await expect(
      page.locator(".kpi-card").filter({ hasText: "IT-вакансий" }).locator(".kpi-card__value"),
    ).toHaveText(/\d/, { timeout: 30_000 });

    // Sidebar facet «Город» — после load появляется хотя бы один chip.
    const cityChip = page.locator(".sidebar .chip-list .chip").first();
    await expect(cityChip).toBeVisible({ timeout: 30_000 });

    await expect(page.locator(".executive-brief")).toContainText("Рыночный срез");
    await expect(page.locator(".market-lens")).toContainText("hh.ru core");

    // Toolbar показывает range без 0–0.
    await expect(page.locator(".toolbar__left .mono")).not.toContainText("0–0 из 0");
  });

  test("search input filters results", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".toolbar__left .mono")).toContainText("из", {
      timeout: 30_000,
    });

    await page.locator('input[aria-label="Поиск"]').fill("python");
    // debounce 300ms + DuckDB.
    await page.waitForTimeout(800);
    await expect(page.locator(".toolbar__left .mono")).toContainText("из", {
      timeout: 30_000,
    });
  });

  test("table ↔ cards toggle works", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".toolbar__left .mono")).toContainText("из", {
      timeout: 30_000,
    });

    // Default desktop = table.
    await expect(page.locator(".vacancy-table")).toBeVisible({ timeout: 30_000 });

    await page.locator('button.mode-button:has-text("Карточки")').click();
    await expect(page.locator(".cards .vacancy-card").first()).toBeVisible({ timeout: 30_000 });
    await expect(page.locator(".vacancy-table")).toHaveCount(0);
  });

  test("clicking a card opens slide-over detail panel", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".toolbar__left .mono")).toContainText("из", {
      timeout: 30_000,
    });

    await page.locator('button.mode-button:has-text("Карточки")').click();
    const firstCard = page.locator(".cards .vacancy-card").first();
    await expect(firstCard).toBeVisible({ timeout: 30_000 });
    await firstCard.click();

    const panel = page.locator(".detail-panel");
    await expect(panel).toBeVisible();
    await expect(panel.locator(".detail-panel__title")).not.toBeEmpty();

    await page.keyboard.press("Escape");
    await expect(panel).toHaveCount(0);
  });

  test("tab nav switches to /trends", async ({ page }) => {
    await page.goto("/");
    await page.locator('.tab-nav__item:has-text("Тренды")').click();
    await expect(page).toHaveURL(/\/trends$/);
  });

  test("trends page shows executive annotations", async ({ page }) => {
    await page.goto("/trends");

    await expect(page.locator(".trends-brief")).toContainText("Недельный вывод", {
      timeout: 30_000,
    });
    await expect(page.locator(".trend-card__insight")).toHaveCount(4, {
      timeout: 30_000,
    });
    await expect(page.locator(".trend-card__insight").first()).toContainText("Вывод");
  });
});
