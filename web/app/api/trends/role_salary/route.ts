import { NextResponse } from "next/server";
import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";
import { WEEKLY_ROLE_SALARY_PARQUET, runQuery } from "@/lib/duckdb";
import { fetchSnapshot } from "@/lib/snapshots";

export const revalidate = 3600;
export const runtime = "nodejs";

type TrendsBody = { rows: unknown[]; refreshed_at: string };

export async function GET() {
  const snapshot = await fetchSnapshot<TrendsBody>("trends/role_salary.json", {
    revalidateSeconds: revalidate,
  });
  if (snapshot) {
    return NextResponse.json(snapshot);
  }
  const fromNeon = await fetchAggregateFromNeon<TrendsBody>("trends/role_salary");
  if (fromNeon) {
    return NextResponse.json(fromNeon);
  }
  try {
    // Frontend показывает national rollup (city IS NULL); фильтруем в SQL
    // до LIMIT, иначе при большом числе city-level строк national может
    // не попасть в первые 200 → пустой график.
    const rows = await runQuery(
      `SELECT
         strftime(week_start, '%Y-%m-%d') AS week_start,
         role_canonical,
         seniority,
         CAST(NULL AS VARCHAR) AS city,
         n_vacancies::INT AS n_vacancies,
         salary_rub_p25::INT AS salary_rub_p25,
         salary_rub_median::INT AS salary_rub_median,
         salary_rub_p75::INT AS salary_rub_p75
       FROM read_parquet($1)
       WHERE city IS NULL
       ORDER BY week_start DESC, salary_rub_median DESC
       LIMIT 200`,
      [WEEKLY_ROLE_SALARY_PARQUET],
    );
    return NextResponse.json({ rows, refreshed_at: new Date().toISOString() });
  } catch (error) {
    console.error("trends/role_salary failed", error);
    return NextResponse.json(
      { rows: [], error: "trends_unavailable", refreshed_at: new Date().toISOString() },
      { status: 500 },
    );
  }
}
