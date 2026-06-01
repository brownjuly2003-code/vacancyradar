"use client";

import { useEffect, useMemo, useState } from "react";

import { DashboardSidebar } from "@/components/DashboardSidebar";
import { DetailPanel } from "@/components/DetailPanel";
import { KpiRow } from "@/components/KpiRow";
import { ResultsCards } from "@/components/ResultsCards";
import { ResultsTable } from "@/components/ResultsTable";
import { TableSkeleton } from "@/components/TableSkeleton";
import { TabNav } from "@/components/TabNav";
import { buildParams, formatDate, formatInt, formatSource } from "@/lib/dashboard-format";
import type {
  FacetsResponse,
  SearchResponse,
  SearchRow,
  ViewMode,
} from "@/lib/dashboard-types";

const LIMIT = 50;

export default function DashboardPage() {
  const [facets, setFacets] = useState<FacetsResponse | null>(null);
  const [search, setSearch] = useState<SearchResponse | null>(null);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [city, setCity] = useState<string | null>(null);
  const [remoteType, setRemoteType] = useState("all");
  const [seniority, setSeniority] = useState<Set<string>>(new Set());
  const [source, setSource] = useState<Set<string>>(new Set(["hh"]));
  const [skills, setSkills] = useState<Set<string>>(new Set());
  const [employerName, setEmployerName] = useState<string | null>(null);
  const [skillSearch, setSkillSearch] = useState("");
  const [employerSearch, setEmployerSearch] = useState("");
  const [salaryMin, setSalaryMin] = useState("");
  const [salaryMax, setSalaryMax] = useState("");
  const [offset, setOffset] = useState(0);
  const [viewMode, setViewMode] = useState<ViewMode>("table");
  const [urlStateReady, setUrlStateReady] = useState(false);
  const [searchRetryKey, setSearchRetryKey] = useState(0);

  // Auto-pick cards on narrow viewports — table сжимается до 2 колонок и
  // обрезает остальные. Перехватываем только при первом mount, потом
  // юзер может явно переключиться.
  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (window.matchMedia("(max-width: 768px)").matches) {
      setViewMode("cards");
    }
  }, []);
  const [loading, setLoading] = useState(true);
  const [facetsError, setFacetsError] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [detailFor, setDetailFor] = useState<SearchRow | null>(null);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    const initialQuery = new URLSearchParams(window.location.search).get("q") ?? "";
    if (initialQuery) {
      setQuery(initialQuery);
      setDebouncedQuery(initialQuery);
    }
    setUrlStateReady(true);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedQuery(query);
      setOffset(0);
    }, 300);

    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let active = true;

    fetch("/api/facets")
      .then(async (response) => {
        // Parse the body even on 503 — the route's fallback payload preserves
        // the FacetsResponse shape with zero counts plus `error`/`detail`
        // fields. Treating it as a soft-fail (banner) is much better UX than
        // a hard exception (silent broken dashboard).
        const data = (await response.json()) as FacetsResponse;
        if (active) {
          setFacets(data);
          if (!response.ok || data.error) {
            // The backend's `detail` field is intentionally technical (for
            // logs). User-facing copy is set here so the banner reads like
            // an outage notice, not a stack trace.
            setFacetsError(
              data.error === "facets_unavailable"
                ? "Данные временно недоступны — внешнее хранилище превысило egress-квоту. Обычно восстанавливается за 24 часа."
                : (data.detail ?? data.error ?? "Данные временно недоступны"),
            );
          }
        }
      })
      .catch((caught: Error) => {
        if (active) {
          setFacetsError(caught.message);
        }
      });

    return () => {
      active = false;
    };
  }, []);

  const params = useMemo(
    () =>
      buildParams(
        {
          query: debouncedQuery,
          city,
          remoteType,
          seniority,
          source,
          skills,
          employerName,
          salaryMin,
          salaryMax,
          offset,
        },
        LIMIT,
      ),
    [city, debouncedQuery, employerName, offset, remoteType, salaryMax, salaryMin, seniority, skills, source],
  );

  useEffect(() => {
    if (!urlStateReady || typeof window === "undefined") {
      return;
    }
    const url = new URL(window.location.href);
    const trimmedQuery = debouncedQuery.trim();
    if (trimmedQuery) {
      url.searchParams.set("q", trimmedQuery);
    } else {
      url.searchParams.delete("q");
    }
    const nextUrl = `${url.pathname}${url.search}${url.hash}`;
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (nextUrl !== currentUrl) {
      window.history.replaceState(null, "", nextUrl);
    }
  }, [debouncedQuery, urlStateReady]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setSearchError(null);

    fetch(`/api/search?${params.toString()}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error("Не удалось загрузить вакансии");
        }
        return response.json() as Promise<SearchResponse>;
      })
      .then((data) => {
        setSearch(data);
      })
      .catch((caught: Error) => {
        if (caught.name !== "AbortError") {
          setSearch(null);
          setSearchError(caught.message);
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });

    return () => controller.abort();
  }, [params, searchRetryKey]);

  const rows = search?.rows ?? [];
  const total = search?.total ?? 0;
  const totalExact = search?.total_exact ?? true;
  const totalLabel = search?.total_label ?? formatInt(total);
  const from = rows.length === 0 ? 0 : offset + 1;
  const to = offset + rows.length;
  const rangeLabel = searchError
    ? "результаты недоступны"
    : `${formatInt(from)}–${formatInt(to)} из ${formatInt(total)}`;
  const sourceIsDefault = source.size === 1 && source.has("hh");
  const sourceValues = Array.from(source);
  const sourceLabel = sourceIsDefault
    ? "hh.ru core"
    : sourceValues.map(formatSource).join(" + ");
  const currentTotalLabel = search ? formatInt(total) : "—";
  const coverageTotal = facets?.summary.total_vacancies ?? null;
  const hhTotal = facets?.summary.source_breakdown.hh ?? null;
  const telegramTotal = facets?.summary.source_breakdown.telegram ?? null;
  const salaryP50 = facets?.facets.salary_range.p50 ?? null;
  const salaryP90 = facets?.facets.salary_range.p90 ?? null;
  const salaryDisclosure = facets ? Math.round(facets.facets.salary_range.with_salary_pct) : null;
  const topCity = facets?.facets.city[0] ?? null;
  const topSkills = facets?.facets.skills.slice(0, 3).map((item) => item.value).join(" · ") ?? "";
  const hasFilters =
    Boolean(city) ||
    remoteType !== "all" ||
    seniority.size > 0 ||
    !sourceIsDefault ||
    skills.size > 0 ||
    Boolean(employerName) ||
    Boolean(salaryMin) ||
    Boolean(salaryMax) ||
    Boolean(query);

  function clearFilters() {
    setQuery("");
    setCity(null);
    setRemoteType("all");
    setSeniority(new Set());
    setSource(new Set(["hh"]));
    setSkills(new Set());
    setEmployerName(null);
    setSalaryMin("");
    setSalaryMax("");
    setOffset(0);
  }

  function applySourceLens(next: Set<string>) {
    setSource(next);
    setOffset(0);
  }

  return (
    <main className="dashboard">
      <div className="dashboard__inner">
        <header className="dashboard__header">
          <div>
            <h1 className="dashboard__title">IT-рынок вакансий</h1>
            <p className="dashboard__subtitle">
              hh.ru IT-роли + отобранные Telegram-каналы · ежедневное обновление
            </p>
          </div>
          <div className="dashboard__meta mono" aria-busy={!facets}>
            {facets ? (
              <>обновлено · {formatDate(facets.refreshed_at)}</>
            ) : (
              <span className="dashboard__meta-skeleton" aria-hidden="true" />
            )}
          </div>
        </header>
        <TabNav />

        <KpiRow facets={facets} />

        <section className="executive-brief" aria-label="Сводка рынка">
          <div className="executive-brief__main">
            <span className="executive-brief__eyebrow">Рыночный срез</span>
            <h2>{currentTotalLabel} вакансий в текущей выдаче</h2>
            <p>
              Охват: {coverageTotal === null ? "—" : formatInt(coverageTotal)} IT-вакансий
              {hhTotal !== null && telegramTotal !== null
                ? ` · ${formatInt(hhTotal)} hh.ru · ${formatInt(telegramTotal)} Telegram`
                : ""}
            </p>
          </div>
          <div className="executive-brief__signals">
            <div>
              <span>ЗП</span>
              <strong>{salaryP50 ? `${formatInt(Math.round(salaryP50 / 1000))}к ₽` : "—"}</strong>
              <small>{salaryP90 ? `p90 ${formatInt(Math.round(salaryP90 / 1000))}к ₽` : "p90 —"}</small>
            </div>
            <div>
              <span>Топ-город</span>
              <strong>{topCity ? topCity.value : "—"}</strong>
              <small>{topCity ? `${formatInt(topCity.count)} вакансий` : "нет данных"}</small>
            </div>
            <div>
              <span>Раскрытие</span>
              <strong>{salaryDisclosure === null ? "—" : `${salaryDisclosure}% с ЗП`}</strong>
              <small>{topSkills || "скиллы не раскрыты"}</small>
            </div>
          </div>
          <div className="market-lens" role="group" aria-label="Режим рыночного среза">
            <button
              type="button"
              data-active={sourceIsDefault}
              aria-pressed={sourceIsDefault}
              onClick={() => applySourceLens(new Set(["hh"]))}
            >
              hh.ru core
            </button>
            <button
              type="button"
              data-active={source.has("hh") && source.has("telegram")}
              aria-pressed={source.has("hh") && source.has("telegram")}
              onClick={() => applySourceLens(new Set(["hh", "telegram"]))}
            >
              вся база
            </button>
          </div>
        </section>

        <div className="search-row">
          <input
            aria-label="Поиск"
            placeholder="Поиск по названию и описанию"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="drawer-toggle" type="button" onClick={() => setSidebarOpen((value) => !value)}>
            Фильтры
          </button>
        </div>

        {facetsError ? <div className="status-banner">{facetsError}</div> : null}

        <div className="dashboard__grid">
          <DashboardSidebar
            facets={facets}
            open={sidebarOpen}
            city={city}
            remoteType={remoteType}
            seniority={seniority}
            source={source}
            skills={skills}
            employerName={employerName}
            salaryMin={salaryMin}
            salaryMax={salaryMax}
            skillSearch={skillSearch}
            employerSearch={employerSearch}
            setCity={setCity}
            setRemoteType={setRemoteType}
            setSeniority={setSeniority}
            setSource={setSource}
            setSkills={setSkills}
            setEmployerName={setEmployerName}
            setSalaryMin={setSalaryMin}
            setSalaryMax={setSalaryMax}
            setSkillSearch={setSkillSearch}
            setEmployerSearch={setEmployerSearch}
            setOffset={setOffset}
          />

          <section className="content">
            <div className="toolbar">
              <div className="toolbar__left">
                <span className="mono">{rangeLabel}</span>
                {sourceLabel ? <span className="active-filter-chip">{sourceLabel}</span> : null}
                {search?.query_expanded_to && search.query_expanded_to.length > 1 ? (
                  <span
                    className="expansion-chip"
                    title="Запрос автоматически расширен по таксономии скиллов (alias → canonical и обратно)"
                  >
                    + также: {search.query_expanded_to.filter((t) => t.toLowerCase() !== query.trim().toLowerCase()).join(", ")}
                  </span>
                ) : null}
                {hasFilters ? (
                  <button className="clear-button" type="button" onClick={clearFilters}>
                    Сбросить
                  </button>
                ) : null}
              </div>
              <div className="mode-toggle" role="group" aria-label="Режим просмотра">
                <button
                  className="mode-button"
                  data-active={viewMode === "table"}
                  aria-pressed={viewMode === "table"}
                  type="button"
                  onClick={() => setViewMode("table")}
                >
                  Таблица
                </button>
                <button
                  className="mode-button"
                  data-active={viewMode === "cards"}
                  aria-pressed={viewMode === "cards"}
                  type="button"
                  onClick={() => setViewMode("cards")}
                >
                  Карточки
                </button>
              </div>
            </div>

            {loading ? <TableSkeleton rows={10} /> : null}
            {!loading && searchError ? (
              <div className="empty-state empty-state--error" role="alert">
                <p className="empty-state__title">{searchError}</p>
                <p className="empty-state__hint">
                  Это сбой загрузки выдачи, а не пустой результат фильтров.
                </p>
                <button
                  type="button"
                  className="empty-state__action"
                  onClick={() => setSearchRetryKey((value) => value + 1)}
                >
                  Повторить
                </button>
              </div>
            ) : null}

            {!loading && !searchError && rows.length === 0 ? (
              <div className="empty-state">
                <p className="empty-state__title">Ничего не найдено</p>
                <p className="empty-state__hint">
                  Попробуй убрать фильтры или сократить запрос.
                </p>
                {hasFilters ? (
                  <button
                    type="button"
                    className="empty-state__action"
                    onClick={clearFilters}
                  >
                    Сбросить фильтры
                  </button>
                ) : null}
              </div>
            ) : null}

            {!loading && rows.length > 0 && viewMode === "table" ? (
              <ResultsTable rows={rows} onRowClick={setDetailFor} />
            ) : null}

            {!loading && rows.length > 0 && viewMode === "cards" ? (
              <ResultsCards rows={rows} onRowClick={setDetailFor} />
            ) : null}

            {!searchError ? (
              <div className="pager">
                <button
                  type="button"
                  disabled={offset === 0 || loading}
                  onClick={() => setOffset(Math.max(0, offset - LIMIT))}
                >
                  ← Prev
                </button>
                <span className="mono" title={totalExact ? undefined : "точное количество не считалось — COUNT() timeout"}>
                  {formatInt(from)}–{formatInt(to)} из {totalLabel}
                </span>
                <button
                  type="button"
                  disabled={offset + LIMIT >= total || loading}
                  onClick={() => setOffset(offset + LIMIT)}
                >
                  Next →
                </button>
              </div>
            ) : null}
          </section>
        </div>
      </div>
      {detailFor ? (
        <DetailPanel
          row={detailFor}
          onClose={() => setDetailFor(null)}
        />
      ) : null}
    </main>
  );
}
