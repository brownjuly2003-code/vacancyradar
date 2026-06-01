// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { KpiRow } from "@/components/KpiRow";
import type { FacetsResponse } from "@/lib/dashboard-types";

const FACETS: FacetsResponse = {
  summary: {
    total_vacancies: 12345,
    unique_cities: 47,
    unique_employers: 980,
    unique_skills: 288,
    latest_seen_at: "2026-05-24T05:30:00Z",
    source_breakdown: { hh: 7800, telegram: 4545 },
  },
  facets: {
    city: [],
    employer_name: [],
    remote_type: [],
    seniority: [],
    source: [],
    skills: [],
    salary_range: {
      min: 30000,
      max: 5000000,
      p50: 200000,
      p90: 450000,
      with_salary_pct: 47.5,
    },
  },
  refreshed_at: "2026-05-24T05:30:00Z",
};

describe("KpiRow", () => {
  it("renders four kpi cards", () => {
    const { container } = render(<KpiRow facets={FACETS} />);
    expect(container.querySelectorAll(".kpi-card")).toHaveLength(4);
  });

  it("formats total vacancies with thousands separator", () => {
    render(<KpiRow facets={FACETS} />);
    // formatInt uses ru-RU locale → non-breaking space separator
    expect(screen.getByText(/12.?345/)).toBeInTheDocument();
  });

  it("formats salary p50 in thousands of rubles", () => {
    render(<KpiRow facets={FACETS} />);
    // 200000 / 1000 = 200к
    expect(screen.getByText(/200к/)).toBeInTheDocument();
    expect(screen.getByText(/p90: 450к/)).toBeInTheDocument();
  });

  it("renders disclosure rate as integer percent", () => {
    render(<KpiRow facets={FACETS} />);
    // Math.round(47.5) = 48
    expect(screen.getByText("48%")).toBeInTheDocument();
  });

  it("renders source breakdown", () => {
    render(<KpiRow facets={FACETS} />);
    expect(screen.getByText(/7.?800 hh\.ru/)).toBeInTheDocument();
    expect(screen.getByText(/4.?545 telegram/)).toBeInTheDocument();
  });

  it("renders skeleton state when facets is null", () => {
    const { container } = render(<KpiRow facets={null} />);
    const skeletons = container.querySelectorAll(".kpi-card--skeleton");
    expect(skeletons).toHaveLength(4);
  });

  it("treats missing p50/p90 as dash (graceful degradation)", () => {
    const noSalary: FacetsResponse = {
      ...FACETS,
      facets: {
        ...FACETS.facets,
        salary_range: { min: null, max: null, p50: null, p90: null, with_salary_pct: 0 },
      },
    };
    render(<KpiRow facets={noSalary} />);
    // p90 hint shows dash when null
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
