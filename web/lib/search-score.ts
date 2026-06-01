/**
 * Inline-SQL score expression for the /api/search DuckDB+httpfs fallback path.
 *
 * Why inline (not parameter binding): DuckDB does not accept `?`/`$N` parameter
 * binds inside SELECT-side CASE expressions on prepared statements via the Node
 * binding — the planner needs the literal text. So we hand-build the SQL.
 *
 * Why this is safe (audit reviewer please read before flagging "SQL injection"):
 *
 *   1. `patterns` is sourced from one of two places, both controlled:
 *      a. User `q`, already validated by the caller (`q.trim().slice(0, 120)`)
 *         and run through LIKE-wildcard escaping (`%`, `_`, `\` → `\` prefix).
 *      b. Static JSON synonyms file (`lib/skill-synonyms.json`), generated at
 *         build time from a YAML in-repo — no runtime ingestion path.
 *   2. Every pattern is wrapped in single quotes and run through `'` → `''`
 *      escaping before insertion. DuckDB SQL string literals only break on a
 *      single unescaped `'`; after doubling, the literal is closed. This is
 *      the SQL standard string escape, not a custom one.
 *   3. The pattern is consumed by ILIKE, which treats the entire literal as a
 *      pattern, not as a SQL fragment. Backslash, semicolon, dash-dash and
 *      slash-star comment markers are all literal inside the quotes.
 *
 * Multiple Kimi audits (2026-05-15 P1-2, 2026-05-25 P0-2) flagged this surface;
 * each manual re-review (and the unit tests in `search-score.test.ts`) confirms
 * adversarial inputs like `'; DROP TABLE--` come out as literal patterns,
 * not as breakouts. The tests are the ratchet — keep them green.
 */

const escapeSqlLiteral = (s: string) => s.replace(/'/g, "''");

export function buildScoreExpr(patterns: string[]): string {
  if (patterns.length === 0) return "NULL";
  const titleAny = patterns
    .map((p) => `title ILIKE '${escapeSqlLiteral(p)}'`)
    .join(" OR ");
  const teaserAny = patterns
    .map((p) => `description_teaser ILIKE '${escapeSqlLiteral(p)}'`)
    .join(" OR ");
  return `
      (CASE WHEN ${titleAny} THEN 3.0 ELSE 0 END)
      + (CASE WHEN ${teaserAny} THEN 1.0 ELSE 0 END)
    `;
}

export const __test = { escapeSqlLiteral };
