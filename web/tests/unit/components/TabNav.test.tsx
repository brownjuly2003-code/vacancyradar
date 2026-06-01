// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TabNav } from "@/components/TabNav";

// Next App Router hook lives in `next/navigation`. We mock it per-test so the
// component sees a deterministic pathname without a real Router context.
let mockedPathname = "/";
vi.mock("next/navigation", () => ({
  usePathname: () => mockedPathname,
}));

describe("TabNav", () => {
  it("renders both tab links", () => {
    mockedPathname = "/";
    render(<TabNav />);
    expect(screen.getByText("Вакансии")).toBeInTheDocument();
    expect(screen.getByText("Тренды")).toBeInTheDocument();
  });

  it("marks home as active when on /", () => {
    mockedPathname = "/";
    render(<TabNav />);
    const home = screen.getByText("Вакансии").closest("a")!;
    const trends = screen.getByText("Тренды").closest("a")!;
    expect(home).toHaveAttribute("data-active", "true");
    expect(home).toHaveAttribute("aria-current", "page");
    expect(trends).toHaveAttribute("data-active", "false");
    expect(trends).not.toHaveAttribute("aria-current");
  });

  it("marks trends as active when on /trends", () => {
    mockedPathname = "/trends";
    render(<TabNav />);
    expect(screen.getByText("Вакансии").closest("a")).toHaveAttribute(
      "data-active",
      "false",
    );
    expect(screen.getByText("Тренды").closest("a")).toHaveAttribute(
      "data-active",
      "true",
    );
  });

  it("matches /trends prefix paths (e.g. /trends/weekly)", () => {
    mockedPathname = "/trends/weekly";
    render(<TabNav />);
    expect(screen.getByText("Тренды").closest("a")).toHaveAttribute(
      "data-active",
      "true",
    );
    // Home should not match `/trends/weekly` even though they share root
    expect(screen.getByText("Вакансии").closest("a")).toHaveAttribute(
      "data-active",
      "false",
    );
  });
});
