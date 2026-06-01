// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ExpandableChips } from "@/components/ExpandableChips";

const items = Array.from({ length: 20 }, (_, i) => ({
  value: `Skill${i}`,
  count: 100 - i,
}));

describe("ExpandableChips", () => {
  it("renders only the first 12 chips collapsed", () => {
    render(
      <ExpandableChips items={items} isActive={() => false} onToggle={() => undefined} />,
    );
    expect(screen.queryByText(/Skill0/)).toBeInTheDocument();
    expect(screen.queryByText(/Skill11/)).toBeInTheDocument();
    expect(screen.queryByText(/Skill12/)).not.toBeInTheDocument();
  });

  it("shows '+ ещё N' affordance when items exceed the collapsed limit", () => {
    render(
      <ExpandableChips items={items} isActive={() => false} onToggle={() => undefined} />,
    );
    expect(screen.getByText(/\+ ещё 8/)).toBeInTheDocument();
  });

  it("does not show 'ещё' button when items.length <= 12", () => {
    render(
      <ExpandableChips
        items={items.slice(0, 5)}
        isActive={() => false}
        onToggle={() => undefined}
      />,
    );
    expect(screen.queryByText(/\+ ещё/)).not.toBeInTheDocument();
  });

  it("expands to show all chips when '+ ещё' is clicked, with a collapse button", async () => {
    const user = userEvent.setup();
    render(
      <ExpandableChips items={items} isActive={() => false} onToggle={() => undefined} />,
    );
    await user.click(screen.getByText(/\+ ещё 8/));
    expect(screen.getByText(/Skill19/)).toBeInTheDocument();
    expect(screen.getByText(/свернуть/)).toBeInTheDocument();
  });

  it("invokes onToggle with the chip value", async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(
      <ExpandableChips items={items} isActive={() => false} onToggle={onToggle} />,
    );
    await user.click(screen.getByText(/Skill3 · 97/));
    expect(onToggle).toHaveBeenCalledWith("Skill3");
  });

  it("marks active chips via data-active attribute", () => {
    render(
      <ExpandableChips
        items={items.slice(0, 3)}
        isActive={(v) => v === "Skill1"}
        onToggle={() => undefined}
      />,
    );
    const skill1 = screen.getByText(/Skill1 · 99/).closest("button")!;
    const skill0 = screen.getByText(/Skill0 · 100/).closest("button")!;
    expect(skill1).toHaveAttribute("data-active", "true");
    expect(skill0).toHaveAttribute("data-active", "false");
  });
});
