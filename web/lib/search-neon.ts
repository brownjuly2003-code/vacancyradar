/**
 * Neon-backend search query.
 *
 * Mirrors /api/search semantics from the DuckDB route byte-for-byte:
 *   - ILIKE on title + description_teaser (Postgres pg_trgm GIN index)
 *   - Equality WHERE on city / employer_name / remote_type / seniority / source
 *   - Array containment for skills (`skills @> ARRAY[skill]`)
 *   - Range on salary_rub_min / salary_rub_max
 *   - Score = 3 (title hit) + 1 (teaser hit); 0 when no q
 *   - Order: q → score DESC, last_seen_at DESC; no q → last_seen_at DESC
 *
 * Synonym expansion happens in the route handler before this is called.
 */
import { neonQuery } from "@/lib/neon";

export interface NeonSearchFilters {
  cities: string[];
  employers: string[];
  remoteTypes: string[];
  seniorities: string[];
  sources: string[];
  skills: string[];
  salaryMin: number | null;
  salaryMax: number | null;
  expandedTerms: string[];
  limit: number;
  offset: number;
}

export interface NeonSearchRow {
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
}

interface BuiltQuery {
  countSql: string;
  dataSql: string;
  params: unknown[];
}

function buildQueries(filters: NeonSearchFilters): BuiltQuery {
  const conditions: string[] = [];
  const params: unknown[] = [];

  const pushIn = (column: string, values: string[]) => {
    if (values.length === 0) return;
    const placeholders: string[] = [];
    for (const v of values) {
      params.push(v);
      placeholders.push(`$${params.length}`);
    }
    conditions.push(`${column} IN (${placeholders.join(", ")})`);
  };

  pushIn("city", filters.cities);
  pushIn("employer_name", filters.employers);
  pushIn("remote_type", filters.remoteTypes);
  pushIn("seniority", filters.seniorities);
  pushIn("source", filters.sources);

  for (const skill of filters.skills) {
    params.push(skill);
    conditions.push(`skills @> ARRAY[$${params.length}]::text[]`);
  }

  if (filters.salaryMin !== null) {
    params.push(filters.salaryMin);
    conditions.push(`salary_rub_min >= $${params.length}`);
  }
  if (filters.salaryMax !== null) {
    params.push(filters.salaryMax);
    conditions.push(`salary_rub_max <= $${params.length}`);
  }

  let scoreExpr = "NULL::int AS score";
  if (filters.expandedTerms.length > 0) {
    const titleOrs: string[] = [];
    const teaserOrs: string[] = [];
    const ftsOrs: string[] = [];
    for (const term of filters.expandedTerms) {
      // Escape ILIKE wildcards in the user term: % _ \
      const escaped = term.replace(/[\\%_]/g, "\\$&");
      params.push(`%${escaped}%`);
      const pIdx = params.length;
      titleOrs.push(`title ILIKE $${pIdx}`);
      teaserOrs.push(`description_teaser ILIKE $${pIdx}`);

      // Russian FTS (idx_vacancies_fts_ru) on multi-word terms only:
      // - Multi-word ("data engineer"): stemmer catches «data engineering» и
      //   morfo-варианты «масштабирование» ↔ «масштабировать», +47% recall
      //   на multi-token query (session 14 evidence).
      // - Single-token («python»): substring ILIKE catches `python3`,
      //   `python-developer` через trgm; FTS усечёт стеммингом до точного
      //   корня и потеряет 178 строк на "python", 159 на "rust". OR-union
      //   с ILIKE даёт recall strict ⊇ ILIKE-only при любых term'ах, но
      //   single-token путь не даёт FTS никакого нового матча → пропускаем
      //   ради latency (combined path был 260ms на «python» vs 8ms ILIKE).
      if (/\s/.test(term.trim())) {
        params.push(term);
        ftsOrs.push(
          `to_tsvector('russian', title || ' ' || COALESCE(description_teaser, '')) @@ plainto_tsquery('russian', $${params.length})`,
        );
      }
    }
    const allOrs = [...titleOrs, ...teaserOrs, ...ftsOrs];
    conditions.push(`(${allOrs.join(" OR ")})`);
    // Scoring: title hit = 3, teaser hit = 1, FTS-stem hit = 2 (between teaser
    // и title — stemmed match менее точный чем substring но всё ещё
    // semantic). Когда ftsOrs пуст для single-token, FTS-блок выпадает.
    const titleClause = `CASE WHEN ${titleOrs.join(" OR ")} THEN 3 ELSE 0 END`;
    const teaserClause = `CASE WHEN ${teaserOrs.join(" OR ")} THEN 1 ELSE 0 END`;
    const ftsClause = ftsOrs.length
      ? ` + (CASE WHEN ${ftsOrs.join(" OR ")} THEN 2 ELSE 0 END)`
      : "";
    scoreExpr = `(${titleClause}) + (${teaserClause})${ftsClause} AS score`;
  }

  const whereSql = conditions.length > 0 ? `WHERE ${conditions.join(" AND ")}` : "";
  // ORDER BY: last_seen_at DESC is primary recency; first_seen_at DESC breaks
  // last_seen_at ties (which are frequent — ~5 distinct values per 24k batch);
  // vacancy_id DESC is the final stable tie-breaker. Adding first_seen_at
  // prevents `tg:*` from lexicographically dominating `hh:*` on tied
  // last_seen_at — KM audit 2026-05-17 P2. Keep ORDER BY identical to the
  // DuckDB path in route.ts and the parity test.
  const orderBy =
    filters.expandedTerms.length > 0
      ? "ORDER BY score DESC NULLS LAST, last_seen_at DESC, first_seen_at DESC, vacancy_id DESC"
      : "ORDER BY last_seen_at DESC, first_seen_at DESC, vacancy_id DESC";

  const countSql = `SELECT COUNT(*)::int AS total FROM vacancies ${whereSql}`;

  // For ORDER BY score DESC we need score in the SELECT. Postgres allows it
  // because we alias it. Limit/offset bound separately to keep param indices stable.
  params.push(filters.limit);
  const limitIdx = params.length;
  params.push(filters.offset);
  const offsetIdx = params.length;

  const dataSql = `
    SELECT
      vacancy_id, title, employer_name,
      salary_rub_min, salary_rub_max, salary_currency,
      city, region, remote_type, seniority,
      description_teaser, skills, source, source_url,
      posted_at, first_seen_at, last_seen_at,
      ${scoreExpr}
    FROM vacancies
    ${whereSql}
    ${orderBy}
    LIMIT $${limitIdx} OFFSET $${offsetIdx}
  `;

  return { countSql, dataSql, params };
}

