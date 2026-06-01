import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // Vitest 4 (rolldown) doesn't ship JSX transform — @vitejs/plugin-react adds
  // it so .test.tsx files can render React components. Pure .test.ts modules
  // (node env) are unaffected.
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "."),
    },
  },
  test: {
    environment: "node",
    // .test.ts → node env (lib pure helpers); .test.tsx files opt-in to jsdom
    // via a `// @vitest-environment jsdom` pragma at the top (Vitest 4
    // dropped environmentMatchGlobs in favor of per-file pragmas + projects).
    include: [
      "tests/unit/**/*.test.ts",
      "tests/unit/components/**/*.test.tsx",
    ],
    setupFiles: ["./tests/setup-jest-dom.ts"],
    coverage: {
      provider: "v8",
      reporter: ["text", "lcov"],
      include: ["lib/**/*.ts"],
      exclude: [
        "lib/**/*.test.ts",
        "lib/**/*.d.ts",
        "lib/skill-synonyms.json",
      ],
      reportsDirectory: "./coverage",
      // 2026-05-18: absolute 100% on lines/statements/functions after the
      // duckdb.ts mock + dashboard-format + facets/search/aggregates edge
      // pass. Branches floor 98% — remaining 1.15% is two genuinely
      // unreachable defensive checks kept for readability
      // (`row.salary_rub_max ?? 0` after a null-guard chain;
      // `if (timer) clearTimeout(timer)` after a synchronous Promise ctor).
      thresholds: {
        lines: 100,
        statements: 100,
        functions: 100,
        branches: 98,
      },
    },
  },
});
