import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `@/lib/snapshots` imports BLOB_BASE from `@/lib/duckdb`, which loads
// "server-only" and the DuckDB native bindings — both blow up in vitest's
// node environment. Mock duckdb to a stub so we can exercise fetchSnapshot
// purely against the global fetch.
async function loadSnapshots(blobBase: string) {
  vi.doMock("@/lib/duckdb", () => ({ BLOB_BASE: blobBase }));
  return await import("@/lib/snapshots");
}

describe("fetchSnapshot", () => {
  const ORIGINAL_FETCH = global.fetch;

  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    global.fetch = ORIGINAL_FETCH;
    vi.doUnmock("@/lib/duckdb");
    vi.useRealTimers();
  });

  it("returns null without performing fetch when BLOB_BASE is empty", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const { fetchSnapshot } = await loadSnapshots("");
    const result = await fetchSnapshot("facets.json");

    expect(result).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("parses and returns JSON on 200", async () => {
    const payload = { facets: { city: [{ count: 1, value: "Moscow" }] } };
    const fetchSpy = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    global.fetch = fetchSpy as unknown as typeof fetch;

    const { fetchSnapshot, SNAPSHOTS_BASE } = await loadSnapshots("https://blob.example.test");
    const result = await fetchSnapshot<typeof payload>("facets.json");

    expect(result).toEqual(payload);
    expect(fetchSpy).toHaveBeenCalledWith(
      `${SNAPSHOTS_BASE}/facets.json`,
      expect.objectContaining({ next: { revalidate: 300 } }),
    );
  });

  it("returns null on non-200 status without throwing", async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 403 }) as unknown as typeof fetch;

    const { fetchSnapshot } = await loadSnapshots("https://blob.example.test");
    expect(await fetchSnapshot("facets.json")).toBeNull();
  });

  it("returns null on generic fetch error (network / DNS)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    global.fetch = vi.fn().mockRejectedValue(new Error("ENOTFOUND")) as unknown as typeof fetch;

    const { fetchSnapshot } = await loadSnapshots("https://blob.example.test");
    const result = await fetchSnapshot("facets.json");

    expect(result).toBeNull();
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("[snapshots] fetch failed"),
      expect.any(Error),
    );
    warn.mockRestore();
  });

  it("aborts and returns null when fetch exceeds timeoutMs", async () => {
    // KM audit 2026-05-17 P1: без timeout зависший Blob edge node съедал бы
    // route maxDuration до Neon/DuckDB fallback.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    global.fetch = ((_url: string, init: { signal?: AbortSignal }) => {
      return new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      });
    }) as unknown as typeof fetch;

    const { fetchSnapshot } = await loadSnapshots("https://blob.example.test");
    const result = await fetchSnapshot("facets.json", { timeoutMs: 25 });

    expect(result).toBeNull();
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("timeout after 25ms"));
    warn.mockRestore();
  });

  it("passes a custom revalidateSeconds to the Next fetch directive", async () => {
    const fetchSpy = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    global.fetch = fetchSpy as unknown as typeof fetch;

    const { fetchSnapshot } = await loadSnapshots("https://blob.example.test");
    await fetchSnapshot("facets.json", { revalidateSeconds: 3600 });

    expect(fetchSpy.mock.calls[0][1]).toMatchObject({ next: { revalidate: 3600 } });
  });
});
