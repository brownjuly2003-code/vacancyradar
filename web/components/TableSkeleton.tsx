/**
 * Loading skeleton for the results table — render N grey rows while data is
 * in flight. Pure presentational, no state. Extracted from `app/page.tsx`
 * 2026-05-16.
 */
export function TableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <div className="skeleton-list" role="status" aria-busy="true" aria-live="polite">
      <span className="visually-hidden">Загрузка результатов</span>
      {Array.from({ length: rows }, (_, i) => (
        <div className="skeleton-row" key={i} />
      ))}
    </div>
  );
}
