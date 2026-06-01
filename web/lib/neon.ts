import "server-only";
import { neon } from "@neondatabase/serverless";

/**
 * Neon HTTP SQL client for `/api/search` Neon-backend.
 *
 * @neondatabase/serverless uses fetch (not TCP), so it works on Vercel
 * Edge + Node runtimes without keepalive connections.
 *
 * Lazy because at module-load time NEON_DATABASE_URL may not be set in
 * environments where the backend is unused (e.g. tests with
 * SEARCH_BACKEND=duckdb).
 *
 * **Connection string priority:**
 *   1. NEON_READ_DATABASE_URL — DSN for read-only role `vradar_readonly`
 *      (SELECT only, INSERT/UPDATE/DELETE return InsufficientPrivilege).
 *      Defence-in-depth: даже если runtime credential leak'нет, attacker
 *      получает только read. Session 14, 2026-05-17.
 *   2. NEON_DATABASE_URL — owner DSN, fallback для local dev / pre-migration
 *      environments. Web routes только SELECT, never need write.
 */

let cached: ReturnType<typeof neon> | null = null;

function resolveDsn(): string | undefined {
  return process.env.NEON_READ_DATABASE_URL ?? process.env.NEON_DATABASE_URL;
}

export function getNeon() {
  if (cached) return cached;
  const url = resolveDsn();
  if (!url) {
    throw new Error(
      "NEON_READ_DATABASE_URL (preferred) or NEON_DATABASE_URL must be set for SEARCH_BACKEND=neon",
    );
  }
  cached = neon(url, { fullResults: false });
  return cached;
}

export function isNeonConfigured(): boolean {
  return !!resolveDsn();
}

/**
 * Race a promise against a timeout. Returns the promise value if it resolves
 * within timeoutMs, otherwise throws `Error: <label> timeout after <N>ms`.
 *
 * Used to bound per-layer latency in the graceful-degradation chain. Without
 * timeouts, a slow-but-not-failed layer would burn the route's maxDuration
 * budget before deeper fallbacks could execute. KM audit 2026-05-17 P1.
 *
 * Note: this only stops *waiting* — the wrapped promise keeps running. For
 * fetch we use AbortController to actually cancel. For Neon HTTP and DuckDB
 * the underlying ops don't take AbortSignal, so the call continues in the
 * background until the runtime tears down the request.
 */
export async function withTimeout<T>(
  promise: Promise<T>,
  timeoutMs: number,
  label: string,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`${label} timeout after ${timeoutMs}ms`)),
      timeoutMs,
    );
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * Retry wrapper for Neon HTTP SQL queries. All public reads are idempotent
 * SELECTs against `vacancies` and `aggregates`, so safe to retry blindly.
 *
 * The @neondatabase/serverless fetch client has no built-in retry — a single
 * transient 429/5xx or cold-start TLS stall would bubble up as a 503 to the
 * caller, even though a second attempt would likely succeed. KM audit
 * 2026-05-17 P1.
 *
 * Policy: 3 attempts, exp backoff (150ms, 300ms), per-attempt timeoutMs
 * (default 4s). Total worst-case latency ≈ 3 × 4s + 450ms ≈ 12.5s if every
 * attempt times out, but route maxDuration is hit first; in practice callers
 * see either a fast success or fast-fail with the underlying error so the
 * caller can fall through to the next degradation layer. Each retry logs to
 * Vercel console for flap-rate tracking.
 *
 * Cancellation: each attempt creates an AbortController and pipes the signal
 * via `fetchOptions.signal` to the underlying fetch in @neondatabase/serverless.
 * On timeout we call `controller.abort()` so the in-flight HTTP request stops
 * holding sockets/event-loop slots — without this, a slow Neon query would
 * keep running long after the route returned, paying Vercel function ms.
 * Kimi audit 2026-05-25 P2-6.
 */
export async function neonQuery<T = unknown>(
  sqlString: string,
  params: unknown[] = [],
  options: { retries?: number; baseDelayMs?: number; timeoutMs?: number } = {},
): Promise<T> {
  const retries = options.retries ?? 3;
  const baseDelayMs = options.baseDelayMs ?? 150;
  const timeoutMs = options.timeoutMs ?? 4000;
  const sql = getNeon();

  let lastError: unknown;
  for (let attempt = 0; attempt < retries; attempt++) {
    // Dual cancellation: withTimeout guarantees the *await* unblocks at
    // timeoutMs regardless of underlying behavior; AbortController signals
    // @neondatabase/serverless to abort the in-flight fetch so the HTTP
    // socket and event-loop slot are released instead of leaking. Either
    // alone is incomplete: a signal-only design loses if the mock/runtime
    // ignores it; a race-only design leaks sockets.
    const controller = new AbortController();
    try {
      return (await withTimeout(
        sql.query(sqlString, params, {
          fetchOptions: { signal: controller.signal },
        }),
        timeoutMs,
        "neon query",
      )) as T;
    } catch (error) {
      // After timeout reject from withTimeout, abort the underlying fetch.
      controller.abort();
      lastError = error;
      if (attempt === retries - 1) break;
      const message = error instanceof Error ? error.message : String(error);
      const delay = baseDelayMs * Math.pow(2, attempt);
      console.warn(
        `[neon] query attempt ${attempt + 1}/${retries} failed, retrying in ${delay}ms: ${message}`,
      );
      await new Promise((resolve) => setTimeout(resolve, delay));
    }
  }
  throw lastError;
}
