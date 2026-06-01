import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildParams,
  formatDate,
  formatInt,
  formatRelative,
  formatRemoteType,
  formatSalary,
  formatSeniority,
  formatSource,
  safeHref,
  type SearchFilters,
} from "@/lib/dashboard-format";

describe("dashboard display formatters", () => {
  it("formats compact Russian salary ranges", () => {
    expect(formatSalary({ salary_rub_min: 120000, salary_rub_max: 180000 })).toBe("120 000–180 000 ₽");
    expect(formatSalary({ salary_rub_min: 200000, salary_rub_max: null })).toBe("от 200 000 ₽");
    expect(formatSalary({ salary_rub_min: null, salary_rub_max: 250000 })).toBe("до 250 000 ₽");
    expect(formatSalary({ salary_rub_min: null, salary_rub_max: null })).toBe("—");
  });

  it("formats enum values as dashboard labels", () => {
    expect(formatRemoteType("remote")).toBe("удалённо");
    expect(formatRemoteType("hybrid")).toBe("гибрид");
    expect(formatRemoteType("office")).toBe("офис");
    expect(formatRemoteType("unknown")).toBe("не указан");
    expect(formatSeniority("middle")).toBe("middle");
    expect(formatSeniority("unknown")).toBe("не указан");
    expect(formatSource("telegram")).toBe("Telegram");
    expect(formatSource("hh")).toBe("hh.ru");
  });

  it("passes unknown enum values through unchanged (open-ended schema)", () => {
    // hh.ru occasionally surfaces brand-new enum values (new remote_type
    // before our dict catches up). The dashboard must render the raw value,
    // not the empty/em-dash placeholder, so support can still triage.
    expect(formatRemoteType("future_kind")).toBe("future_kind");
    expect(formatSeniority("staff_engineer")).toBe("staff_engineer");
    expect(formatSource("linkedin")).toBe("linkedin");
  });

  it("formatInt guards against non-finite inputs", () => {
    // KM re-audit 2026-05-17 P1: sparse aggregates rendered "NaN" before this.
    // ru-RU Intl.NumberFormat groups with non-breaking space (U+00A0).
    expect(formatInt(67165).replace(/ /g, " ")).toBe("67 165");
    expect(formatInt(0)).toBe("0");
    expect(formatInt(null)).toBe("—");
    expect(formatInt(undefined)).toBe("—");
    expect(formatInt(Number.NaN)).toBe("—");
    expect(formatInt(Number.POSITIVE_INFINITY)).toBe("—");
  });
});

describe("safeHref XSS guard", () => {
  it("permits only https URLs", () => {
    expect(safeHref("https://hh.ru/vacancy/123")).toBe("https://hh.ru/vacancy/123");
  });

  it("rejects javascript:, data:, file:, http: and malformed input", () => {
    // source_url приходит из slim_active.parquet (hh.ru shards) — внешний trust.
    expect(safeHref("javascript:alert(1)")).toBeNull();
    expect(safeHref("JavaScript:alert(1)")).toBeNull();
    expect(safeHref("data:text/html,<script>")).toBeNull();
    expect(safeHref("file:///etc/passwd")).toBeNull();
    expect(safeHref("http://hh.ru/vacancy/1")).toBeNull();
    expect(safeHref("//hh.ru/vacancy/1")).toBeNull();
    expect(safeHref("ftp://hh.ru")).toBeNull();
    expect(safeHref(null)).toBeNull();
    expect(safeHref("")).toBeNull();
    expect(safeHref(" https://hh.ru")).toBeNull();
  });
});

describe("formatDate / formatRelative", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-17T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("formats ISO timestamps via ru-RU locale or em-dash on failure", () => {
    const formatted = formatDate("2026-05-15T10:30:00Z");
    expect(formatted).toMatch(/15\.05\.2026/);
    expect(formatted).toMatch(/\d{2}:\d{2}/);
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("—");
  });

  it("describes relative age in часов/дней with ru-RU labels", () => {
    expect(formatRelative(new Date("2026-05-17T11:59:30Z").toISOString())).toBe("меньше часа назад");
    expect(formatRelative(new Date("2026-05-17T09:00:00Z").toISOString())).toBe("3 ч назад");
    expect(formatRelative(new Date("2026-05-15T12:00:00Z").toISOString())).toBe("2 д назад");
    expect(formatRelative(null)).toBe("—");
    expect(formatRelative("not-a-date")).toBe("—");
  });

  it("clamps future timestamps to «меньше часа назад» instead of negative hours", () => {
    // Clock skew protection: vacancy posted_at in the future должно не рендерить "-3 ч назад".
    expect(formatRelative(new Date("2026-05-17T15:00:00Z").toISOString())).toBe("меньше часа назад");
  });
});

describe("buildParams URL filter state", () => {
  const empty: SearchFilters = {
    query: "",
    city: null,
    remoteType: "all",
    seniority: new Set(),
    source: new Set(),
    skills: new Set(),
    employerName: null,
    salaryMin: "",
    salaryMax: "",
    offset: 0,
  };

  it("emits only limit/offset when filters are empty", () => {
    const params = buildParams(empty, 25);
    expect(params.toString()).toBe("limit=25&offset=0");
  });

  it("trims query whitespace and skips blank salary inputs", () => {
    const params = buildParams(
      { ...empty, query: "  python  ", salaryMin: "  ", salaryMax: "" },
      10,
    );
    expect(params.get("q")).toBe("python");
    expect(params.has("salary_min")).toBe(false);
    expect(params.has("salary_max")).toBe(false);
  });

  it("omits remote_type when set to the «all» sentinel", () => {
    expect(buildParams({ ...empty, remoteType: "all" }, 10).has("remote_type")).toBe(false);
    expect(buildParams({ ...empty, remoteType: "remote" }, 10).get("remote_type")).toBe("remote");
  });

  it("appends multiple values for set-typed filters in insertion order", () => {
    const params = buildParams(
      {
        ...empty,
        seniority: new Set(["middle", "senior"]),
        source: new Set(["hh", "telegram"]),
        skills: new Set(["Python", "SQL"]),
      },
      10,
    );
    expect(params.getAll("seniority")).toEqual(["middle", "senior"]);
    expect(params.getAll("source")).toEqual(["hh", "telegram"]);
    expect(params.getAll("skills")).toEqual(["Python", "SQL"]);
  });

  it("preserves non-zero offset for pagination", () => {
    const params = buildParams({ ...empty, offset: 50 }, 25);
    expect(params.get("offset")).toBe("50");
    expect(params.get("limit")).toBe("25");
  });

  it("appends scalar text filters when present (city / employer / salary bounds)", () => {
    const params = buildParams(
      {
        ...empty,
        city: "Moscow",
        employerName: "Yandex",
        salaryMin: "150000",
        salaryMax: "300000",
      },
      25,
    );
    expect(params.get("city")).toBe("Moscow");
    expect(params.get("employer_name")).toBe("Yandex");
    expect(params.get("salary_min")).toBe("150000");
    expect(params.get("salary_max")).toBe("300000");
  });
});

describe("formatSalary edge cases", () => {
  it("renders only-max branch using fallback when max is explicit zero", () => {
    // The `?? 0` fallback in the only-max branch covers a paranoia case:
    // `salary_rub_max: 0` is theoretically valid for a "до 0 ₽" listing.
    expect(formatSalary({ salary_rub_min: null, salary_rub_max: 0 })).toBe("до 0 ₽");
  });
});
