// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FacetSection } from "@/components/FacetSection";

describe("FacetSection", () => {
  it("renders title and children inside <details>", () => {
    const { container } = render(
      <FacetSection title="Города">
        <div data-testid="child">Москва · 100</div>
      </FacetSection>,
    );
    expect(container.querySelector("details")).toBeInTheDocument();
    expect(screen.getByText("Города")).toBeInTheDocument();
    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("renders count slot when provided", () => {
    render(
      <FacetSection title="Города" count={<span data-testid="cnt">47</span>}>
        <span />
      </FacetSection>,
    );
    expect(screen.getByTestId("cnt")).toHaveTextContent("47");
  });

  it("is open by default for discoverability", () => {
    const { container } = render(
      <FacetSection title="Roles">
        <span />
      </FacetSection>,
    );
    const details = container.querySelector("details");
    expect(details).toHaveAttribute("open");
  });
});
