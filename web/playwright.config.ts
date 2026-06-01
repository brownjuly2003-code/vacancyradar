import { defineConfig, devices } from "@playwright/test";

const E2E_PORT = 3100;
const E2E_BASE_URL = `http://127.0.0.1:${E2E_PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false, // dev server один — параллель ломает state
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  timeout: 60_000, // первый рендер тащит DuckDB cold-start
  use: {
    baseURL: E2E_BASE_URL,
    actionTimeout: 10_000,
    navigationTimeout: 30_000,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
  webServer: {
    command: `npm run dev -- --hostname 127.0.0.1 --port ${E2E_PORT}`,
    url: E2E_BASE_URL,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      SEARCH_BACKEND: "neon",
    },
  },
});
