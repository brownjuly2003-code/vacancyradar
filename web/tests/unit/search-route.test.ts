import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runQuery: vi.fn(),
  runNeonSearch: vi.fn(),
  isNeonConfigured: vi.fn(),
}));

vi.mock("server-only", () => ({}));

vi.mock("@/lib/duckdb", () => ({
  ACTIVE_PARQUET: "https://hf.example/slim/active.parquet",
  runQuery: mocks.runQuery,
}));

vi.mock("@/lib/search-neon", () => ({
  runNeonSearch: mocks.runNeonSearch,
}));

vi.mock("@/lib/neon", () => ({
  isNeonConfigured: mocks.isNeonConfigured,
}));

const ROW = {
  vacancy_id: "hh:1",
  title: "Python developer",
  employer_name: "ACME",
  salary_rub_min: 100000,
  salary_rub_max: 200000,
  salary_currency: "RUR",
  city: "Москва",
  region: "ЦФО",
  remote_type: "remote",
  seniority: "middle",
  description_teaser: "Python backend",
  skills: ["Python"],
  source: "hh",
  source_url: "https://hh.ru/vacancy/1",
  posted_at: "2026-05-31T00:00:00Z",
  first_seen_at: "2026-05-31T00:00:00Z",
  last_seen_at: "2026-05-31T00:00:00Z",
  score: null,
};

describe("/api/search route", () => {
  const originalBackend = process.env.SEARCH_BACKEND;

  beforeEach(() => {
    vi.resetModules();
    mocks.runQuery.mockReset();
    mocks.runNeonSearch.mockReset();
    mocks.isNeonConfigured.mockReset();
    process.env.SEARCH_BACKEND = "duckdb";
  });

  afterEach(() => {
    if (originalBackend === undefined) {
      delete process.env.SEARCH_BACKEND;
    } else {
      process.env.SEARCH_BACKEND = originalBackend;
    }
  });

  it("uses Neon primary when configured even if legacy SEARCH_BACKEND=duckdb", async () => {
    mocks.runQuery.mockRejectedValue(new Error("duckdb query timeout after 8000ms"));
    mocks.isNeonConfigured.mockReturnValue(true);
    mocks.runNeonSearch.mockResolvedValue({ total: 1, rows: [ROW] });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);

    const { GET } = await import("@/app/api/search/route");
    const response = await GET(
      new Request("http://localhost/api/search?limit=5&source=hh") as never,
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.backend).toBe("neon");
    expect(body.total).toBe(1);
    expect(body.rows).toHaveLength(1);
    expect(mocks.runQuery).not.toHaveBeenCalled();
    expect(mocks.runNeonSearch).toHaveBeenCalledWith(
      expect.objectContaining({ sources: ["hh"], limit: 5, offset: 0 }),
    );
    errorSpy.mockRestore();
    warnSpy.mockRestore();
  });
});
