import { describe, expect, it } from "vitest";

import {
  REMOTE_TYPES,
  SEARCH_LIMITS,
  SENIORITIES,
  SOURCES,
  allowedValues,
  parseBoundedInt,
  parseFiniteNumber,
  parseSearchParams,
  values,
} from "@/lib/search-validation";

const sp = (s: string) => new URLSearchParams(s);

describe("enum whitelists", () => {
  it("REMOTE_TYPES covers exactly the slim_active enum values", () => {
    expect([...REMOTE_TYPES].sort()).toEqual(
      ["hybrid", "office", "remote", "unknown"].sort(),
    );
  });

  it("SENIORITIES covers exactly the seven slim_active tiers", () => {
    expect([...SENIORITIES].sort()).toEqual(
      [
        "intern",
        "junior",
        "middle",
        "senior",
        "lead",
        "principal",
        "unknown",
      ].sort(),
    );
  });

  it("SOURCES covers exactly hh + telegram", () => {
    expect([...SOURCES].sort()).toEqual(["hh", "telegram"]);
  });
});

describe("parseBoundedInt", () => {
  it("returns default for null/missing input", () => {
    expect(parseBoundedInt(null, 50, 1, 200)).toBe(50);
  });

  it("clamps below min", () => {
    expect(parseBoundedInt("-3", 50, 1, 200)).toBe(1);
    expect(parseBoundedInt("0", 50, 1, 200)).toBe(1);
  });

  it("clamps above max", () => {
    expect(parseBoundedInt("9999", 50, 1, 200)).toBe(200);
  });

  it("collapses NaN / non-numeric to default", () => {
    expect(parseBoundedInt("abc", 50, 1, 200)).toBe(50);
    expect(parseBoundedInt("", 50, 1, 200)).toBe(50);
  });

  it("collapses Infinity to default", () => {
    expect(parseBoundedInt("Infinity", 50, 1, 200)).toBe(50);
  });
});

describe("parseFiniteNumber", () => {
  it("returns null for null/empty/non-finite", () => {
    expect(parseFiniteNumber(null)).toBeNull();
    expect(parseFiniteNumber("")).toBeNull();
    expect(parseFiniteNumber("foo")).toBeNull();
    expect(parseFiniteNumber("Infinity")).toBeNull();
    expect(parseFiniteNumber("NaN")).toBeNull();
  });

  it("returns the parsed number for valid input", () => {
    expect(parseFiniteNumber("100000")).toBe(100000);
    expect(parseFiniteNumber("0")).toBe(0);
    expect(parseFiniteNumber("-50")).toBe(-50);
  });
});

describe("values()", () => {
  it("trims, slices to maxLength, drops empties", () => {
    const params = sp("city=%20Moscow%20&city=&city=Saint%20Petersburg");
    expect(values(params, "city", 120)).toEqual(["Moscow", "Saint Petersburg"]);
  });

  it("caps repeated keys at maxValuesPerKey", () => {
    const qs = Array.from({ length: 100 }, (_, i) => `city=City${i}`).join("&");
    const result = values(sp(qs), "city", 120);
    expect(result.length).toBe(SEARCH_LIMITS.maxValuesPerKey);
    expect(result[0]).toBe("City0");
  });

  it("truncates individual values to maxLength", () => {
    const big = "x".repeat(500);
    const result = values(sp(`city=${big}`), "city", 10);
    expect(result[0]).toBe("x".repeat(10));
  });
});

describe("allowedValues()", () => {
  it("filters out values not in the whitelist", () => {
    const params = sp(
      "remote_type=remote&remote_type=invalid&remote_type=hybrid&remote_type=DROP%20TABLE",
    );
    expect(allowedValues(params, "remote_type", REMOTE_TYPES)).toEqual([
      "remote",
      "hybrid",
    ]);
  });
});

describe("parseSearchParams()", () => {
  it("parses an empty querystring with sane defaults", () => {
    const result = parseSearchParams(sp(""));
    expect(result).toEqual({
      q: "",
      limit: 50,
      offset: 0,
      minSalary: null,
      maxSalary: null,
      cities: [],
      employers: [],
      remoteTypes: [],
      seniorities: [],
      skills: [],
      sources: ["hh"], // default source filter
    });
  });

  it("trims and length-caps the q param", () => {
    const result = parseSearchParams(sp("q=%20Python%20"));
    expect(result.q).toBe("Python");

    const longQ = "x".repeat(500);
    expect(parseSearchParams(sp(`q=${longQ}`)).q.length).toBe(
      SEARCH_LIMITS.qMaxChars,
    );
  });

  it("parses limit/offset with bounds", () => {
    expect(parseSearchParams(sp("limit=10&offset=100")).limit).toBe(10);
    expect(parseSearchParams(sp("limit=10&offset=100")).offset).toBe(100);
    expect(parseSearchParams(sp("limit=999")).limit).toBe(SEARCH_LIMITS.limitMax);
    expect(parseSearchParams(sp("limit=-3")).limit).toBe(SEARCH_LIMITS.limitMin);
  });

  it("filters source against whitelist and defaults to hh when none valid", () => {
    expect(parseSearchParams(sp("source=evil&source=injected")).sources).toEqual(
      ["hh"],
    );
    expect(parseSearchParams(sp("source=telegram")).sources).toEqual(["telegram"]);
    expect(
      parseSearchParams(sp("source=hh&source=telegram")).sources.sort(),
    ).toEqual(["hh", "telegram"]);
  });

  it("drops invalid enums silently (no error to caller)", () => {
    // P1-6 ratchet: invalid enum values shouldn't blow up the request — they
    // just disappear. UI can show 0 results.
    const result = parseSearchParams(
      sp("remote_type=teleporter&seniority=archmage"),
    );
    expect(result.remoteTypes).toEqual([]);
    expect(result.seniorities).toEqual([]);
  });

  it("passes through valid salary bounds", () => {
    const result = parseSearchParams(sp("salary_min=100000&salary_max=500000"));
    expect(result.minSalary).toBe(100000);
    expect(result.maxSalary).toBe(500000);
  });

  it("absorbs malformed salary values to null", () => {
    const result = parseSearchParams(sp("salary_min=abc&salary_max=NaN"));
    expect(result.minSalary).toBeNull();
    expect(result.maxSalary).toBeNull();
  });

  it("caps repeated skill filter at maxValuesPerKey", () => {
    const qs = Array.from({ length: 100 }, (_, i) => `skills=tech${i}`).join("&");
    expect(parseSearchParams(sp(qs)).skills.length).toBe(
      SEARCH_LIMITS.maxValuesPerKey,
    );
  });
});
