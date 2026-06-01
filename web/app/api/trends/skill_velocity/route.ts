import { NextResponse } from "next/server";
import { fetchAggregateFromNeon } from "@/lib/aggregates-neon";
import { WEEKLY_SKILL_VELOCITY_PARQUET, runQuery } from "@/lib/duckdb";
import { fetchSnapshot } from "@/lib/snapshots";

export const revalidate = 3600;
export const runtime = "nodejs";

type TrendsBody = { rows: unknown[]; refreshed_at: string };

export async function GET() {
  const snapshot = await fetchSnapshot<TrendsBody>("trends/skill_velocity.json", {
    revalidateSeconds: revalidate,
  });
  if (snapshot) {
    return NextResponse.json(snapshot);
  }
  const fromNeon = await fetchAggregateFromNeon<TrendsBody>("trends/skill_velocity");
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
         skill,
         mentions_this_week::INT AS mentions_this_week,
         mentions_prev_week::INT AS mentions_prev_week,
         delta_pct::DOUBLE AS delta_pct,
         rank_this_week::INT AS rank_this_week
       FROM read_parquet($1)
       WHERE week_start = (SELECT w FROM latest)
       ORDER BY rank_this_week
       LIMIT 30`,
      [WEEKLY_SKILL_VELOCITY_PARQUET],
    );
    return NextResponse.json({ rows, refreshed_at: new Date().toISOString() });
  } catch (error) {
    console.error("trends/skill_velocity failed", error);
    return NextResponse.json(
      { rows: [], error: "trends_unavailable", refreshed_at: new Date().toISOString() },
      { status: 500 },
    );
  }
}
