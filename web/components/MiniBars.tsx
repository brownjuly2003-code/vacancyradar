/**
 * Tiny horizontal bar chart used inside facet panels (`source` breakdown).
 * Pure presentational — gets a pre-aggregated `{value, count}[]` and just
 * renders it via recharts. Extracted from `app/page.tsx` 2026-05-16.
 *
 * Bar color is the dashboard accent (#2563eb) — kept inline rather than a CSS
 * variable because recharts paints SVG attributes directly and doesn't read
 * `var(...)` references at render time.
 */
import { Bar, BarChart, ResponsiveContainer, XAxis, YAxis } from "recharts";

import type { CountFacet } from "@/lib/dashboard-types";

export function MiniBars({ data }: { data: CountFacet[] }) {
  if (data.length === 0) {
    return null;
  }

  return (
    <div className="mini-chart" aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 4, bottom: 4, left: 0 }}>
          <XAxis type="number" hide />
          <YAxis dataKey="value" type="category" hide />
          <Bar dataKey="count" fill="#2563eb" radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
