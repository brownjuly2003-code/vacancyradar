"use client";
import { useState } from "react";

import { formatInt } from "@/lib/dashboard-format";

const CHIP_COLLAPSED_LIMIT = 12;

/**
 * Generic chip list with a collapsed / expanded mode. Renders the first
 * `CHIP_COLLAPSED_LIMIT` items by default and shows a "+ ещё N" affordance to
 * reveal the rest in a scrollable area. Each chip is a toggle button.
 *
 * Extracted from `app/page.tsx` 2026-05-16.
 */
export function ExpandableChips<T extends { value: string; count: number }>({
  items,
  isActive,
  onToggle,
}: {
  items: T[];
  isActive: (value: string) => boolean;
  onToggle: (value: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? items : items.slice(0, CHIP_COLLAPSED_LIMIT);
  const hidden = items.length - visible.length;

  return (
    <>
      <div
        className={expanded ? "chip-list chip-list--scroll" : "chip-list"}
        style={{ marginTop: 8 }}
      >
        {visible.map((item) => (
          <button
            className="chip"
            data-active={isActive(item.value)}
            key={item.value}
            type="button"
            onClick={() => onToggle(item.value)}
          >
            {item.value} · {formatInt(item.count)}
          </button>
        ))}
      </div>
      {hidden > 0 ? (
        <button
          className="chip-list__more"
          type="button"
          onClick={() => setExpanded(true)}
        >
          + ещё {formatInt(hidden)}
        </button>
      ) : null}
      {expanded && items.length > CHIP_COLLAPSED_LIMIT ? (
        <button
          className="chip-list__more"
          type="button"
          onClick={() => setExpanded(false)}
        >
          свернуть
        </button>
      ) : null}
    </>
  );
}
