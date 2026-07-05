# VacancyRadar Dashboard UX Research — 2025-2026

**Scope:** Actionable code-review recommendations for a read-only Russian job-market dashboard (Next.js 15 + Recharts + DuckDB). Style target: Linear / Vercel / Hex / Rill — clean white, data-first, minimal accent (#2563eb).

---

## 1) Layout: Single-Pane with Slide-Over Detail

**Pattern A — Collapsible Filter Rail (Rill, Hex, Mode)**
A narrow left rail (240 px) holds facets. It collapses to icon-only (56 px) and remembers state per user. The main area is a single scrollable canvas. This keeps filters visible without the classic "sidebar eats 30 % of viewport" problem.
- **Trade-off:** Rail consumes horizontal space on laptops; solves it via collapse.
- **Anti-pattern:** Fixed 320 px sidebar that never collapses — wastes space on 13-inch screens and forces horizontal scrolling on tables.

**Pattern B — Top Filter Bar + Contextual Panel (Stripe Apps, Vercel Analytics)**
Filters live in a sticky top bar as chips. When a user selects a vacancy, a slide-over drawer (400–480 px right panel) shows detail + "Похожие вакансии". Main list stays visible underneath.
- **Trade-off:** Loses persistent facet visibility, but gains maximum table width and mental context (no full-page jump).
- **Anti-pattern:** Full-page detail routes (`/vacancy/[id]`) for read-only browsing — breaks scan-and-compare flow.

**Pattern C — Command Palette as Primary Filter Entry (Linear, Raycast)**
`Cmd+K` opens a palette where typing `city:` or `skill:` autocompletes facet values. Surface a `/` shortcut in the search input to teach power users. Chips render below the search bar as applied filters.
- **Trade-off:** Requires discovery; supplement with visible chips, not replace them.
- **Anti-pattern:** Seven separate dropdown widgets visible at all times — visual noise for a read-only dashboard.

---

## 2) Information Density: The Linear Compromise

**Concrete numbers from reference products**
- **Row height:** 36–40 px for tables (Linear issue rows = 38 px). Cards in grid = 280 × 180 px minimum.
- **Font size:** 13 px for data cells (salary, dates), 14 px for body, 12 px for meta (source, channel). Line-height 1.35–1.4.
- **Padding:** 12 px horizontal per cell; 8 px vertical. No vertical borders, only `border-bottom: 1px solid #f0f0f0` (Gray-100).

**Pattern — Condensed Table with Expandable Rich Row**
Default to a dense table. Hovering or clicking a chevron expands the row to 120 px height showing skill chips, employer logo placeholder, and a sparkline. This is Bloomberg density with Notion reveal.
- **Trade-off:** Slightly more complex component, but one view serves both scanning and shallow reading.
- **Anti-pattern:** Card grid as the default for 1,000+ tabular records — cards waste vertical space and break columnar comparison.

---

## 3) Multi-Facet Search: Chips + Palette Hybrid

**Pattern — Top Bar with Inline Filter Tokens (Stripe Dashboard, GitHub Issues)**
Search input sits alone. Applied facets become removable chips to the right of the input: `Москва ×`, `Python ×`, `150–300k ₽ ×`. Clicking the input reveals a popover with 7 facet categories; selecting one opens its value list. A "Clear all" ghost button appears after the second chip.
- **Trade-off:** Compact, scales to 10+ active filters, but requires two clicks per new facet.
- **Anti-pattern:** Seven `<select>` dropdowns in a sidebar — users can't see active filter state at a glance.

**Pattern — `/` Slash Syntax in Search Input (Linear, Notion)**
Typing `/город Москва` or `/навык Python` inside the main search input converts text to a chip on blur/Enter. Display placeholder hint: "Поиск или /фильтр".
- **Trade-off:** Zero UI chrome for power users; discover via placeholder and tooltip.
- **Anti-pattern:** Advanced query syntax without UI fallback — excludes casual users.

---

## 4) Visualization: Micro-Charts First, Full Charts on Demand

**Pattern — Sparklines Inside Table Rows (Hex KPI tables, Vercel Analytics)**
Add a 96 × 24 px sparkline (7-day salary trend or posting velocity) directly in the vacancy table row using a custom SVG or `visx`. It answers "Is this role heating up?" without leaving the list.
- **Trade-off:** Adds one column; use only where the metric is actionable.
- **Anti-pattern:** Rendering a full 300 px Recharts `<LineChart>` inside a table cell — kills scroll performance.

**Pattern — Tremor-style Metric Cards for `/trends`**
Use Recharts (wrapped in shadcn/chart) for the 4 trend cards, but add a `SparkAreaChart` from Tremor as the card background. The main number is 32 px; the sparkline sits behind it at 10 % opacity.
- **Trade-off:** Keeps cards compact while showing trajectory.
- **Anti-pattern:** Four full-width line charts stacked vertically — too much chart chrome for a summary view.

**Library recommendation:** Keep Recharts for full cards. Add `visx` only if you need custom sparklines. Avoid ECharts — too much visual noise out of the box for this aesthetic.

---

## 5) Color: Gray-First, Blue-Accent, Signal-Only Red/Green

**6-token scale (Hex, IBM Carbon light-theme approach)**
| Token | Value | Role |
|-------|-------|------|
| `bg-primary` | `#ffffff` | Canvas |
| `bg-secondary` | `#f8f9fa` | Hover rows, sidebar |
| `border` | `#e5e7eb` | Dividers, card borders |
| `text-primary` | `#111827` | Headlines, primary data |
| `text-secondary` | `#6b7280` | Meta, labels |
| `accent` | `#2563eb` | Active filters, links, selected row |
| `signal-red` | `#dc2626` | Closed/archived vacancies only |
| `signal-green` | `#16a34a` | Salary increase / new posting trend only |

**Rule:** Color is signal, not decoration. Salary ranges use a 4-step blue sequential scale (not green-to-red), because higher salary is not "good" — it is just higher. Use red exclusively for closed/archived.
- **Anti-pattern:** Multi-hued category charts for 20 employers — rainbow palettes obscure meaning. Use blue monochrome + opacity for density.

---

## 6) Typography: Inter + IBM Plex Mono Pairing

**Stack recommendation**
- **UI (headings, filters, buttons):** Inter or Geist Sans (Geist is slightly tighter, feels more "2025"). Weight 500 for labels, 400 for body.
- **Data (salary, dates, IDs, similarity %):** IBM Plex Mono. Its numerals are designed for tabular alignment.

**Mandatory CSS**
```css
font-variant-numeric: tabular-nums;
```
Apply to salary, date columns, and the "N % match" pill. This prevents layout shift when numbers change during filtering.
- **Anti-pattern:** Inter everywhere. Proportional numerals make salary columns ragged and hard to scan vertically.

---

## 7) Empty / Error / Loading: Skeletons with Shape, Never Spinners

**Pattern — Content-Shaped Skeletons (Linear, Vercel, YouTube)**
Render gray placeholder blocks that match the exact geometry of table rows (38 px height, 4 columns at proportional widths). Animate a subtle shimmer over 1.5 s. Never use a centered spinner on a blank canvas.
- **Trade-off:** Requires maintaining a skeleton layout parallel to the real table.
- **Anti-pattern:** Generic `<Spinner size="lg" />` centered in a white void — destroys perceived performance.

**Pattern — Inline Empty State with Action (Stripe Apps, Linear)**
If a filter combo returns 0 results, show a 120 px tall inline panel inside the table area: title "Ничего не найдено", subtitle "Попробуйте убрать фильтр по зарплате", and a ghost button "Сбросить фильтры". Do not redirect to a separate empty page.
- **Anti-pattern:** Blank white table body with no CTA — users assume the app is broken.

**Pattern — Sonner Toasts for Errors (Vercel, Resend)**
Use `sonner` for DuckDB / network errors. Bottom-right, swipe-to-dismiss, pause on hover. Include a "Retry" button inside the toast.
- **Anti-pattern:** Red banner at top of page that pushes layout down on every poll.

---

## 8) Navigation: Unified Explore View with Persistent Tabs

**Pattern — Tabs Inside a Single Layout (Observable Notebook, Modal.com)**
Keep `/` and `/trends` as routes for shareability, but render them as tabs inside one persistent layout. The nav bar shows: **Вакансии** | **Тренды**. Switching tabs preserves applied filters (city, skills) via query-string sync so the trends view reflects the current filter context.
- **Trade-off:** Requires lifting filter state to the layout level.
- **Anti-pattern:** Separate routes with no shared state — users lose filter context when switching views.

**Pattern — Split-Pane for Large Screens (Hex, Rill Canvas)**
On viewports ≥ 1440 px, show the trends tab as a bottom panel (40 % height) and the vacancy table as the top panel (60 % height). Drag handle adjusts ratio. On < 1440 px, fall back to tabs.
- **Trade-off:** Complex responsive logic, but answers "I see a trend, now show me the rows" in one click.
- **Anti-pattern:** Opening trends in a modal over the table — modals block comparison and feel temporary.

**Pattern — Deep-Link Everything (Vercel, Linear)**
Every filter combination and tab selection updates the URL (`?city=moscow&skill=python&view=trends`). Users can share exact dashboard states.
- **Anti-pattern:** Client-side only state — breaks refresh and sharing.

---

*Recommendation priority for next sprint:*
1. Switch to **top filter chips + command palette** (Section 3).
2. Add **tabular-nums** and **IBM Plex Mono** to salary/match columns (Section 6).
3. Replace spinner with **content-shaped skeleton** (Section 7).
4. Unify navigation via **layout-level tabs** with query-string sync (Section 8).
