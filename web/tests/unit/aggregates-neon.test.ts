import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  sql: vi.fn(),
}));

vi.mock("@/lib/neon", () => ({
  getNeon: () => mocks.sql,
  isNeonConfigured: () => true,
}));

import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";

describe("fetchAggregateFromNeon", () => {
  beforeEach(() => {
    mocks.sql.mockReset();
  });

  it("reads legacy aggregates rows when schema_version column is absent", async () => {
    const recentIso = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    mocks.sql.mockResolvedValueOnce([
      {
        payload: { summary: { total_vacancies: 42 } },
        schema_version: 1,
        refreshed_at: recentIso,
      },
    ]);

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toEqual({ summary: { total_vacancies: 42 } });
    expect(String.raw(mocks.sql.mock.calls[0][0])).toContain("to_jsonb(aggregates)");
  });

  it("rejects rows older than maxAgeHours (default 36h)", async () => {
    // KM re-audit 2026-05-17 P1: stale aggregate row used to bypass live data.
    const staleIso = new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString();
    mocks.sql.mockResolvedValueOnce([
      {
        payload: { summary: { total_vacancies: 1 } },
        schema_version: 1,
        refreshed_at: staleIso,
      },
    ]);

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toBeNull();
  });

  it("honors explicit maxAgeHours override", async () => {
    const eightHoursOldIso = new Date(Date.now() - 8 * 60 * 60 * 1000).toISOString();
    mocks.sql.mockResolvedValueOnce([
      {
        payload: { summary: { total_vacancies: 7 } },
        schema_version: 1,
        refreshed_at: eightHoursOldIso,
      },
    ]);

    const result = await fetchAggregateFromNeon("facets", { maxAgeHours: 4 });

    expect(result).toBeNull();
  });

  it("accepts rows when refreshed_at is null (column missing on legacy tables)", async () => {
    mocks.sql.mockResolvedValueOnce([
      {
        payload: { summary: { total_vacancies: 3 } },
        schema_version: 1,
        refreshed_at: null,
      },
    ]);

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toEqual({ summary: { total_vacancies: 3 } });
  });

  it("returns null on schema_version mismatch and logs a warning", async () => {
    // CX P2 schema_version drift guard: payload shape changed in publish
    // snapshots but Neon `aggregates` row still has v1. Route must fall
    // through to next layer rather than serve the wrong shape.
    const recentIso = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    mocks.sql.mockResolvedValueOnce([
      {
        payload: { stale: "shape" },
        schema_version: 999,
        refreshed_at: recentIso,
      },
    ]);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("schema_version mismatch"),
    );
    warn.mockRestore();
  });

  it("returns null and logs error when the SQL call throws", async () => {
    // Defence-in-depth: a transient Neon error must not bubble up and tank
    // the route — caller cascades to live recompute / 503.
    mocks.sql.mockRejectedValueOnce(new Error("ECONNRESET"));
    const err = vi.spyOn(console, "error").mockImplementation(() => {});

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toBeNull();
    expect(err).toHaveBeenCalledWith(
      expect.stringContaining("fetch failed for facets"),
      expect.any(Error),
    );
    err.mockRestore();
  });

  it("returns null when the aggregates row is missing entirely", async () => {
    // First boot after deploy / aggregates row deleted: SELECT returns 0
    // rows. Route must cascade to next layer, not crash on rows[0] access.
    mocks.sql.mockResolvedValueOnce([]);

    const result = await fetchAggregateFromNeon("facets");

    expect(result).toBeNull();
  });

  it("returns null when Neon is not configured", async () => {
    // Re-import path: stub isNeonConfigured=false via a fresh vi.doMock.
    vi.resetModules();
    vi.doMock("@/lib/neon", () => ({
      getNeon: () => {
        throw new Error("should not be called when not configured");
      },
      isNeonConfigured: () => false,
    }));
    const { fetchAggregateFromNeon: fetchUnconfigured } = await import(
      "@/lib/aggregates-neon"
    );

    const result = await fetchUnconfigured("facets");

    expect(result).toBeNull();
    vi.doUnmock("@/lib/neon");
  });
});
