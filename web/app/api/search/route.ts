import { NextRequest, NextResponse } from "next/server";

import { ACTIVE_PARQUET, runQuery } from "@/lib/duckdb";
import { isNeonConfigured } from "@/lib/neon";
import { buildScoreExpr } from "@/lib/search-score";
import { NeonSearchFilters, runNeonSearch } from "@/lib/search-neon";
import { parseSearchParams } from "@/lib/search-validation";
import skillSynonyms from "@/lib/skill-synonyms.json";

const SYNONYMS = skillSynonyms as Record<string, string[]>;

export const runtime = "nodejs";
export const maxDuration = 10;
export const revalidate = 60;

/**
 * /api/search reads the live IT slim_active.parquet on Vercel Blob via DuckDB
 * + httpfs — same data path as /api/facets and /api/trends, so all three
 * surfaces agree on what "IT-рынок вакансий" means. We deliberately do NOT
 * route through Turso anymore: the Turso vacancies table still holds the
 * pre-pivot full-market snapshot (write quota exhausted 2026-05-12 → no
 * resync), which made the search panel show "продажи Новокузнецка" under an
 * "IT" header.
 *
 * No FTS5 — DuckDB on parquet gives us ILIKE with Unicode case-folding which
 * is plenty for a 66k corpus. Ranking when `q` is set: 3·title + 1·teaser
 * substring hits, then last_seen_at DESC. Without `q`: just recency.
 *
 * 2026-05-16: `description_fts` dropped from slim_active.parquet (Turso FTS5
 * was the only real consumer; ILIKE substring on a 1.5KB field added marginal
 * recall but doubled Blob egress). Search now reads only title+teaser.
 *
 * 2026-05-16: Russian-query expansion. Single-word `q` whose lowercase form
 * matches a key in `skill-synonyms.json` is expanded to the full skill cluster
 * before ILIKE matching, so `питон` retrieves `Python` jobs and vice-versa.
 * Multi-word queries pass through unchanged — we want `senior python` to keep
 * its phrase semantics. Aliases shorter than 3 chars (py/js/go) are excluded
 * by the generator since they explode recall with substring matching.
 */

type SearchRow = {
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
  skills: string[];
  source: string;
  source_url: string | null;
  posted_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  score: number | null;
};

function addInFilter(
  field: string,
  valuesToBind: string[],
  conditions: string[],
  args: unknown[],
) {
  if (valuesToBind.length === 0) return;
  const placeholders = valuesToBind.map(() => "?").join(", ");
  conditions.push(`${field} IN (${placeholders})`);
  args.push(...valuesToBind);
}

/**
 * Single-word queries are looked up in the skill synonym map and expanded to
 * the full term cluster (canonical + aliases ≥3 chars, lowercased). Returns
 * the original query verbatim when:
 *   - the query has whitespace (phrase search, expansion would alter intent),
 *   - the lowercased query isn't a known alias,
 *   - the alias maps to only itself (singleton — nothing to expand).
 *
 * Returned terms are guaranteed lowercase; ILIKE is case-insensitive so casing
 * doesn't matter, but lowercase keeps the wire format stable for the response.
 */
function expandQuery(q: string): string[] {
  const trimmed = q.trim();
  if (!trimmed) return [];
  if (/\s/.test(trimmed)) return [trimmed];
  const expansion = SYNONYMS[trimmed.toLowerCase()];
  if (!expansion || expansion.length <= 1) return [trimmed];
  return expansion;
}

function formatTotalLabel(
  total: number,
  rowsLen: number,
  limit: number,
  offset: number,
): string {
  // DuckDB COUNT(*) is exact, but the UI still wants a "50+" affordance when
  // we cap large result sets at the page boundary in the past. We always return
  // the exact total here — keep label aligned for backwards compatibility.
  if (total <= offset + rowsLen) return String(total);
  return String(total);
}

