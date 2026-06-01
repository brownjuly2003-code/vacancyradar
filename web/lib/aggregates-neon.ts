/**
 * Neon-backed `aggregates` table fallback.
 *
 * `aggregates.name → payload` is populated by `vradar publish snapshots`
 * (Python `_publish_snapshots` in src/cli.py). Same JSON payload that the
 * route returns, byte-for-byte.
 *
 * Used as a fallback after the Blob JSON snapshot fails, so /api/trends/*
 * survives a suspended Blob store.
 */
import { getNeon, isNeonConfigured } from "@/lib/neon";

interface AggregateRow {
  payload: unknown;
  schema_version: number;
  refreshed_at: string | null;
}

/**
 * Must stay in sync with `CURRENT_AGGREGATE_SCHEMA_VERSION` in
 * `src/publish/snapshots.py`. Bumped whenever the JSON shape of any
 * snapshot payload changes. Routes reject rows whose version doesn't
 * match and cascade to the next layer. CX audit 2026-05-17 P2.
 */
export const EXPECTED_AGGREGATE_SCHEMA_VERSION = 1;

/**
 * Reject aggregate rows older than this. Daily refresh cadence is 24h;
 * 36h allows for one missed run before falling through to live recompute,
 * which keeps exec brief stats honest if `publish snapshots` is broken
 * for a day or two while `publish neon` (vacancies) keeps succeeding.
 * KM re-audit 2026-05-17 P1 (aggregates-neon stale TTL).
 */
const DEFAULT_MAX_AGE_HOURS = 36;

export async function fetchAggregateFromNeon<T>(
  name: string,
  { maxAgeHours = DEFAULT_MAX_AGE_HOURS }: { maxAgeHours?: number } = {},
): Promise<T | null> {
  if (!isNeonConfigured()) return null;
  try {
    const sql = getNeon();
    const rows = (await sql`
      SELECT
        payload,
        COALESCE((to_jsonb(aggregates)->>'schema_version')::int, 1) AS schema_version,
        to_char(refreshed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS refreshed_at
      FROM aggregates
      WHERE name = ${name}
      LIMIT 1
    `) as unknown as AggregateRow[];
    if (rows.length === 0) return null;
    if (rows[0].schema_version !== EXPECTED_AGGREGATE_SCHEMA_VERSION) {
      console.warn(
        `[aggregates-neon] schema_version mismatch for ${name}: ` +
          `got ${rows[0].schema_version}, expected ${EXPECTED_AGGREGATE_SCHEMA_VERSION}. ` +
          `Falling through to next layer.`,
      );
      return null;
    }
    const refreshedAt = rows[0].refreshed_at;
    if (refreshedAt) {
      const ageMs = Date.now() - Date.parse(refreshedAt);
      if (Number.isFinite(ageMs) && ageMs > maxAgeHours * 3_600_000) {
        console.warn(
          `[aggregates-neon] row for ${name} is stale: ` +
            `refreshed_at=${refreshedAt}, age=${Math.round(ageMs / 3_600_000)}h, ` +
            `maxAge=${maxAgeHours}h. Falling through to next layer.`,
        );
        return null;
      }
    }
    return rows[0].payload as T;
  } catch (error) {
    console.error(`[aggregates-neon] fetch failed for ${name}`, error);
    return null;
  }
}
