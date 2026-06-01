// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ResultsTable } from "@/components/ResultsTable";
import type { SearchRow } from "@/lib/dashboard-types";

const ROWS: SearchRow[] = [
  {
    vacancy_id: "hh:1",
    title: "Senior Python Engineer",
    employer_name: "Acme",
    salary_rub_min: 200000,
    salary_rub_max: 300000,
    salary_currency: "RUR",
    city: "Москва",
    region: "central",
    remote_type: "remote",
    seniority: "senior",
    description_teaser: null,
    skills: null,
    source: "hh",
    source_url: "https://hh.ru/vacancy/1",
    posted_at: "2026-05-24T05:30:00Z",
    first_seen_at: "2026-05-24T05:30:00Z",
    last_seen_at: "2026-05-24T05:30:00Z",
  },
  {
    vacancy_id: "tg:demo:42",
    title: "Middle Data Engineer",
    employer_name: null,
    salary_rub_min: null,
    salary_rub_max: null,
    salary_currency: null,
    city: null,
    region: null,
    remote_type: "unknown",
    seniority: "middle",
    description_teaser: null,
    skills: null,
    source: "telegram",
    source_url: null,
    posted_at: null,
    first_seen_at: "2026-05-23T00:00:00Z",
    last_seen_at: "2026-05-23T00:00:00Z",
  },
];

describe("ResultsTable", () => {
  it("renders one <tr> per row plus the header row", () => {
    const { container } = render(<ResultsTable rows={ROWS} onRowClick={() => undefined} />);
    const dataRows = container.querySelectorAll("tbody tr");
    expect(dataRows).toHaveLength(2);
  });

  it("shows titles and employers", () => {
    render(<ResultsTable rows={ROWS} onRowClick={() => undefined} />);
    expect(screen.getByText("Senior Python Engineer")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
  });

  it("renders dash for missing employer/city", () => {
    render(<ResultsTable rows={ROWS} onRowClick={() => undefined} />);
    // The TG row has employer/city null → must surface as "—" (em-dash)
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2); // employer + city on row 2
  });

  it("tags rows with data-source so CSS can style hh vs telegram", () => {
    const { container } = render(
      <ResultsTable rows={ROWS} onRowClick={() => undefined} />,
    );
    const sources = Array.from(
      container.querySelectorAll("tbody tr"),
    ).map((tr) => tr.getAttribute("data-source"));
    expect(sources).toEqual(["hh", "telegram"]);
  });

  it("invokes onRowClick with the clicked row payload", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<ResultsTable rows={ROWS} onRowClick={onClick} />);
    await user.click(screen.getByText("Senior Python Engineer"));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onClick.mock.calls[0][0]).toMatchObject({ vacancy_id: "hh:1" });
  });

  it("renders header columns in Russian", () => {
    render(<ResultsTable rows={ROWS} onRowClick={() => undefined} />);
    expect(screen.getByText("Вакансия")).toBeInTheDocument();
    expect(screen.getByText("Зарплата")).toBeInTheDocument();
    expect(screen.getByText("Грейд")).toBeInTheDocument();
  });

  it("renders empty body when rows is empty", () => {
    const { container } = render(<ResultsTable rows={[]} onRowClick={() => undefined} />);
    expect(container.querySelectorAll("tbody tr")).toHaveLength(0);
    // Header still present
    expect(container.querySelectorAll("thead tr")).toHaveLength(1);
  });
});
