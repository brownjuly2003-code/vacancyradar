"use client";
import { formatDate, formatRemoteType, formatSalary, formatSeniority } from "@/lib/dashboard-format";
import type { SearchRow } from "@/lib/dashboard-types";

export function ResultsTable({
  rows,
  onRowClick,
}: {
  rows: SearchRow[];
  onRowClick: (row: SearchRow) => void;
}) {
  return (
    <div className="table-wrap">
      <table className="vacancy-table">
        <thead>
          <tr>
            <th>Вакансия</th>
            <th>Компания</th>
            <th>Город</th>
            <th>Зарплата</th>
            <th>Формат</th>
            <th>Грейд</th>
            <th>Опубликована</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.vacancy_id}
              className="vacancy-row"
              data-source={row.source}
              onClick={() => onRowClick(row)}
            >
              <td>
                <span className="vacancy-link">{row.title}</span>
              </td>
              <td>{row.employer_name ?? "—"}</td>
              <td>{row.city ?? "—"}</td>
              <td>{formatSalary(row)}</td>
              <td>{formatRemoteType(row.remote_type)}</td>
              <td>{formatSeniority(row.seniority)}</td>
              <td>{formatDate(row.posted_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
