import { NextResponse } from "next/server";
import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";
import { ACTIVE_PARQUET, runQuery } from "@/lib/duckdb";
import { fetchFacetsFromNeon } from "@/lib/facets-neon";
import { isNeonConfigured } from "@/lib/neon";
import { fetchSnapshot } from "@/lib/snapshots";

// Data refreshes once daily (~06:00 UTC) via daily_refresh.ps1. 3600 = 1h
// balances quick post-refresh propagation with not regenerating 12×/h for no
// reason. KM re-audit 2026-05-17 P2 (300s was misaligned with 24h cadence).
export const revalidate = 3600;
export const runtime = "nodejs";

type CountFacetRow = {
  value: string;
  count: number;
};

type SalaryRange = {
  min: number | null;
  max: number | null;
  p50: number | null;
  p90: number | null;
  with_salary_pct: number;
};

type FacetsBody = {
  summary: Record<string, unknown>;
  facets: Record<string, unknown>;
  refreshed_at: string;
};

const SOURCE_KEYS = ["hh", "telegram"] as const;

function countBySource(rows: CountFacetRow[]) {
  return SOURCE_KEYS.reduce<Record<string, number>>((acc, source) => {
    acc[source] = rows.find((row) => row.value === source)?.count ?? 0;
    return acc;
  }, {});
}

// Build-time pre-rendering catches the GET response (including error responses).
// Without this try/catch a DuckDB+httpfs failure (e.g. Blob 403 store_suspended)
// surfaces as an unhandled exception → Next.js fails the entire production build.
// With the wrapper, an unreachable Blob yields a 503 payload whose shape matches
// the success response so the client doesn't crash; ISR will regenerate fresh
// data on the first runtime request that succeeds.
export async function GET() {
  // Fast path: pre-aggregated JSON snapshot in Blob (~30 KB vs 12 MB parquet).
  const snapshot = await fetchSnapshot<FacetsBody>("facets.json", { revalidateSeconds: revalidate });
  if (snapshot) {
    return NextResponse.json(snapshot);
  }

  // Second-fast path: aggregates table in Neon (same JSON the snapshot
  // route serves, upserted by `vradar publish snapshots`). One row read.
  const fromAggregates = await fetchAggregateFromNeon<FacetsBody>("facets");
  if (fromAggregates) {
    return NextResponse.json(fromAggregates);
  }

  // Live recompute against the Neon vacancies table — used when the
  // pre-aggregated row hasn't been pushed yet but `vacancies` is fresh.
  if (isNeonConfigured()) {
    try {
      const body = await fetchFacetsFromNeon();
      return NextResponse.json(body);
    } catch (error) {
      console.error("[facets] Neon query failed", error);
    }
  }

  // Last fallback: live DuckDB+httpfs over the Blob parquet. Used when both
  // the snapshot and Neon are unavailable (e.g. local dev without Neon creds).
  try {
    return await fetchFacets();
  } catch (error) {
    console.error("[facets] DuckDB query failed", error);
    return NextResponse.json(
      {
        summary: {
          total_vacancies: 0,
          unique_cities: 0,
          unique_employers: 0,
          unique_skills: 0,
          latest_seen_at: null,
          source_breakdown: countBySource([]),
        },
        facets: {
          city: [],
          employer_name: [],
          remote_type: [],
          seniority: [],
          source: [],
          skills: [],
          salary_range: { min: null, max: null, p50: null, p90: null, with_salary_pct: 0 },
        },
        refreshed_at: new Date().toISOString(),
        error: "facets_unavailable",
        detail: "DuckDB+httpfs query error — see server logs.",
      },
      { status: 503 },
    );
  }
}

async function fetchFacets() {
  const [summaryRows, city, employer_name, remote_type, seniority, source, skills, salaryRows] =
    await Promise.all([
      runQuery(
        `SELECT
           count(*)::INT AS total_vacancies,
           count(DISTINCT city)::INT AS unique_cities,
           count(DISTINCT employer_name)::INT AS unique_employers,
           (
             SELECT count(DISTINCT skill)::INT
             FROM read_parquet($1), unnest(skills) AS t(skill)
             WHERE skill IS NOT NULL AND skill <> ''
           ) AS unique_skills,
           strftime(max(last_seen_at), '%Y-%m-%dT%H:%M:%S.%fZ') AS latest_seen_at
         FROM read_parquet($1)`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `SELECT city AS value, count(*)::INT AS count
         FROM read_parquet($1)
         WHERE city IS NOT NULL AND city <> ''
         GROUP BY city
         ORDER BY count DESC, value ASC
         LIMIT 50`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `SELECT employer_name AS value, count(*)::INT AS count
         FROM read_parquet($1)
         WHERE employer_name IS NOT NULL AND employer_name <> ''
         GROUP BY employer_name
         ORDER BY count DESC, value ASC
         LIMIT 30`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `WITH allowed(value) AS (
           VALUES ('office'), ('hybrid'), ('remote'), ('unknown')
         ),
         counts AS (
           SELECT remote_type AS value, count(*)::INT AS count
           FROM read_parquet($1)
           GROUP BY remote_type
         )
         SELECT allowed.value, coalesce(counts.count, 0)::INT AS count
         FROM allowed
         LEFT JOIN counts USING (value)
         ORDER BY allowed.value`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `WITH allowed(value) AS (
           VALUES ('intern'), ('junior'), ('middle'), ('senior'), ('lead'), ('principal'), ('unknown')
         ),
         counts AS (
           SELECT seniority AS value, count(*)::INT AS count
           FROM read_parquet($1)
           GROUP BY seniority
         )
         SELECT allowed.value, coalesce(counts.count, 0)::INT AS count
         FROM allowed
         LEFT JOIN counts USING (value)
         ORDER BY allowed.value`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `SELECT source AS value, count(*)::INT AS count
         FROM read_parquet($1)
         WHERE source IS NOT NULL AND source <> ''
         GROUP BY source
         ORDER BY count DESC, value ASC`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `SELECT skill AS value, count(*)::INT AS count
         FROM read_parquet($1), unnest(skills) AS t(skill)
         WHERE skill IS NOT NULL AND skill <> ''
         GROUP BY skill
         ORDER BY count DESC, value ASC
         LIMIT 50`,
        [ACTIVE_PARQUET],
      ),
      runQuery(
        `SELECT
           min(salary_rub_min)::INT AS min,
           max(salary_rub_max)::INT AS max,
           quantile_cont(salary_rub_min, 0.5)::INT AS p50,
           quantile_cont(salary_rub_min, 0.9)::INT AS p90,
           coalesce(
             (count(*) FILTER (WHERE salary_disclosed))::DOUBLE * 100.0 / nullif(count(*), 0),
             0
           )::DOUBLE AS with_salary_pct
         FROM read_parquet($1)`,
        [ACTIVE_PARQUET],
      ),
    ]);

  const sourceFacet = source as CountFacetRow[];

  return NextResponse.json({
    summary: {
      ...(summaryRows[0] as object),
      source_breakdown: countBySource(sourceFacet),
    },
    facets: {
      city,
      employer_name,
      remote_type,
      seniority,
      source: sourceFacet,
      skills,
      salary_range: (salaryRows[0] as SalaryRange | undefined) ?? {
        min: null,
        max: null,
        p50: null,
        p90: null,
        with_salary_pct: 0,
      },
    },
    refreshed_at: new Date().toISOString(),
  });
}
