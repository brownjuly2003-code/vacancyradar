// @vitest-environment jsdom
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DashboardPage from "@/app/page";
import type { FacetsResponse } from "@/lib/dashboard-types";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
}));

const FACETS: FacetsResponse = {
  summary: {
    total_vacancies: 88382,
    unique_cities: 1435,
    unique_employers: 13537,
    unique_skills: 288,
    latest_seen_at: "2026-05-31T04:11:00Z",
    source_breakdown: { hh: 41637, telegram: 46745 },
  },
  facets: {
    city: [{ value: "Москва", count: 23804 }],
    employer_name: [{ value: "Сбер. IT", count: 765 }],
    remote_type: [{ value: "remote", count: 100 }],
    seniority: [{ value: "senior", count: 11090 }],
    source: [
      { value: "hh", count: 41637 },
      { value: "telegram", count: 46745 },
    ],
    skills: [{ value: "Python", count: 9786 }],
    salary_range: {
      min: 10000,
      max: 5000000,
      p50: 118000,
      p90: 300000,
      with_salary_pct: 35,
    },
  },
  refreshed_at: "2026-05-31T04:11:00Z",
};

describe("DashboardPage", () => {
  const originalFetch = global.fetch;
  const originalMatchMedia = window.matchMedia;

  beforeEach(() => {
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
    class MockResizeObserver {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    }
    window.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
  });

  afterEach(() => {
    global.fetch = originalFetch;
    window.matchMedia = originalMatchMedia;
  });

  it("does not render empty results or pager when search fails", async () => {
    global.fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/facets")) {
        return Promise.resolve(Response.json(FACETS));
      }
      if (url.startsWith("/api/search")) {
        return Promise.resolve(
          Response.json({ error: "search_failed" }, { status: 503 }),
        );
      }
      throw new Error(`unexpected fetch: ${url}`);
    }) as unknown as typeof fetch;

    render(<DashboardPage />);

    expect(await screen.findByText("Не удалось загрузить вакансии")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("Ничего не найдено")).not.toBeInTheDocument();
    });
    expect(screen.queryByText("Попробуй убрать фильтры или сократить запрос.")).not.toBeInTheDocument();
    expect(screen.queryByText("Next →")).not.toBeInTheDocument();
  });
});
