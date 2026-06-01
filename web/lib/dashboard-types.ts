/**
 * Wire-format types for the live dashboard at `/`.
 *
 * Extracted from `app/page.tsx` (2026-05-16) to keep that file focused on
 * orchestration rather than schema. Anything backed by an `/api/*` route or
 * surfaced as React state lives here; presentation-only helpers stay in
 * the page module.
 *
 * Every type maps directly to one route response (or one row inside it), so
 * the comments on `FacetsResponse.error`/`detail` and `SearchResponse.
 * query_expanded_to` are authoritative — change them when the route changes.
 */

export type CountFacet = {
  value: string;
  count: number;
};

export type FacetsResponse = {
  summary: {
    total_vacancies: number;
    unique_cities: number;
    unique_employers: number;
    unique_skills: number;
    latest_seen_at: string | null;
    source_breakdown: Record<string, number>;
  };
  facets: {
    city: CountFacet[];
    employer_name: CountFacet[];
    remote_type: CountFacet[];
    seniority: CountFacet[];
    source: CountFacet[];
    skills: CountFacet[];
    salary_range: {
      min: number | null;
      max: number | null;
      p50: number | null;
      p90: number | null;
      with_salary_pct: number;
    };
  };
  refreshed_at: string;
  // 503 fallback payload still has the same shape (with zero counts) so the
  // dashboard layout stays stable. `error` and `detail` fields surface when
  // the backend couldn't reach the live data source (typically Vercel Blob
  // store_suspended on free-tier egress overage).
  error?: string;
  detail?: string;
};

export type SearchRow = {
  vacancy_id: string;
  title: string;
  employer_name: string | null;
  salary_rub_min: number | null;
  salary_rub_max: number | null;
  salary_currency: string | null;
  city: string | null;
  region: string | null;
  remote_type: string;
  seniority: string;
  description_teaser: string | null;
  skills: string[] | null;
  source: string;
  source_url: string | null;
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
};

export type SearchResponse = {
  total: number;
  total_exact?: boolean;
  total_label?: string;
  limit: number;
  offset: number;
  rows: SearchRow[];
  query_ms: number;
  // Server-side synonym expansion (e.g. `питон` → ['python', 'пайтон', 'питон']).
  // Present only when expansion actually broadened the original single-word query;
  // omitted for phrase queries or non-skill terms.
  query_expanded_to?: string[];
};

export type ViewMode = "table" | "cards";