function mapRow(raw: Record<string, unknown>, score: number | null): SearchRow {
  // DuckDB returns list<string> as actual JS arrays through runAndReadAll.
  const skillsRaw = raw.skills;
  const skills =
    Array.isArray(skillsRaw) && skillsRaw.every((s) => typeof s === "string")
      ? (skillsRaw as string[])
      : [];
  return {
    vacancy_id: String(raw.vacancy_id),
    title: String(raw.title ?? ""),
    employer_name: raw.employer_name == null ? null : String(raw.employer_name),
    salary_rub_min: raw.salary_rub_min == null ? null : Number(raw.salary_rub_min),
    salary_rub_max: raw.salary_rub_max == null ? null : Number(raw.salary_rub_max),
    salary_currency: raw.salary_currency == null ? null : String(raw.salary_currency),
    city: raw.city == null ? null : String(raw.city),
    region: raw.region == null ? null : String(raw.region),
    remote_type: String(raw.remote_type ?? "unknown"),
    seniority: String(raw.seniority ?? "unknown"),
    description_teaser: raw.description_teaser == null ? null : String(raw.description_teaser),
    skills,
    source: String(raw.source ?? "hh"),
    source_url: raw.source_url == null ? null : String(raw.source_url),
    posted_at: raw.posted_at == null ? null : String(raw.posted_at),
    first_seen_at: String(raw.first_seen_at ?? ""),
    last_seen_at: String(raw.last_seen_at ?? ""),
    score,
  };
}

