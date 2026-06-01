// Vitest setup file for component tests. Pulled in via vitest.config.ts
// `setupFiles` so every test gets jest-dom matchers (toBeInTheDocument, etc.)
// without per-file imports. Pure lib/* tests run under the `node` environment
// and ignore this file's DOM-related side effects.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing-library auto-cleanup only triggers when `test.globals: true` —
// register it explicitly so each `it()` starts with a fresh document.
afterEach(() => {
  cleanup();
});
