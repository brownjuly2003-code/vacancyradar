/**
 * Runtime request validation for /api/search query parameters.
 *
 * Kimi audit 2026-05-25 P1-6 flagged the absence of a runtime schema layer
 * (no Zod / Valibot). The actual exposure is small — every untrusted input
 * already passes through one of:
 *   - SQL parameter binding (`runQuery(sql, [...args])`),
 *   - LIKE-wildcard escape (`replace(/[%_\\]/g, "\\$&")`),
 *   - SQL literal escape (`buildScoreExpr` in lib/search-score.ts),
 *   - Enum whitelist (`REMOTE_TYPES`, `SENIORITIES`, `SOURCES`).
 *
 * But the parsing was inline in `route.ts`, making it hard to test or audit
 * the bounds. This module pulls parse + validate into pure functions so the
 * invariants (max lengths, integer ranges, enum membership) live in one place
 * with unit tests. No new dependency — `URLSearchParams.get/getAll` + native
 * `Number.parseInt` cover the surface; Zod would buy syntactic sugar at the
 * cost of bundle size on the Vercel function.
 */

export const REMOTE_TYPES = new Set([
  "office",
  "hybrid",
  "remote",
  "unknown",
] as const);
export const SENIORITIES = new Set([
  "intern",
  "junior",
  "middle",
  "senior",
  "lead",
  "principal",
  "unknown",
] as const);
export const SOURCES = new Set(["hh", "telegram"] as const);

export const SEARCH_LIMITS = {
  // Tight bounds derived from realistic UI usage. `q` matches the input field
  // maxlength; `limit` matches the largest infinite-scroll page used by the
  // dashboard; per-filter `maxLength` matches the longest realistic enum value
  // or city/employer name.
  qMaxChars: 120,
  limitMin: 1,
  limitMax: 200,
  limitDefault: 50,
  offsetMax: 1_000_000, // sane upper bound; UI never pages past ~5k anyway
  cityMaxLen: 120,
  employerMaxLen: 160,
  enumMaxLen: 80,
  skillMaxLen: 80,
  // Per-key request fan-out cap — protects against `?seniority=...&seniority=...&...`
  // attempts to inflate the SQL IN-list.
  maxValuesPerKey: 50,
} as const;

/**
 * Trim + length-cap + drop-empties for repeated query params.
 *
 * Order matters: `slice(0, maxLength)` happens after `trim` so leading/trailing
 * whitespace doesn't eat the budget. Empty-after-trim values are dropped — a
 * common UI bug is sending `?city=&city=Москва`, and the empty entry should
 * never reach SQL.
 */
export function values(
  searchParams: URLSearchParams,
  key: string,
  maxLength: number,
): string[] {
  return searchParams
    .getAll(key)
    .map((value) => value.trim().slice(0, maxLength))
    .filter(Boolean)
    .slice(0, SEARCH_LIMITS.maxValuesPerKey);
}

/** Same as `values` but additionally whitelist-filtered against an enum set. */
export function allowedValues<T extends string>(
  searchParams: URLSearchParams,
  key: string,
  allowed: ReadonlySet<T>,
): T[] {
  return values(searchParams, key, SEARCH_LIMITS.enumMaxLen).filter((v): v is T =>
    (allowed as ReadonlySet<string>).has(v),
  );
}

/**
 * Parse a non-negative bounded integer from a query string, returning a default
 * when the value is missing or out of bounds. `NaN`, `Infinity`, and negative
 * values all collapse to the default — the caller never sees junk.
 */
export function parseBoundedInt(
  raw: string | null,
  fallback: number,
  min: number,
  max: number,
): number {
  if (raw === null) return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(Math.max(parsed, min), max);
}

/** Parse a finite number or null. Used for salary_min / salary_max. */
export function parseFiniteNumber(raw: string | null): number | null {
  if (raw === null || raw === "") return null;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : null;
}

export interface ParsedSearch {
  q: string;
  limit: number;
  offset: number;
  minSalary: number | null;
  maxSalary: number | null;
  cities: string[];
  employers: string[];
  remoteTypes: string[];
  seniorities: string[];
  skills: string[];
  sources: string[];
}

/**
 * One-stop search-params parser. Returns a fully-normalized request object;
 * downstream code can trust the bounds without any further validation.
 *
 * Defaulting source filter to `["hh"]` when none is requested is a UX choice
 * (TG raw lake has weak signal on recency sort) — kept here so the default is
 * visible in tests rather than hidden in route logic.
 */
export function parseSearchParams(searchParams: URLSearchParams): ParsedSearch {
  const q = (searchParams.get("q") ?? "").trim().slice(0, SEARCH_LIMITS.qMaxChars);
  const limit = parseBoundedInt(
    searchParams.get("limit"),
    SEARCH_LIMITS.limitDefault,
    SEARCH_LIMITS.limitMin,
    SEARCH_LIMITS.limitMax,
  );
  const offset = parseBoundedInt(
    searchParams.get("offset"),
    0,
    0,
    SEARCH_LIMITS.offsetMax,
  );
  const minSalary = parseFiniteNumber(searchParams.get("salary_min"));
  const maxSalary = parseFiniteNumber(searchParams.get("salary_max"));

  const cities = values(searchParams, "city", SEARCH_LIMITS.cityMaxLen);
  const employers = values(
    searchParams,
    "employer_name",
    SEARCH_LIMITS.employerMaxLen,
  );
  const remoteTypes = allowedValues(searchParams, "remote_type", REMOTE_TYPES);
  const seniorities = allowedValues(searchParams, "seniority", SENIORITIES);
  const skills = values(searchParams, "skills", SEARCH_LIMITS.skillMaxLen);
  const explicitSources = allowedValues(searchParams, "source", SOURCES);
  const sources = explicitSources.length > 0 ? explicitSources : ["hh"];

  return {
    q,
    limit,
    offset,
    minSalary,
    maxSalary,
    cities,
    employers,
    remoteTypes: remoteTypes as string[],
    seniorities: seniorities as string[],
    skills,
    sources: sources as string[],
  };
}
