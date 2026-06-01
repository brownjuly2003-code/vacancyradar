/**
 * Display formatters and URL builder for the live dashboard.
 *
 * Extracted from `app/page.tsx` (2026-05-16). Pure, side-effect-free — these
 * are safe to import from any client component, but the locale is hard-coded
 * to ru-RU because the UI is single-locale today.
 */
import type { SearchRow } from "./dashboard-types";

const NUMBER_FORMATTER = new Intl.NumberFormat("ru-RU");

const DATE_FORMATTER = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

export function formatDate(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return DATE_FORMATTER.format(date);
}

export function formatRelative(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";

  const diffMs = Date.now() - date.getTime();
  const diffHours = Math.max(0, Math.round(diffMs / 3_600_000));
  if (diffHours < 1) return "меньше часа назад";
  if (diffHours < 24) return `${diffHours} ч назад`;
  return `${Math.round(diffHours / 24)} д назад`;
}

// Defence-in-depth: source_url приходит из slim_active.parquet (hh.ru shards).
// React не блокирует javascript:/data:/file: URL автоматически — отказываемся
// рендерить anchor, если URL не https-схема.
export function safeHref(url: string | null): string | null {
  return url && url.startsWith("https://") ? url : null;
}

export function formatInt(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return NUMBER_FORMATTER.format(value);
}

function formatSalaryNumber(value: number): string {
  return NUMBER_FORMATTER.format(value).replace(/\u00a0/g, " ");
}

export function formatSalary(
  row: Pick<SearchRow, "salary_rub_min" | "salary_rub_max">,
): string {
  if (row.salary_rub_min === null && row.salary_rub_max === null) return "—";
  if (row.salary_rub_min !== null && row.salary_rub_max !== null) {
    return `${formatSalaryNumber(row.salary_rub_min)}–${formatSalaryNumber(row.salary_rub_max)} ₽`;
  }
  if (row.salary_rub_min !== null) {
    return `от ${formatSalaryNumber(row.salary_rub_min)} ₽`;
  }
  return `до ${formatSalaryNumber(row.salary_rub_max ?? 0)} ₽`;
}

const REMOTE_LABELS: Record<string, string> = {
  office: "офис",
  hybrid: "гибрид",
  remote: "удалённо",
  unknown: "не указан",
};

const SENIORITY_LABELS: Record<string, string> = {
  intern: "intern",
  junior: "junior",
  middle: "middle",
  senior: "senior",
  lead: "lead",
  principal: "principal",
  unknown: "не указан",
};

const SOURCE_LABELS: Record<string, string> = {
  hh: "hh.ru",
  telegram: "Telegram",
};

export function formatRemoteType(value: string): string {
  return REMOTE_LABELS[value] ?? value;
}

export function formatSeniority(value: string): string {
  return SENIORITY_LABELS[value] ?? value;
}

export function formatSource(value: string): string {
  return SOURCE_LABELS[value] ?? value;
}

export type SearchFilters = {
  query: string;
  city: string | null;
  remoteType: string;
  seniority: Set<string>;
  source: Set<string>;
  skills: Set<string>;
  employerName: string | null;
  salaryMin: string;
  salaryMax: string;
  offset: number;
};

export function buildParams(filters: SearchFilters, limit: number): URLSearchParams {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(filters.offset));

  if (filters.query.trim()) params.set("q", filters.query.trim());
  if (filters.city) params.append("city", filters.city);
  if (filters.remoteType !== "all") params.append("remote_type", filters.remoteType);
  if (filters.employerName) params.append("employer_name", filters.employerName);
  filters.seniority.forEach((value) => params.append("seniority", value));
  filters.source.forEach((value) => params.append("source", value));
  filters.skills.forEach((value) => params.append("skills", value));
  if (filters.salaryMin.trim()) params.set("salary_min", filters.salaryMin.trim());
  if (filters.salaryMax.trim()) params.set("salary_max", filters.salaryMax.trim());

  return params;
}
