"use client";
import {
  formatRemoteType,
  formatRelative,
  formatSalary,
  formatSeniority,
  formatSource,
} from "@/lib/dashboard-format";
import type { SearchRow } from "@/lib/dashboard-types";

export function ResultsCards({
  rows,
  onRowClick,
}: {
  rows: SearchRow[];
  onRowClick: (row: SearchRow) => void;
}) {
  return (
    <div className="cards">
      {rows.map((row) => (
        <article
          className="vacancy-card"
          key={row.vacancy_id}
          onClick={() => onRowClick(row)}
          role="button"
          tabIndex={0}
          aria-label={`${row.title}${row.employer_name ? `, ${row.employer_name}` : ""}`}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              onRowClick(row);
            }
          }}
        >
          <div className="vacancy-card__top">
            <span className="source-badge" data-source={row.source}>
              {formatSource(row.source)}
            </span>
            <span className="vacancy-card__date">{formatRelative(row.posted_at)}</span>
          </div>
          <h2 className="vacancy-card__title">
            <span className="vacancy-link">{row.title}</span>
          </h2>
          <p className="vacancy-card__meta">
            {row.employer_name ?? "—"} · {row.city ?? "—"}
          </p>
          <div className="vacancy-card__row">
            <span className="mono">{formatSalary(row)}</span>
            <span className="vacancy-card__pills">
              <span className="pill">{formatRemoteType(row.remote_type)}</span>
              <span className="pill">{formatSeniority(row.seniority)}</span>
            </span>
          </div>
          <p className="vacancy-card__snippet">{row.description_teaser ?? "—"}</p>
        </article>
      ))}
    </div>
  );
}
