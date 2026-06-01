import { describe, expect, it } from "vitest";

import { __test, buildScoreExpr } from "@/lib/search-score";

describe("escapeSqlLiteral", () => {
  it("doubles single quotes (SQL standard)", () => {
    expect(__test.escapeSqlLiteral("O'Brien")).toBe("O''Brien");
  });

  it("does not escape double quotes, semicolons, or comment markers", () => {
    // Inside a `'...'` literal these are all data, not SQL syntax. Escaping them
    // would only obscure intent — verify we leave them alone so future audits
    // don't reintroduce the misconception.
    expect(__test.escapeSqlLiteral(`"; DROP TABLE--`)).toBe(`"; DROP TABLE--`);
    expect(__test.escapeSqlLiteral("/* comment */")).toBe("/* comment */");
  });

  it("leaves backslashes intact (LIKE-escape is the caller's responsibility)", () => {
    expect(__test.escapeSqlLiteral("a\\b")).toBe("a\\b");
  });
});

describe("buildScoreExpr", () => {
  it("returns 'NULL' when no patterns supplied", () => {
    expect(buildScoreExpr([])).toBe("NULL");
  });

  it("emits a CASE WHEN ... ILIKE expression for a single pattern", () => {
    const sql = buildScoreExpr(["%python%"]);
    expect(sql).toContain("title ILIKE '%python%'");
    expect(sql).toContain("description_teaser ILIKE '%python%'");
    expect(sql).toMatch(/CASE WHEN .* THEN 3\.0 ELSE 0 END/);
    expect(sql).toMatch(/CASE WHEN .* THEN 1\.0 ELSE 0 END/);
  });

  it("ORs multiple patterns via title/teaser branches", () => {
    const sql = buildScoreExpr(["%python%", "%питон%"]);
    expect(sql).toContain("title ILIKE '%python%' OR title ILIKE '%питон%'");
    expect(sql).toContain(
      "description_teaser ILIKE '%python%' OR description_teaser ILIKE '%питон%'",
    );
  });

  it("escapes single quotes in patterns (no literal-breakout possible)", () => {
    // The adversarial pattern below would close the string and inject DROP if
    // the escape were missing. After doubling, the apostrophe stays inside the
    // literal — DuckDB parses one quoted string, not a multi-statement.
    const sql = buildScoreExpr(["%'; DROP TABLE vacancies--%"]);
    expect(sql).toContain(
      "title ILIKE '%''; DROP TABLE vacancies--%'",
    );
    // Strip every '...' quoted literal (with '' escape sequences) and assert
    // DROP no longer appears in the remaining SQL — i.e., it was entirely
    // contained inside a literal, never as a free statement.
    const stripped = sql.replace(/'(?:[^']|'')*'/g, "''");
    expect(stripped).not.toMatch(/DROP\s+TABLE/i);
  });

  it("escapes UNION ALL SELECT attack pattern", () => {
    const sql = buildScoreExpr(["%' UNION ALL SELECT * FROM passwords --%"]);
    expect(sql).toContain(
      "title ILIKE '%'' UNION ALL SELECT * FROM passwords --%'",
    );
  });

  it("handles patterns with multiple apostrophes (all get doubled)", () => {
    const sql = buildScoreExpr(["it's a 'test'"]);
    expect(sql).toContain("title ILIKE 'it''s a ''test'''");
  });

  it("preserves SQL comments inside the literal as data", () => {
    const sql = buildScoreExpr(["%--%", "%/*%"]);
    // Comment markers are inside `'...'` so they're just data — DuckDB never
    // reaches comment-stripping. Assert they appear quoted, not bare.
    expect(sql).toContain("ILIKE '%--%'");
    expect(sql).toContain("ILIKE '%/*%'");
  });

  it("preserves LIKE wildcards (caller already escaped them where intended)", () => {
    // buildScoreExpr is a pure formatter — LIKE-wildcard escaping happens
    // upstream (`replace(/[%_\\]/g, "\\$&")` in the route). Here we just verify
    // we don't strip or re-escape `%` / `_` ourselves.
    const sql = buildScoreExpr(["%python_3%"]);
    expect(sql).toContain("title ILIKE '%python_3%'");
  });
});
