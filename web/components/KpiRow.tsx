"use client";
import { formatInt } from "@/lib/dashboard-format";
import type { FacetsResponse } from "@/lib/dashboard-types";

export function KpiRow({ facets }: { facets: FacetsResponse | null }) {
  return (
    <div className="kpi-row">
      <article className={`kpi-card${facets ? "" : " kpi-card--skeleton"}`}>
        <div className="kpi-card__label">IT-вакансий</div>
        <div className="kpi-card__value">
          {facets ? formatInt(facets.summary.total_vacancies) : ""}
        </div>
        <div className="kpi-card__hint kpi-card__hint--neutral">
          {facets
            ? `${formatInt(facets.summary.source_breakdown.hh ?? 0)} hh.ru · ${formatInt(facets.summary.source_breakdown.telegram ?? 0)} telegram`
            : "—"}
        </div>
      </article>
      <article className={`kpi-card${facets ? "" : " kpi-card--skeleton"}`}>
        <div className="kpi-card__label">медиана зарплаты</div>
        <div className="kpi-card__value">
          {facets?.facets.salary_range.p50
            ? `${formatInt(Math.round(facets.facets.salary_range.p50 / 1000))}к ₽`
            : ""}
        </div>
        <div className="kpi-card__hint kpi-card__hint--neutral">
          {facets?.facets.salary_range.p90
            ? `p90: ${formatInt(Math.round(facets.facets.salary_range.p90 / 1000))}к`
            : "—"}
        </div>
      </article>
      <article className={`kpi-card${facets ? "" : " kpi-card--skeleton"}`}>
        <div className="kpi-card__label">с открытой ЗП</div>
        <div className="kpi-card__value">
          {facets ? `${Math.round(facets.facets.salary_range.with_salary_pct)}%` : ""}
        </div>
        <div className="kpi-card__hint kpi-card__hint--neutral">
          {facets ? `${formatInt(facets.summary.unique_employers)} работодателей` : "—"}
        </div>
      </article>
      <article className={`kpi-card${facets ? "" : " kpi-card--skeleton"}`}>
        <div className="kpi-card__label">география</div>
        <div className="kpi-card__value">
          {facets ? formatInt(facets.summary.unique_cities) : ""}
        </div>
        <div className="kpi-card__hint kpi-card__hint--neutral">
          {facets ? `городов · ${formatInt(facets.summary.unique_skills)} навыков` : "—"}
        </div>
      </article>
    </div>
  );
}