export async function GET(request: NextRequest) {
  const startedAt = performance.now();
  const { searchParams } = new URL(request.url);

  // All bounds + enum-whitelist + cap-per-key validation lives in
  // lib/search-validation.ts (unit-tested). The caller can trust the result
  // shape without re-checking ranges. Kimi audit 2026-05-25 P1-6.
  const {
    q,
    limit,
    offset,
    minSalary,
    maxSalary,
    cities,
    employers,
    remoteTypes: remoteTypesFilter,
    seniorities: senioritiesFilter,
    skills: skillsFilter,
    sources: sourceFilter,
  } = parseSearchParams(searchParams);

  let expandedTerms: string[] = [];
  if (q) {
    expandedTerms = expandQuery(q);
  }

  const neonFilters: NeonSearchFilters = {
    cities,
    employers,
    remoteTypes: remoteTypesFilter,
    seniorities: senioritiesFilter,
    sources: sourceFilter,
    skills: skillsFilter,
    salaryMin: minSalary,
    salaryMax: maxSalary,
    expandedTerms,
    limit,
    offset,
  };

  // Neon is the primary live read model whenever its DSN is configured.
  // DuckDB+httpfs remains a fallback/export path; it is not a safe default for
  // interactive search because a timed-out native DuckDB query is not
  // cancellable by Promise.race and can keep running after the route returns.
  // Treat stale local `SEARCH_BACKEND=duckdb` as legacy config unless Neon is
  // absent. `duckdb-only` is reserved for explicit local fallback diagnostics.
  const neonConfigured = isNeonConfigured();
  const useNeonPrimary =
    neonConfigured && process.env.SEARCH_BACKEND !== "duckdb-only";
  let neonAttempted = false;
  if (useNeonPrimary) {
    neonAttempted = true;
    try {
      const { total, rows } = await runNeonSearch(neonFilters);
      const total_label = formatTotalLabel(total, rows.length, limit, offset);
      return NextResponse.json({
        total,
        total_exact: true,
        total_label,
        limit,
        offset,
        rows,
        query_ms: Math.round((performance.now() - startedAt) * 10) / 10,
        ranking: q ? "ilike-score" : "recency",
        backend: "neon",
        ...(q && expandedTerms.length > 1
          ? { query_expanded_to: expandedTerms }
          : {}),
      });
    } catch (error) {
      // Fall through to DuckDB path. Log so Vercel surfaces the cascade rate.
      console.warn(
        "[search] Neon query failed, cascading to DuckDB+httpfs",
        error,
      );
    }
  }

  const conditions: string[] = [];
  const args: unknown[] = [];

  addInFilter("city", cities, conditions, args);
  addInFilter("employer_name", employers, conditions, args);
  addInFilter("remote_type", remoteTypesFilter, conditions, args);
  addInFilter("seniority", senioritiesFilter, conditions, args);
  addInFilter("source", sourceFilter, conditions, args);

  // Skills filter — DuckDB list_contains over the `skills` LIST<STRING> column.
  for (const skill of skillsFilter) {
    conditions.push("list_contains(skills, ?)");
    args.push(skill);
  }

  if (minSalary !== null) {
    conditions.push("salary_rub_min >= ?");
    args.push(minSalary);
  }
  if (maxSalary !== null) {
    // Match the Turso route's semantics: cap on max bound.
    conditions.push("salary_rub_max <= ?");
    args.push(maxSalary);
  }

  // Text search via ILIKE on title+teaser. DuckDB folds the case under
  // Unicode by default with ILIKE, so "Python" matches "PYTHON".
  let scoreExpr: string;
  if (q) {
    const patterns = expandedTerms.map((t) => `%${t.replace(/[%_\\]/g, "\\$&")}%`);
    const titleClauses = patterns.map(() => "title ILIKE ?").join(" OR ");
    const teaserClauses = patterns.map(() => "description_teaser ILIKE ?").join(" OR ");
    conditions.push(`((${titleClauses}) OR (${teaserClauses}))`);
    // Same patterns once for title bindings, again for teaser bindings.
    args.push(...patterns, ...patterns);

    // Score expression is built inline (see lib/search-score.ts for safety
    // analysis + unit tests on adversarial inputs).
    scoreExpr = buildScoreExpr(patterns);
  } else {
    scoreExpr = "NULL";
  }

  const where = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
  // ORDER BY: last_seen_at DESC is primary recency; first_seen_at DESC breaks
  // last_seen_at ties (daily ingest stamps many rows identically — typically
  // only ~5 distinct last_seen_at across a 24k row batch); vacancy_id DESC
  // is the final stable tie-breaker. Adding first_seen_at prevents `tg:*`
  // from lexicographically dominating `hh:*` on tied last_seen_at (KM audit
  // 2026-05-17 P2). Must stay identical to the Neon path in lib/search-neon.ts
  // and the parity test.
  const orderBy = q
    ? "ORDER BY score DESC NULLS LAST, last_seen_at DESC, first_seen_at DESC, vacancy_id DESC"
    : "ORDER BY last_seen_at DESC, first_seen_at DESC, vacancy_id DESC";

  // The parquet path goes as the first parameter; everything else follows.
  // ACTIVE_PARQUET is the live IT slim at Blob.
  const countSql = `
    SELECT COUNT(*)::INT AS total
    FROM read_parquet($1)
    ${where}
  `;
  const dataSql = `
    SELECT
      vacancy_id, title, employer_name,
      salary_rub_min, salary_rub_max, salary_currency,
      city, region, remote_type, seniority,
      description_teaser, skills, source, source_url,
      posted_at, first_seen_at, last_seen_at,
      ${scoreExpr} AS score
    FROM read_parquet($1)
    ${where}
    ${orderBy}
    LIMIT ? OFFSET ?
  `;

  try {
    const [countRows, dataRows] = await Promise.all([
      runQuery(countSql, [ACTIVE_PARQUET, ...args]),
      runQuery(dataSql, [ACTIVE_PARQUET, ...args, limit, offset]),
    ]);

    const total = Number((countRows[0] as { total?: number })?.total ?? 0);
    const rows: SearchRow[] = (dataRows as Record<string, unknown>[]).map((r) =>
      mapRow(r, r.score == null ? null : Number(r.score)),
    );
    const total_label = formatTotalLabel(total, rows.length, limit, offset);

    return NextResponse.json({
      total,
      total_exact: true,
      total_label,
      limit,
      offset,
      rows,
      query_ms: Math.round((performance.now() - startedAt) * 10) / 10,
      ranking: q ? "ilike-score" : "recency",
      backend: neonAttempted ? "duckdb-fallback" : "duckdb",
      // Only echo expansion when it actually broadened the query — single-term
      // arrays are noise. UI can show a hint "+ Python jobs included" when this
      // is present and contains terms beyond what the user typed.
      ...(q && expandedTerms.length > 1
        ? { query_expanded_to: expandedTerms }
        : {}),
    });
  } catch (error) {
    console.error("[search] DuckDB query failed", error);
    if (
      !neonAttempted &&
      neonConfigured &&
      process.env.SEARCH_BACKEND !== "duckdb-only"
    ) {
      try {
        const { total, rows } = await runNeonSearch(neonFilters);
        const total_label = formatTotalLabel(total, rows.length, limit, offset);
        return NextResponse.json({
          total,
          total_exact: true,
          total_label,
          limit,
          offset,
          rows,
          query_ms: Math.round((performance.now() - startedAt) * 10) / 10,
          ranking: q ? "ilike-score" : "recency",
          backend: "neon-fallback",
          ...(q && expandedTerms.length > 1
            ? { query_expanded_to: expandedTerms }
            : {}),
        });
      } catch (neonError) {
        console.warn(
          "[search] Neon fallback after DuckDB failure failed",
          neonError,
        );
      }
    }
    const detail = neonAttempted
      ? "Both Neon and DuckDB+httpfs paths failed — see server logs."
      : "DuckDB+httpfs query error — see server logs.";
    return NextResponse.json(
      { error: "search_failed", detail },
      { status: 503 },
    );
  }
}
