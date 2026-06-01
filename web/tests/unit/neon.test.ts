import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `@/lib/neon` имеет `import "server-only"` (Next.js guard), который throws в
// vitest node env. Stub the marker module to a no-op so we can import the lib.
vi.mock("server-only", () => ({}));

const mocks = vi.hoisted(() => ({
  sqlQuery: vi.fn(),
  neonFactory: vi.fn(),
}));

vi.mock("@neondatabase/serverless", () => ({
  neon: (...args: unknown[]) => {
    mocks.neonFactory(...args);
    // Каждый getNeon() кэширует — но между тестами resetModules сбрасывает.
    const tagged = (() => undefined) as unknown as { query: typeof mocks.sqlQuery };
    tagged.query = mocks.sqlQuery;
    return tagged;
  },
}));

const ORIGINAL_URL = process.env.NEON_DATABASE_URL;
const ORIGINAL_READ_URL = process.env.NEON_READ_DATABASE_URL;

beforeEach(() => {
  mocks.sqlQuery.mockReset();
  mocks.neonFactory.mockReset();
  process.env.NEON_DATABASE_URL = "postgresql://owner@test/db";
  delete process.env.NEON_READ_DATABASE_URL;
  vi.resetModules();
});

afterEach(() => {
  if (ORIGINAL_URL === undefined) {
    delete process.env.NEON_DATABASE_URL;
  } else {
    process.env.NEON_DATABASE_URL = ORIGINAL_URL;
  }
  if (ORIGINAL_READ_URL === undefined) {
    delete process.env.NEON_READ_DATABASE_URL;
  } else {
    process.env.NEON_READ_DATABASE_URL = ORIGINAL_READ_URL;
  }
  vi.useRealTimers();
});

describe("withTimeout", () => {
  it("returns the wrapped value when it resolves within budget", async () => {
    const { withTimeout } = await import("@/lib/neon");
    const value = await withTimeout(Promise.resolve(42), 500, "fast op");
    expect(value).toBe(42);
  });

  it("rejects with a labeled timeout error when budget elapses", async () => {
    const { withTimeout } = await import("@/lib/neon");
    const slow = new Promise((resolve) => setTimeout(resolve, 200));
    await expect(withTimeout(slow, 20, "slow op")).rejects.toThrow(/slow op timeout after 20ms/);
  });

  it("clears the timer after the wrapped promise resolves (no leaked handle)", async () => {
    const { withTimeout } = await import("@/lib/neon");
    const clearSpy = vi.spyOn(global, "clearTimeout");
    await withTimeout(Promise.resolve("ok"), 1000, "noop");
    expect(clearSpy).toHaveBeenCalled();
    clearSpy.mockRestore();
  });
});

describe("isNeonConfigured", () => {
  it("reflects presence of NEON_DATABASE_URL", async () => {
    const { isNeonConfigured } = await import("@/lib/neon");
    expect(isNeonConfigured()).toBe(true);
    delete process.env.NEON_DATABASE_URL;
    expect(isNeonConfigured()).toBe(false);
  });
});

describe("getNeon", () => {
  it("throws when neither NEON_READ_DATABASE_URL nor NEON_DATABASE_URL is set", async () => {
    delete process.env.NEON_DATABASE_URL;
    delete process.env.NEON_READ_DATABASE_URL;
    const { getNeon } = await import("@/lib/neon");
    expect(() => getNeon()).toThrow(/NEON_READ_DATABASE_URL.*NEON_DATABASE_URL/);
  });

  it("caches the client between calls (neon() invoked once)", async () => {
    const { getNeon } = await import("@/lib/neon");
    getNeon();
    getNeon();
    getNeon();
    expect(mocks.neonFactory).toHaveBeenCalledTimes(1);
  });

  it("prefers NEON_READ_DATABASE_URL over NEON_DATABASE_URL (defence-in-depth)", async () => {
    process.env.NEON_DATABASE_URL = "postgresql://owner@test/db";
    process.env.NEON_READ_DATABASE_URL = "postgresql://readonly@test/db";
    const { getNeon } = await import("@/lib/neon");
    getNeon();
    expect(mocks.neonFactory).toHaveBeenCalledWith(
      "postgresql://readonly@test/db",
      expect.anything(),
    );
  });

  it("falls back to NEON_DATABASE_URL when readonly DSN is absent", async () => {
    process.env.NEON_DATABASE_URL = "postgresql://owner@test/db";
    delete process.env.NEON_READ_DATABASE_URL;
    const { getNeon } = await import("@/lib/neon");
    getNeon();
    expect(mocks.neonFactory).toHaveBeenCalledWith(
      "postgresql://owner@test/db",
      expect.anything(),
    );
  });
});

