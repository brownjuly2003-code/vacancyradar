import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  neonQuery: vi.fn(),
}));

vi.mock("@/lib/neon", () => ({
  neonQuery: mocks.neonQuery,
}));

import { runNeonSearch, type NeonSearchFilters } from "@/lib/search-neon";

const baseFilters: NeonSearchFilters = {
  cities: [],
  employers: [],
  remoteTypes: [],
  seniorities: [],
  sources: [],
  skills: [],
  salaryMin: null,
  salaryMax: null,
  expandedTerms: [],
  limit: 50,
  offset: 0,
};

describe("runNeonSearch", () => {
  beforeEach(() => {
    mocks.neonQuery.mockReset();
  });

  it("builds filtered ILIKE SQL with stable ordering and split count params (single-token terms skip FTS)", async () => {
    mocks.neonQuery.mockImplementation((sql: string) => {
      if (sql.includes("COUNT(*)::int")) return Promise.resolve([{ total: 2 }]);
      return Promise.resolve([]);
    });

    await runNeonSearch({
      ...baseFilters,
      cities: ["Moscow"],
      remoteTypes: ["remote"],
      sources: ["hh"],
      skills: ["Python", "Django"],
      salaryMin: 100000,
      salaryMax: 300000,
      expandedTerms: ["python", "py_100%"],
      limit: 10,
      offset: 20,
    });

    expect(mocks.neonQuery).toHaveBeenCalledTimes(2);
    const [countSql, countParams] = mocks.neonQuery.mock.calls[0] as [string, unknown[]];
    const [dataSql, dataParams] = mocks.neonQuery.mock.calls[1] as [string, unknown[]];

    expect(countSql).toContain("SELECT COUNT(*)::int AS total FROM vacancies");
    expect(countSql).toContain("city IN ($1)");
    expect(countSql).toContain("skills @> ARRAY[$4]::text[]");
    expect(countSql).toContain("title ILIKE $8");
    // Single-token expanded terms: FTS skipped (session 14 evidence — substring
    // ILIKE на single tokens recall ≥ FTS, добавлять FTS только удлинит план).
    expect(countSql).not.toContain("plainto_tsquery");
    expect(countSql).not.toContain("LIMIT");
    expect(countParams).toEqual([
      "Moscow",
      "remote",
      "hh",
      "Python",
      "Django",
      100000,
      300000,
      "%python%",
      "%py\\_100\\%%",
    ]);

    expect(dataSql).toContain(
      "ORDER BY score DESC NULLS LAST, last_seen_at DESC, first_seen_at DESC, vacancy_id DESC",
    );
    expect(dataSql).toContain("LIMIT $10 OFFSET $11");
    expect(dataParams).toEqual([...countParams, 10, 20]);
  });

  it("adds Russian FTS OR-clause + score boost for multi-word expanded terms", async () => {
    mocks.neonQuery.mockImplementation((sql: string) => {
      if (sql.includes("COUNT(*)::int")) return Promise.resolve([{ total: 0 }]);
      return Promise.resolve([]);
    });

    await runNeonSearch({
      ...baseFilters,
      sources: ["hh"],
      expandedTerms: ["data engineer", "rust"],
      limit: 5,
      offset: 0,
    });

    const [countSql, countParams] = mocks.neonQuery.mock.calls[0] as [string, unknown[]];
    const [dataSql] = mocks.neonQuery.mock.calls[1] as [string, unknown[]];

    // Multi-word "data engineer" must produce a plainto_tsquery clause; single
    // "rust" must not. Push order: [%data engineer%, "data engineer" (FTS),
    // %rust%], так что плейсхолдер FTS = $3.
    expect(countSql).toMatch(/plainto_tsquery\('russian',\s*\$3\)/);
    expect(countParams).toEqual(["hh", "%data engineer%", "data engineer", "%rust%"]);

    // Score expression must include the FTS clause with +2 boost.
    expect(dataSql).toMatch(
      /CASE WHEN to_tsvector\('russian',[\s\S]*plainto_tsquery[\s\S]*THEN 2 ELSE 0 END/,
    );
  });

  it("normalizes Neon rows to the API search row shape", async () => {
    mocks.neonQuery
      .mockResolvedValueOnce([{ total: 1 }])
      .mockResolvedValueOnce([
        {
          vacancy_id: 123,
          title: null,
          employer_name: "ACME",
          salary_rub_min: "100000",
          salary_rub_max: null,
          salary_currency: null,
          city: "Moscow",
          region: null,
          remote_type: null,
          seniority: "middle",
          description_teaser: null,
          skills: ["Python"],
          source: null,
          source_url: null,
          posted_at: new Date("2026-05-16T12:00:00Z"),
          first_seen_at: new Date("2026-05-15T08:00:00Z"),
          last_seen_at: "2026-05-17T00:00:00Z",
          score: "3",
        },
      ]);

    const result = await runNeonSearch({ ...baseFilters, limit: 1 });

    expect(result).toEqual({
      total: 1,
      rows: [
        {
          vacancy_id: "123",
          title: "",
          employer_name: "ACME",
          salary_rub_min: 100000,
          salary_rub_max: null,
          salary_currency: null,
          city: "Moscow",
          region: null,
          remote_type: "unknown",
          seniority: "middle",
          description_teaser: null,
          skills: ["Python"],
          source: "hh",
          source_url: null,
          posted_at: "2026-05-16T12:00:00.000Z",
          first_seen_at: "2026-05-15T08:00:00.000Z",
          last_seen_at: "2026-05-17T00:00:00Z",
          score: 3,
        },
      ],
    });
  });

  it("treats an empty count rowset as total=0 (defence-in-depth fallback)", async () => {
    // neon-postgres always returns at least one row for `SELECT COUNT(*)::int
    // AS total`, but if a future driver upgrade or a router-side error swallows
    // it, the route must not crash on `rows[0].total` access. The `?? 0`
    // fallback ensures graceful behaviour.
    mocks.neonQuery
      .mockResolvedValueOnce([]) // count
      .mockResolvedValueOnce([]); // data

    const result = await runNeonSearch({ ...baseFilters, limit: 1 });

    expect(result).toEqual({ total: 0, rows: [] });
  });

  it("preserves all non-null scalar fields through String() coercion", async () => {
    // Existing tests pass null for region/source_url/description_teaser/
    // salary_currency, leaving the non-null branches of those ternaries
    // uncovered. Lock in that string fields round-trip cleanly.
    mocks.neonQuery
      .mockResolvedValueOnce([{ total: 1 }])
      .mockResolvedValueOnce([
        {
          vacancy_id: "999",
          title: "Senior backend",
          employer_name: "Yandex",
          salary_rub_min: 100,
          salary_rub_max: 200,
          salary_currency: "RUR",
          city: "Москва",
          region: "Центральный",
          remote_type: "remote",
          seniority: "senior",
          description_teaser: "Build cool stuff",
          // skillsRaw has non-string members → falls to [] (Array.every branch).
          skills: [1, "Python"],
          source: "hh",
          source_url: "https://hh.ru/vacancy/999",
          posted_at: "2026-05-17T00:00:00Z",
          first_seen_at: "2026-05-17T00:00:00Z",
          last_seen_at: "2026-05-17T00:00:00Z",
          score: 7,
        },
      ]);

    const result = await runNeonSearch({ ...baseFilters, limit: 1 });
    const row = result.rows[0];
    expect(row.salary_currency).toBe("RUR");
    expect(row.region).toBe("Центральный");
    expect(row.description_teaser).toBe("Build cool stuff");
    expect(row.source_url).toBe("https://hh.ru/vacancy/999");
    // Mixed-type skills array sanitized to []
    expect(row.skills).toEqual([]);
  });

  it("coerces unexpected timestamp shapes via String() fallback", async () => {
    // Defence: neon-postgres usually hands ISO strings or Date objects for
    // timestamptz columns, but if an upgrade switches to numeric epoch or
    // a vendor wrapper changes the driver, we surface something the route
    // can still serialize rather than throwing on .toISOString().
    mocks.neonQuery
      .mockResolvedValueOnce([{ total: 1 }])
      .mockResolvedValueOnce([
        {
          vacancy_id: "x",
          title: "x",
          employer_name: null,
          salary_rub_min: null,
          salary_rub_max: null,
          salary_currency: null,
          city: null,
          region: null,
          remote_type: null,
          seniority: null,
          description_teaser: null,
          skills: null,
          source: null,
          source_url: null,
          // Numeric epoch — falls to `return String(value)` branch.
          posted_at: 1747396800000,
          first_seen_at: null,
          last_seen_at: null,
          score: null,
        },
      ]);

    const result = await runNeonSearch({ ...baseFilters, limit: 1 });

    expect(result.rows[0].posted_at).toBe("1747396800000");
    // first_seen_at/last_seen_at are non-null in the API shape; null DB
    // value normalises to "" so JSON consumers don't see literal nulls there.
    expect(result.rows[0].first_seen_at).toBe("");
  });
});
