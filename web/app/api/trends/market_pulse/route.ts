import { NextResponse } from "next/server";
import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";
import { WEEKLY_MARKET_PULSE_PARQUET, runQuery } from "@/lib/duckdb";
import { fetchSnapshot } from "@/lib/snapshots";

export const revalidate = 3600;
export const runtime = "nodejs";

type TrendsBody = { rows: unknown[]; refreshed_at: string };

export async function GET() {
  const snapshot = await fetchSnapshot<TrendsBody>("trends/market_pulse.json", {
    revalidateSeconds: revalidate,
  });
  if (snapshot) {
    return NextResponse.json(snapshot);
  }
  const fromNeon = await fetchAggregateFromNeon<TrendsBody>("trends/market_pulse");
  if (fromNeon) {
    return NextResponse.json(fromNeon);
  }
  try {
    const rows = await runQuery(
      `SELECT
         strftime(date, '%Y-%m-%d') AS date,
         total_active::INT AS total_active,
         new_vacancies::INT AS new_vacancies,
         closed_vacancies::INT AS closed_vacancies,
         salary_disclosure_rate::DOUBLE AS salary_disclosure_rate,
         median_active_age_days::DOUBLE AS median_active_age_days
       FROM read_parquet($1)
       ORDER BY date`,
      [WEEKLY_MARKET_PULSE_PARQUET],
    );
    return NextResponse.json({ rows, refreshed_at: new Date().toISOString() });
  } catch (error) {
    console.error("trends/market_pulse failed", error);
    return NextResponse.json(
      { rows: [], error: "trends_unavailable", refreshed_at: new Date().toISOString() },
      { status: 500 },
    );
  }
}
