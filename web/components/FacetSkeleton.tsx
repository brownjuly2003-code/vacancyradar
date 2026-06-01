/**
 * Loading skeleton for individual facet groups inside the sidebar. Two layout
 * variants: `chips` mimics ExpandableChips, default mimics a checkbox list.
 * Pure presentational — extracted from `app/page.tsx` 2026-05-16.
 */
export function FacetSkeleton({ chips = false, rows = 3 }: { chips?: boolean; rows?: number }) {
  return (
    <div
      className={chips ? "chip-list facet-skeleton" : "checkbox-list facet-skeleton"}
      role="status"
      aria-busy="true"
      aria-label="Загрузка фасетов"
    >
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row skeleton-row--inline" key={i} />
      ))}
    </div>
  );
}
