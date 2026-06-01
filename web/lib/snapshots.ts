/**
 * Fetch pre-aggregated JSON snapshots from Vercel Blob.
 *
 * Snapshots are ~30 KB JSON files produced by `vradar publish snapshots`
 * that mirror the API response shape of /api/facets and /api/trends/*.
 * Reading them costs ~400x less egress than the DuckDB+httpfs path
 * (which has to download the full 12 MB slim/active.parquet on every
 * cold-start). When a snapshot is missing or stale, callers fall back to
 * the original DuckDB path so a missing snapshot is never an outage.
 */

import { BLOB_BASE } from "@/lib/duckdb";

export const SNAPSHOTS_BASE = `${BLOB_BASE}/slim/snapshots`;

/**
 * GET a JSON snapshot from Vercel Blob.
 *
 * Returns `null` on any non-200 (Blob suspended, snapshot not yet published,
 * transient 5xx). Callers handle null by falling back to the DuckDB path.
 *
 * Snapshot fetches use a positive Next.js revalidate value so production
 * builds do not downgrade these route handlers to dynamic usage just because
 * the snapshot layer is checked before Neon/DuckDB fallbacks.
 */
export async function fetchSnapshot<T>(
  pathname: string,
  options: { timeoutMs?: number; revalidateSeconds?: number } = {},
): Promise<T | null> {
  if (!BLOB_BASE) {
    return null;
  }
  // 2s default: Blob JSON is ~30 KB, normally < 200ms. A slow Blob layer must
  // not eat the route's maxDuration budget — without this timeout, a hung
  // edge node would let Vercel SIGKILL the function before the Neon/DuckDB
  // fallbacks could execute. KM audit 2026-05-17 P1.
  const timeoutMs = options.timeoutMs ?? 2000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${SNAPSHOTS_BASE}/${pathname}`, {
      next: { revalidate: options.revalidateSeconds ?? 300 },
      signal: controller.signal,
    });
    if (!response.ok) {
      return null;
    }
    return (await response.json()) as T;
  } catch (error) {
    if ((error as { name?: string }).name === "AbortError") {
      console.warn(`[snapshots] timeout after ${timeoutMs}ms for ${pathname}`);
    } else {
      console.warn(`[snapshots] fetch failed for ${pathname}`, error);
    }
    return null;
  } finally {
    clearTimeout(timer);
  }
}
