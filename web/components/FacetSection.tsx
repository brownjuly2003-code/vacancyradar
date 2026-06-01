import type { ReactNode } from "react";

/**
 * Collapsible filter group with a title row that shows an optional count.
 * Renders a native `<details>` element so keyboard/AT semantics come for free.
 * Extracted from `app/page.tsx` 2026-05-16.
 */
export function FacetSection({
  title,
  count,
  children,
}: {
  title: string;
  count?: ReactNode;
  children: ReactNode;
}) {
  return (
    <details className="facet" open>
      <summary className="facet__summary">
        <span className="facet__button">
          <span>{title}</span>
          <span>{count ?? ""}</span>
        </span>
      </summary>
      <div className="facet__body">{children}</div>
    </details>
  );
}