describe("neonQuery retry policy", () => {
  it("returns first-attempt result without backoff", async () => {
    mocks.sqlQuery.mockResolvedValueOnce([{ ok: true }]);
    const { neonQuery } = await import("@/lib/neon");
    const result = await neonQuery("SELECT 1");
    expect(result).toEqual([{ ok: true }]);
    expect(mocks.sqlQuery).toHaveBeenCalledTimes(1);
  });

  it("retries up to N attempts with exponential backoff", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.sqlQuery
      .mockRejectedValueOnce(new Error("transient 500"))
      .mockRejectedValueOnce(new Error("transient 503"))
      .mockResolvedValueOnce([{ ok: true }]);

    const { neonQuery } = await import("@/lib/neon");
    const result = await neonQuery("SELECT 1", [], { retries: 3, baseDelayMs: 1 });

    expect(result).toEqual([{ ok: true }]);
    expect(mocks.sqlQuery).toHaveBeenCalledTimes(3);
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });

  it("throws the last error after exhausting all retries", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.sqlQuery
      .mockRejectedValueOnce(new Error("err 1"))
      .mockRejectedValueOnce(new Error("err 2"))
      .mockRejectedValueOnce(new Error("err 3 final"));

    const { neonQuery } = await import("@/lib/neon");
    await expect(neonQuery("SELECT 1", [], { retries: 3, baseDelayMs: 1 })).rejects.toThrow(
      /err 3 final/,
    );
    expect(mocks.sqlQuery).toHaveBeenCalledTimes(3);
    warn.mockRestore();
  });

  it("logs non-Error throws as String(...) before retrying", async () => {
    // sql.query may reject with a non-Error (string, plain object); the warn
    // log path must still emit a coherent message, not "[object Object]"
    // tracebacks. Covers the `error instanceof Error ? ... : String(error)`
    // fallback branch.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    mocks.sqlQuery
      .mockRejectedValueOnce("ECONNRESET as bare string")
      .mockResolvedValueOnce([{ ok: true }]);

    const { neonQuery } = await import("@/lib/neon");
    const result = await neonQuery("SELECT 1", [], { retries: 2, baseDelayMs: 1 });

    expect(result).toEqual([{ ok: true }]);
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining("ECONNRESET as bare string"),
    );
    warn.mockRestore();
  });

  it("treats per-attempt timeout as a retryable error", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    // 1-я попытка зависает >timeoutMs → withTimeout rejects → retry → 2-я успешна.
    mocks.sqlQuery
      .mockImplementationOnce(() => new Promise(() => undefined))
      .mockResolvedValueOnce([{ ok: true }]);

    const { neonQuery } = await import("@/lib/neon");
    const result = await neonQuery("SELECT 1", [], {
      retries: 2,
      baseDelayMs: 1,
      timeoutMs: 20,
    });

    expect(result).toEqual([{ ok: true }]);
    expect(mocks.sqlQuery).toHaveBeenCalledTimes(2);
    expect(warn).toHaveBeenCalledWith(expect.stringContaining("timeout after 20ms"));
    warn.mockRestore();
  });

  it("passes an AbortSignal in fetchOptions and aborts it on timeout", async () => {
    // Verifies P2-6 fix: timeout actually cancels the in-flight HTTP request
    // instead of leaving a socket hanging. The mock captures the signal and
    // checks `aborted` after withTimeout rejects.
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    let capturedSignal: AbortSignal | undefined;
    mocks.sqlQuery
      .mockImplementationOnce((_sql: string, _params: unknown[], opts: { fetchOptions?: { signal?: AbortSignal } }) => {
        capturedSignal = opts?.fetchOptions?.signal;
        return new Promise(() => undefined);
      })
      .mockResolvedValueOnce([{ ok: true }]);

    const { neonQuery } = await import("@/lib/neon");
    await neonQuery("SELECT 1", [], {
      retries: 2,
      baseDelayMs: 1,
      timeoutMs: 20,
    });

    expect(capturedSignal).toBeInstanceOf(AbortSignal);
    expect(capturedSignal!.aborted).toBe(true);
    warn.mockRestore();
  });
});
