import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sql: vi.fn(),
}));

vi.mock("@/lib/neon", () => ({
  getNeon: () => mocks.sql,
}));

import { fetchFacetsFromNeon } from "@/lib/facets-neon";

describe("fetchFacetsFromNeon", () => {
  beforeEach(() => {
    mocks.sql.mockReset();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-17T00:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("maps Neon aggregate rows to the facets response shape", async () => {
    mocks.sql
      .mockResolvedValueOnce([
        {
          total_vacancies: 3,
          unique_cities: 1,
          unique_employers: 2,
          unique_skills: 2,
          latest_seen_at: "2026-05-16T23:00:00.000Z",
        },
      ])
      .mockResolvedValueOnce([{ value: "Moscow", count: 2 }])
      .mockResolvedValueOnce([{ value: "ACME", count: 2 }])
      .mockResolvedValueOnce([{ value: "remote", count: 3 }])
      .mockResolvedValueOnce([{ value: "middle", count: 3 }])
      .mockResolvedValueOnce([{ value: "hh", count: 3 }])
      .mockResolvedValueOnce([{ value: "Python", count: 2 }])
      .mockResolvedValueOnce([
        {
          min: 100000,
          max: 300000,
          p50: 150000,
          p90: 250000,
          with_salary_pct: 66.7,
        },
      ]);

    const result = await fetchFacetsFromNeon();

    expect(mocks.sql).toHaveBeenCalledTimes(8);
    expect(result).toEqual({
      summary: {
        total_vacancies: 3,
        unique_cities: 1,
        unique_employers: 2,
        unique_skills: 2,
        latest_seen_at: "2026-05-16T23:00:00.000Z",
        source_breakdown: {
          hh: 3,
          telegram: 0,
        },
      },
      facets: {
        city: [{ value: "Moscow", count: 2 }],
        employer_name: [{ value: "ACME", count: 2 }],
        remote_type: [{ value: "remote", count: 3 }],
        seniority: [{ value: "middle", count: 3 }],
        source: [{ value: "hh", count: 3 }],
        skills: [{ value: "Python", count: 2 }],
        salary_range: {
          min: 100000,
          max: 300000,
          p50: 150000,
          p90: 250000,
          with_salary_pct: 66.7,
        },
      },
      refreshed_at: "2026-05-17T00:00:00.000Z",
    });
  });

  it("fills in defaults when summary/salary rollup rows are empty", async () => {
    // Empty `vacancies` table — every SELECT returns 0 rows. The shape
    // contract must still match: `summary` is `{source_breakdown}` and
    // `salary_range` falls through to the zero placeholder.
    mocks.sql
      .mockResolvedValueOnce([]) // summary
      .mockResolvedValueOnce([]) // city
      .mockResolvedValueOnce([]) // employer_name
      .mockResolvedValueOnce([]) // remote_type
      .mockResolvedValueOnce([]) // seniority
      .mockResolvedValueOnce([]) // source
      .mockResolvedValueOnce([]) // skills
      .mockResolvedValueOnce([]); // salary_range

    const result = await fetchFacetsFromNeon();

    expect(result.summary).toEqual({
      source_breakdown: { hh: 0, telegram: 0 },
    });
    expect(result.facets).toEqual({
      city: [],
      employer_name: [],
      remote_type: [],
      seniority: [],
      source: [],
      skills: [],
      salary_range: {
        min: null,
        max: null,
        p50: null,
        p90: null,
        with_salary_pct: 0,
      },
    });
  });
});
