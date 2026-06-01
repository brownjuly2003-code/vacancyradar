"use client";
import { useEffect } from "react";

import { formatDate, formatSalary, safeHref } from "@/lib/dashboard-format";
import type { SearchRow } from "@/lib/dashboard-types";

export function DetailPanel({
  row,
  onClose,
}: {
  row: SearchRow;
  onClose: () => void;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const href = safeHref(row.source_url);

  return (
    <>
      <div
        className="detail-backdrop"
        onClick={onClose}
        aria-hidden="true"
      />
      <aside
        className="detail-panel"
        role="dialog"
        aria-label="Детали вакансии"
        aria-modal="true"
      >
        <header className="detail-panel__head">
          <h2 className="detail-panel__title">{row.title}</h2>
          <button
            type="button"
            className="detail-panel__close"
            onClick={onClose}
            aria-label="Закрыть"
          >
            ✕
          </button>
        </header>
        <div className="detail-panel__body">
          <dl className="detail-panel__facts">
            <div>
              <dt>Работодатель</dt>
              <dd>{row.employer_name ?? "—"}</dd>
            </div>
            <div>
              <dt>Город</dt>
              <dd>
                {row.city ?? "—"}
                {row.region ? <span className="muted"> · {row.region}</span> : null}
              </dd>
            </div>
            <div>
              <dt>Зарплата</dt>
              <dd className="mono">{formatSalary(row)}</dd>
            </div>
            <div>
              <dt>Формат</dt>
              <dd>{row.remote_type}</dd>
            </div>
            <div>
              <dt>Грейд</dt>
              <dd>{row.seniority}</dd>
            </div>
            <div>
              <dt>Опубликовано</dt>
              <dd className="mono">{formatDate(row.posted_at)}</dd>
            </div>
          </dl>

          {row.skills && row.skills.length > 0 ? (
            <section className="detail-panel__section">
              <h3>Навыки</h3>
              <div className="chip-list">
                {row.skills.map((skill) => (
                  <span className="chip" key={skill}>
                    {skill}
                  </span>
                ))}
              </div>
            </section>
          ) : null}

          {row.description_teaser ? (
            <section className="detail-panel__section">
              <h3>Описание</h3>
              <p className="detail-panel__desc">{row.description_teaser}</p>
            </section>
          ) : null}

          {href ? (
            <a
              className="detail-panel__external"
              href={href}
              target="_blank"
              rel="noopener noreferrer"
            >
              Открыть на источнике ↗
            </a>
          ) : null}
        </div>
      </aside>
    </>
  );
}
