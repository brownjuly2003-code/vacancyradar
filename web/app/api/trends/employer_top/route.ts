import { NextResponse } from "next/server";
import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";
import { WEEKLY_EMPLOYER_TOP_PARQUET, runQuery } from "@/lib/duckdb";
import { fetchSnapshot } from "@/lib/snapshots";

export const revalidate = 3600;
export const runtime = "nodejs";

type TrendsBody = { rows: unknown[]; refreshed_at: string };

export async function GET() {
  const snapshot = await fetchSnapshot<TrendsBody>("trends/employer_top.json", {
    revalidateSeconds: revalidate,
  });
  if (snapshot) {
    return NextResponse.json(snapshot);
  }
  const fromNeon = await fetchAggregateFromNeon<TrendsBody>("trends/employer_top");
  if (fromNeon) {
    return NextResponse.json(fromNeon);
  }
  try {
    const rows = await runQuery(
      `WITH latest AS (
         SELECT max(week_start) AS w FROM read_parquet($1)
       )
       SELECT
         strftime(week_start, '%Y-%m-%d') AS week_start,
         employer_id,
         employer_name,
         new_vacancies::INT AS new_vacancies,
         closed_vacancies::INT AS closed_vacancies,
         disclosure_rate::DOUBLE AS disclosure_rate
       FROM read_parquet($1)
       WHERE week_start = (SELECT w FROM latest)
       ORDER BY new_vacancies DESC
       LIMIT 25`,
      [WEEKLY_EMPLOYER_TOP_PARQUET],
    );
    return NextResponse.json({ rows, refreshed_at: new Date().toISOString() });
  } catch (error) {
    console.error("trends/employer_top failed", error);
    return NextResponse.json(
      { rows: [], error: "trends_unavailable", refreshed_at: new Date().toISOString() },
      { status: 500 },
    );
  }
}
