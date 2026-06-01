import "server-only";
import { DuckDBInstance } from "@duckdb/node-api";
import path from "node:path";

import { withTimeout } from "@/lib/neon";

let instance: DuckDBInstance | null = null;
let instancePromise: Promise<DuckDBInstance> | null = null;

const EXTENSION_DIRECTORY = (
  process.env.DUCKDB_EXTENSION_DIRECTORY ??
  (process.env.VERCEL ? "/tmp/duckdb-extensions" : path.join(process.cwd(), ".duckdb-extensions"))
).replace(/\\/g, "/");

// Memory ceiling: Vercel Hobby function gets ~1024 MB total, Pro ~3008 MB.
// Override via env when deploying to a larger plan; default 768 MB leaves
// headroom for the Node runtime (~200-300 MB) on Hobby and is generous for
// the 12 MB slim_active.parquet workload (peak DuckDB observed ~150 MB).
// Kimi audit 2026-05-25 P2-7.
const MEMORY_LIMIT = process.env.DUCKDB_MEMORY_LIMIT ?? "768MB";

export async function getDuckDB() {
  const readyInstance = instance ?? (await createDuckDBInstance());
  const conn = await readyInstance.connect();
  return { instance: readyInstance, conn };
}

async function createDuckDBInstance() {
  if (instancePromise) {
    return instancePromise;
  }

  instancePromise = initializeDuckDBInstance();

  try {
    instance = await instancePromise;
    return instance;
  } finally {
    instancePromise = null;
  }
}

async function initializeDuckDBInstance() {
  let created: DuckDBInstance | null = null;

  try {
    // memory_limit ceiling honors the DUCKDB_MEMORY_LIMIT env (defaults to
    // 768MB — Vercel Hobby-safe). Previously 1.2GB hardcoded would exceed
    // the container limit → SIGKILL OOM that the try/catch around runQuery()
    // cannot intercept (uncatchable in V8). KM audit 2026-05-17 P1; env
    // override surface from Kimi audit 2026-05-25 P2-7.
    created = await DuckDBInstance.create(":memory:", {
      "memory_limit": MEMORY_LIMIT,
      "threads": "1",
      "extension_directory": EXTENSION_DIRECTORY,
      "autoinstall_known_extensions": "true",
      "autoload_known_extensions": "true",
    });

    const conn = await created.connect();

    try {
      // httpfs allows range-request reads over public Blob URLs.
      // On Vercel Node runtime this works; on Edge it will fail.
      await conn.run("INSTALL httpfs;");
      await conn.run("LOAD httpfs;");
      await conn.run("INSTALL fts;");
      await conn.run("LOAD fts;");

      // Reduce HTTPFS request chattiness: increase timeout and allow a bit more parallelism.
      await conn.run("SET http_timeout = 30000;");
    } finally {
      conn.closeSync();
    }

    return created;
  } catch (error) {
    instance = null;
    throw error;
  }
}

export async function runQuery(
  sql: string,
  params?: unknown[],
  options: { timeoutMs?: number } = {},
): Promise<unknown[]> {
  // 8s default leaves ~2s buffer below maxDuration=10 on /api/search and
  // plenty of room on the 60s facets/trends routes. DuckDB ops on the 12 MB
  // slim_active parquet typically complete in <1s; a >8s run signals either
  // Blob slowness or a query plan regression that should bubble up so the
  // route can return 503 (and let the caller cascade). KM audit 2026-05-17 P1.
  const timeoutMs = options.timeoutMs ?? 8000;
  const { conn } = await getDuckDB();

  try {
    const run = (async () => {
      const reader = await conn.runAndReadAll(sql, params as never);
      return reader.getRowObjectsJson() as unknown[];
    })();
    return await withTimeout(run, timeoutMs, "duckdb query");
  } finally {
    conn.closeSync();
  }
}

/**
 * Public artifact base URL injected via env (no trailing slash).
 *
 * Server-only file (DuckDB Node bindings); single env contract is
 * `BLOB_PUBLIC_BASE_URL` (matches Python publish path в src/cli.py).
 * When local/dev env still lacks it after the HF migration, fall back to the
 * active public mirror instead of stale Vercel Blob public URLs.
 */
const DEFAULT_PUBLIC_ARTIFACT_BASE =
  "https://huggingface.co/datasets/your-org/vacancyradar-data/resolve/main";

function nonEmptyEnv(name: string): string | undefined {
  const value = process.env[name]?.trim();
  return value ? value : undefined;
}

export const BLOB_BASE =
  nonEmptyEnv("BLOB_PUBLIC_BASE_URL") ??
  DEFAULT_PUBLIC_ARTIFACT_BASE;

/** Helpers to build Parquet paths */
export const ACTIVE_PARQUET = `${BLOB_BASE}/slim/active.parquet`;
export const WEEKLY_MARKET_PULSE_PARQUET = `${BLOB_BASE}/agg/weekly_market_pulse.parquet`;
export const WEEKLY_EMPLOYER_TOP_PARQUET = `${BLOB_BASE}/agg/weekly_employer_top.parquet`;
export const WEEKLY_SKILL_VELOCITY_PARQUET = `${BLOB_BASE}/agg/weekly_skill_velocity.parquet`;
export const WEEKLY_ROLE_SALARY_PARQUET = `${BLOB_BASE}/agg/weekly_role_salary.parquet`;