function toIsoString(value: unknown): string | null {
  if (value == null) return null;
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "string") return value;
  return String(value);
}

export async function runNeonSearch(filters: NeonSearchFilters): Promise<{
  total: number;
  rows: NeonSearchRow[];
}> {
  const { countSql, dataSql, params } = buildQueries(filters);

  // The count query uses params[0..N-LIMIT-OFFSET], data uses all params.
  // We pre-built both with shared placeholder indices except LIMIT/OFFSET tail.
  const countParams = params.slice(0, params.length - 2);

  // neonQuery wraps sql.query with 3-attempt exp backoff retry — transient
  // 429/5xx/TLS-stall no longer trips an immediate 503. KM audit 2026-05-17 P1.
  const [countRows, dataRows] = await Promise.all([
    neonQuery<Array<{ total: number }>>(countSql, countParams),
    neonQuery<Array<Record<string, unknown>>>(dataSql, params),
  ]);
  const total = Number(countRows[0]?.total ?? 0);

  const rows: NeonSearchRow[] = dataRows.map((r) => {
    const skillsRaw = r.skills;
    const skills =
      Array.isArray(skillsRaw) && skillsRaw.every((s) => typeof s === "string")
        ? (skillsRaw as string[])
        : [];
    return {
      vacancy_id: String(r.vacancy_id),
      title: String(r.title ?? ""),
      employer_name: r.employer_name == null ? null : String(r.employer_name),
      salary_rub_min: r.salary_rub_min == null ? null : Number(r.salary_rub_min),
      salary_rub_max: r.salary_rub_max == null ? null : Number(r.salary_rub_max),
      salary_currency: r.salary_currency == null ? null : String(r.salary_currency),
      city: r.city == null ? null : String(r.city),
      region: r.region == null ? null : String(r.region),
      remote_type: String(r.remote_type ?? "unknown"),
      seniority: String(r.seniority ?? "unknown"),
      description_teaser: r.description_teaser == null ? null : String(r.description_teaser),
      skills,
      source: String(r.source ?? "hh"),
      source_url: r.source_url == null ? null : String(r.source_url),
      posted_at: toIsoString(r.posted_at),
      first_seen_at: toIsoString(r.first_seen_at) ?? "",
      last_seen_at: toIsoString(r.last_seen_at) ?? "",
      score: r.score == null ? null : Number(r.score),
    };
  });

  return { total, rows };
}
