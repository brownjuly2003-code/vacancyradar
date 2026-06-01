/**
 * Coverage for `web/lib/duckdb.ts` — the DuckDB+httpfs fallback path.
 *
 * `@duckdb/node-api` is a native binding (libDuckDB.so / .dll) heavy enough
 * that loading it in test runs both slows things down and pins us to a
 * specific host arch. The unit test mocks the whole module so we exercise
 * the wrapper logic (extension load order, `memory_limit` setting, env
 * resolution, timeout enforcement, instance caching) without booting the
 * native DB.
 *
 * The integration path is still covered by Python parity tests
 * (`tests/integration/test_neon_parity.py`) on the Vercel deploy.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

const mocks = vi.hoisted(() => ({
  createInstance: vi.fn(),
  connect: vi.fn(),
  run: vi.fn(),
  runAndReadAll: vi.fn(),
  closeSync: vi.fn(),
}));

vi.mock("@duckdb/node-api", () => {
  const conn = {
    run: mocks.run,
    runAndReadAll: mocks.runAndReadAll,
    closeSync: mocks.closeSync,
  };
  const instance = { connect: () => Promise.resolve(conn) };
  return {
    DuckDBInstance: {
      create: (...args: unknown[]) => {
        mocks.createInstance(...args);
        return Promise.resolve(instance);
      },
    },
  };
});

const ORIGINAL_BLOB = process.env.BLOB_PUBLIC_BASE_URL;
const ORIGINAL_FALLBACK = process.env.NEXT_PUBLIC_BLOB_URL;
const ORIGINAL_EXT_DIR = process.env.DUCKDB_EXTENSION_DIRECTORY;

beforeEach(() => {
  mocks.createInstance.mockClear();
  mocks.connect.mockClear();
  mocks.run.mockReset();
  mocks.runAndReadAll.mockReset();
  mocks.closeSync.mockClear();
  mocks.run.mockResolvedValue(undefined);
  process.env.BLOB_PUBLIC_BASE_URL = "https://blob.example/";
  delete process.env.NEXT_PUBLIC_BLOB_URL;
  delete process.env.DUCKDB_EXTENSION_DIRECTORY;
  vi.resetModules();
});

afterEach(() => {
  if (ORIGINAL_BLOB === undefined) {
    delete process.env.BLOB_PUBLIC_BASE_URL;
  } else {
    process.env.BLOB_PUBLIC_BASE_URL = ORIGINAL_BLOB;
  }
  if (ORIGINAL_FALLBACK === undefined) {
    delete process.env.NEXT_PUBLIC_BLOB_URL;
  } else {
    process.env.NEXT_PUBLIC_BLOB_URL = ORIGINAL_FALLBACK;
  }
  if (ORIGINAL_EXT_DIR === undefined) {
    delete process.env.DUCKDB_EXTENSION_DIRECTORY;
  } else {
    process.env.DUCKDB_EXTENSION_DIRECTORY = ORIGINAL_EXT_DIR;
  }
});

describe("BLOB_BASE env resolution", () => {
  it("prefers BLOB_PUBLIC_BASE_URL over the legacy fallback", async () => {
    process.env.BLOB_PUBLIC_BASE_URL = "https://prod-blob/";
    process.env.NEXT_PUBLIC_BLOB_URL = "https://legacy-blob/";
    const mod = await import("@/lib/duckdb");
    expect(mod.BLOB_BASE).toBe("https://prod-blob/");
    expect(mod.ACTIVE_PARQUET).toBe("https://prod-blob//slim/active.parquet");
  });

  it("falls back to the active HF mirror when primary env is empty", async () => {
    process.env.BLOB_PUBLIC_BASE_URL = "";
    process.env.NEXT_PUBLIC_BLOB_URL = "https://legacy-blob";
    const mod = await import("@/lib/duckdb");
    expect(mod.BLOB_BASE).toBe(
      "https://huggingface.co/datasets/your-org/vacancyradar-data/resolve/main",
    );
  });

  it("uses the active HF mirror when no env var is set", async () => {
    delete process.env.BLOB_PUBLIC_BASE_URL;
    delete process.env.NEXT_PUBLIC_BLOB_URL;
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const mod = await import("@/lib/duckdb");
    expect(mod.BLOB_BASE).toBe(
      "https://huggingface.co/datasets/your-org/vacancyradar-data/resolve/main",
    );
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it("exports weekly parquet URLs anchored on BLOB_BASE", async () => {
    process.env.BLOB_PUBLIC_BASE_URL = "https://b";
    const mod = await import("@/lib/duckdb");
    expect(mod.WEEKLY_MARKET_PULSE_PARQUET).toBe(
      "https://b/agg/weekly_market_pulse.parquet",
    );
    expect(mod.WEEKLY_EMPLOYER_TOP_PARQUET).toBe(
      "https://b/agg/weekly_employer_top.parquet",
    );
    expect(mod.WEEKLY_SKILL_VELOCITY_PARQUET).toBe(
      "https://b/agg/weekly_skill_velocity.parquet",
    );
    expect(mod.WEEKLY_ROLE_SALARY_PARQUET).toBe(
      "https://b/agg/weekly_role_salary.parquet",
    );
  });
});

describe("EXTENSION_DIRECTORY resolution", () => {
  it("uses /tmp/duckdb-extensions on Vercel and forward-slashes the path", async () => {
    delete process.env.DUCKDB_EXTENSION_DIRECTORY;
    process.env.VERCEL = "1";
    const { getDuckDB } = await import("@/lib/duckdb");
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledWith(
      ":memory:",
      expect.objectContaining({ extension_directory: "/tmp/duckdb-extensions" }),
    );
    delete process.env.VERCEL;
  });

  it("honors an explicit DUCKDB_EXTENSION_DIRECTORY env var", async () => {
    process.env.DUCKDB_EXTENSION_DIRECTORY = "C:/custom/extensions";
    const { getDuckDB } = await import("@/lib/duckdb");
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledWith(
      ":memory:",
      expect.objectContaining({ extension_directory: "C:/custom/extensions" }),
    );
  });
});

describe("getDuckDB initialization", () => {
  it("creates the instance with memory_limit=768MB and loads httpfs + fts", async () => {
    const { getDuckDB } = await import("@/lib/duckdb");
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledWith(
      ":memory:",
      expect.objectContaining({
        memory_limit: "768MB",
        threads: "1",
        autoinstall_known_extensions: "true",
        autoload_known_extensions: "true",
      }),
    );
    const sqlCalls = mocks.run.mock.calls.map((c) => c[0]);
    expect(sqlCalls).toContain("INSTALL httpfs;");
    expect(sqlCalls).toContain("LOAD httpfs;");
    expect(sqlCalls).toContain("INSTALL fts;");
    expect(sqlCalls).toContain("LOAD fts;");
    expect(sqlCalls).toContain("SET http_timeout = 30000;");
  });

  it("honors DUCKDB_MEMORY_LIMIT env override (Pro plan / larger headroom)", async () => {
    process.env.DUCKDB_MEMORY_LIMIT = "2GB";
    const { getDuckDB } = await import("@/lib/duckdb");
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledWith(
      ":memory:",
      expect.objectContaining({ memory_limit: "2GB" }),
    );
    delete process.env.DUCKDB_MEMORY_LIMIT;
  });

  it("caches the instance across subsequent getDuckDB calls", async () => {
    const { getDuckDB } = await import("@/lib/duckdb");
    await getDuckDB();
    await getDuckDB();
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledTimes(1);
  });

  it("dedupes concurrent first-call initialization (returns the in-flight promise)", async () => {
    // Two routes hitting cold-start simultaneously must share one
    // DuckDBInstance.create — without this guard each request would spawn
    // its own in-memory DB and blow past the 768 MB ceiling.
    const { getDuckDB } = await import("@/lib/duckdb");
    const [a, b] = await Promise.all([getDuckDB(), getDuckDB()]);
    expect(a.instance).toBe(b.instance);
    expect(mocks.createInstance).toHaveBeenCalledTimes(1);
  });

  it("nulls the cached instance and rethrows when initialization fails", async () => {
    mocks.run.mockRejectedValueOnce(new Error("httpfs install failed"));
    const { getDuckDB } = await import("@/lib/duckdb");
    await expect(getDuckDB()).rejects.toThrow(/httpfs install failed/);
    // Failed instance is dropped, so the next attempt re-creates rather than
    // serving a broken cached one.
    mocks.run.mockResolvedValue(undefined);
    await getDuckDB();
    expect(mocks.createInstance).toHaveBeenCalledTimes(2);
  });
});

describe("runQuery", () => {
  it("uses runAndReadAll, returns row objects, and closes the connection", async () => {
    mocks.runAndReadAll.mockResolvedValueOnce({
      getRowObjectsJson: () => [{ count: 5 }],
    });
    const { runQuery } = await import("@/lib/duckdb");
    const rows = await runQuery("SELECT 1", []);
    expect(rows).toEqual([{ count: 5 }]);
    expect(mocks.closeSync).toHaveBeenCalled();
  });

  it("closes the connection even if the query throws", async () => {
    mocks.runAndReadAll.mockRejectedValueOnce(new Error("syntax error"));
    const { runQuery } = await import("@/lib/duckdb");
    await expect(runQuery("BAD SQL")).rejects.toThrow(/syntax error/);
    expect(mocks.closeSync).toHaveBeenCalled();
  });

  it("enforces a per-query timeout via withTimeout (default 8s)", async () => {
    // Make the query hang forever and provide a tight override so the
    // timeout fires immediately. The default is 8000ms — verifying that
    // path would slow the suite, but the explicit override covers the
    // race condition.
    mocks.runAndReadAll.mockImplementationOnce(
      () => new Promise(() => undefined),
    );
    const { runQuery } = await import("@/lib/duckdb");
    await expect(
      runQuery("SELECT pg_sleep(60)", [], { timeoutMs: 5 }),
    ).rejects.toThrow(/duckdb query timeout after 5ms/);
    expect(mocks.closeSync).toHaveBeenCalled();
  });
});
