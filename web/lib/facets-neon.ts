/**
 * Neon-backed facets — same summary/facets shape as DuckDB path.
 *
 * Same queries the DuckDB route runs, translated to Postgres:
 *   - `read_parquet($1)` → `vacancies` (Neon table)
 *   - `strftime(..., '%Y-%m-%dT%H:%M:%S.%fZ')` → `to_char(..., 'YYYY-MM-DDTHH24:MI:SS.MS"Z"') AT TIME ZONE 'UTC'`
 *   - `quantile_cont(col, p)` → `percentile_cont(p) WITHIN GROUP (ORDER BY col)`
 *
 * Used as a fallback after Blob snapshot + DuckDB+httpfs both fail
 * (Blob suspended OR network), and as the primary path when
 * `SEARCH_BACKEND=neon`.
 */
import { getNeon } from "@/lib/neon";

interface CountFacetRow {
  value: string;
  count: number;
}

interface SalaryRange {
  min: number | null;
  max: number | null;
  p50: number | null;
  p90: number | null;
  with_salary_pct: number;
}

const SOURCE_KEYS = ["hh", "telegram"] as const;

function countBySource(rows: CountFacetRow[]) {
  return SOURCE_KEYS.reduce<Record<string, number>>((acc, source) => {
    acc[source] = rows.find((row) => row.value === source)?.count ?? 0;
    return acc;
  }, {});
}

export async function fetchFacetsFromNeon() {
  const sql = getNeon();

  const [
    summaryRows,
    city,
    employer_name,
    remote_type,
    seniority,
    source,
    skills,
    salaryRows,
  ] = await Promise.all([
    sql`
      SELECT
        COUNT(*)::int AS total_vacancies,
        COUNT(DISTINCT city)::int AS unique_cities,
        COUNT(DISTINCT employer_name)::int AS unique_employers,
        (
          SELECT COUNT(DISTINCT skill)::int
          FROM vacancies, unnest(skills) AS skill
          WHERE skill IS NOT NULL AND skill <> ''
        ) AS unique_skills,
        to_char(MAX(last_seen_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS latest_seen_at
      FROM vacancies
    `,
    sql`
      SELECT city AS value, COUNT(*)::int AS count
      FROM vacancies
      WHERE city IS NOT NULL AND city <> ''
      GROUP BY city
      ORDER BY count DESC, value ASC
      LIMIT 50
    `,
    sql`
      SELECT employer_name AS value, COUNT(*)::int AS count
      FROM vacancies
      WHERE employer_name IS NOT NULL AND employer_name <> ''
      GROUP BY employer_name
      ORDER BY count DESC, value ASC
      LIMIT 30
    `,
    sql`
      WITH allowed(value) AS (VALUES ('office'), ('hybrid'), ('remote'), ('unknown')),
      counts AS (
        SELECT remote_type AS value, COUNT(*)::int AS count
        FROM vacancies
        GROUP BY remote_type
      )
      SELECT allowed.value, COALESCE(counts.count, 0)::int AS count
      FROM allowed
      LEFT JOIN counts USING (value)
      ORDER BY allowed.value
    `,
    sql`
      WITH allowed(value) AS (VALUES ('intern'), ('junior'), ('middle'), ('senior'), ('lead'), ('principal'), ('unknown')),
      counts AS (
        SELECT seniority AS value, COUNT(*)::int AS count
        FROM vacancies
        GROUP BY seniority
      )
      SELECT allowed.value, COALESCE(counts.count, 0)::int AS count
      FROM allowed
      LEFT JOIN counts USING (value)
      ORDER BY allowed.value
    `,
    sql`
      SELECT source AS value, COUNT(*)::int AS count
      FROM vacancies
      WHERE source IS NOT NULL AND source <> ''
      GROUP BY source
      ORDER BY count DESC, value ASC
    `,
    sql`
      SELECT skill AS value, COUNT(*)::int AS count
      FROM vacancies, unnest(skills) AS skill
      WHERE skill IS NOT NULL AND skill <> ''
      GROUP BY skill
      ORDER BY count DESC, value ASC
      LIMIT 50
    `,
    sql`
      SELECT
        MIN(salary_rub_min)::int AS min,
        MAX(salary_rub_max)::int AS max,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY salary_rub_min)::int AS p50,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY salary_rub_min)::int AS p90,
        COALESCE(
          (COUNT(*) FILTER (WHERE salary_disclosed))::float * 100.0 / NULLIF(COUNT(*), 0),
          0
        )::float AS with_salary_pct
      FROM vacancies
    `,
  ]);

  const sourceFacet = source as unknown as CountFacetRow[];
  const summaryArr = summaryRows as unknown as Array<Record<string, unknown>>;
  const summary = summaryArr[0] ?? {};
  const salaryArr = salaryRows as unknown as Array<SalaryRange>;

  return {
    summary: {
      ...summary,
      source_breakdown: countBySource(sourceFacet),
    },
    facets: {
      city: city as unknown as CountFacetRow[],
      employer_name: employer_name as unknown as CountFacetRow[],
      remote_type: remote_type as unknown as CountFacetRow[],
      seniority: seniority as unknown as CountFacetRow[],
      source: sourceFacet,
      skills: skills as unknown as CountFacetRow[],
      salary_range: salaryArr[0] ?? {
        min: null,
        max: null,
        p50: null,
        p90: null,
        with_salary_pct: 0,
      },
    },
    refreshed_at: new Date().toISOString(),
  };
}
