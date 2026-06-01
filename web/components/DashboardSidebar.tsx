"use client";
import { ExpandableChips } from "@/components/ExpandableChips";
import { FacetSection } from "@/components/FacetSection";
import { FacetSkeleton } from "@/components/FacetSkeleton";
import { MiniBars } from "@/components/MiniBars";
import { formatInt, formatRemoteType, formatSeniority, formatSource } from "@/lib/dashboard-format";
import type { FacetsResponse } from "@/lib/dashboard-types";

const REMOTE_OPTIONS = ["office", "hybrid", "remote", "unknown"];

const SENIORITY_LABELS: Record<string, string> = {
  intern: formatSeniority("intern"),
  junior: formatSeniority("junior"),
  middle: formatSeniority("middle"),
  senior: formatSeniority("senior"),
  lead: formatSeniority("lead"),
  principal: formatSeniority("principal"),
  unknown: formatSeniority("unknown"),
};

export type SidebarProps = {
  facets: FacetsResponse | null;
  open: boolean;
  city: string | null;
  remoteType: string;
  seniority: Set<string>;
  source: Set<string>;
  skills: Set<string>;
  employerName: string | null;
  salaryMin: string;
  salaryMax: string;
  skillSearch: string;
  employerSearch: string;
  setCity: (v: string | null) => void;
  setRemoteType: (v: string) => void;
  setSeniority: (v: Set<string>) => void;
  setSource: (v: Set<string>) => void;
  setSkills: (v: Set<string>) => void;
  setEmployerName: (v: string | null) => void;
  setSalaryMin: (v: string) => void;
  setSalaryMax: (v: string) => void;
  setSkillSearch: (v: string) => void;
  setEmployerSearch: (v: string) => void;
  setOffset: (v: number) => void;
};

function toggleSet(
  setter: (next: Set<string>) => void,
  current: Set<string>,
  value: string,
  setOffset: (v: number) => void,
) {
  const next = new Set(current);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  setter(next);
  setOffset(0);
}

export function DashboardSidebar(props: SidebarProps) {
  const {
    facets,
    open,
    city,
    remoteType,
    seniority,
    source,
    skills,
    employerName,
    salaryMin,
    salaryMax,
    skillSearch,
    employerSearch,
    setCity,
    setRemoteType,
    setSeniority,
    setSource,
    setSkills,
    setEmployerName,
    setSalaryMin,
    setSalaryMax,
    setSkillSearch,
    setEmployerSearch,
    setOffset,
  } = props;

  const cityFacet = facets?.facets.city.slice(0, 20) ?? [];
  const cityCount = facets ? `${formatInt(cityFacet.length)} из ${formatInt(facets.summary.unique_cities)}` : undefined;
  const skillCount = facets ? `${formatInt(facets.facets.skills.length)} из ${formatInt(facets.summary.unique_skills)}` : undefined;
  const employerCount = facets ? `${formatInt(facets.facets.employer_name.length)} из ${formatInt(facets.summary.unique_employers)}` : undefined;
  const skillsFacet =
    facets?.facets.skills
      .filter((item) => item.value.toLowerCase().includes(skillSearch.trim().toLowerCase()))
      .slice(0, 50) ?? [];
  const employerFacet =
    facets?.facets.employer_name
      .filter((item) => item.value.toLowerCase().includes(employerSearch.trim().toLowerCase()))
      .slice(0, 30) ?? [];

  return (
    <aside className="sidebar" data-open={open}>
      <FacetSection title="Город" count={cityCount}>
        {!facets ? (
          <FacetSkeleton chips />
        ) : cityFacet.length > 0 ? (
          <ExpandableChips
            items={cityFacet}
            isActive={(value) => city === value}
            onToggle={(value) => {
              setCity(city === value ? null : value);
              setOffset(0);
            }}
          />
        ) : (
          <p className="empty-facet">нет данных</p>
        )}
      </FacetSection>

      <FacetSection title="Зарплата">
        <div className="salary-grid">
          <input
            className="salary-field"
            inputMode="numeric"
            placeholder="от RUB"
            value={salaryMin}
            onBlur={() => setOffset(0)}
            onChange={(event) => setSalaryMin(event.target.value)}
          />
          <input
            className="salary-field"
            inputMode="numeric"
            placeholder="до RUB"
            value={salaryMax}
            onBlur={() => setOffset(0)}
            onChange={(event) => setSalaryMax(event.target.value)}
          />
        </div>
      </FacetSection>

      <FacetSection title="Удалёнка">
        <div className="radio-list">
          {["all", ...REMOTE_OPTIONS].map((value) => (
            <label className="option" key={value}>
              <input
                checked={remoteType === value}
                name="remote_type"
                type="radio"
                onChange={() => {
                  setRemoteType(value);
                  setOffset(0);
                }}
              />
              {value === "all" ? "все" : formatRemoteType(value)}
            </label>
          ))}
        </div>
        <MiniBars data={facets?.facets.remote_type ?? []} />
      </FacetSection>

      <FacetSection title="Грейд">
        {!facets ? (
          <FacetSkeleton />
        ) : (
          <div className="checkbox-list">
            {facets.facets.seniority.map((item) => (
              <label className="option" key={item.value}>
                <input
                  checked={seniority.has(item.value)}
                  type="checkbox"
                  onChange={() => toggleSet(setSeniority, seniority, item.value, setOffset)}
                />
                {SENIORITY_LABELS[item.value] ?? item.value} · {formatInt(item.count)}
              </label>
            ))}
          </div>
        )}
      </FacetSection>

      <FacetSection title="Навыки" count={skillCount}>
        <input
          className="facet-search"
          placeholder="Найти навык"
          value={skillSearch}
          onChange={(event) => setSkillSearch(event.target.value)}
        />
        {!facets ? (
          <FacetSkeleton chips />
        ) : skillsFacet.length > 0 ? (
          <ExpandableChips
            items={skillsFacet}
            isActive={(value) => skills.has(value)}
            onToggle={(value) => toggleSet(setSkills, skills, value, setOffset)}
          />
        ) : (
          <p className="empty-facet">нет данных</p>
        )}
      </FacetSection>

      <FacetSection title="Работодатель" count={employerCount}>
        <input
          className="facet-search"
          placeholder="Найти работодателя"
          value={employerSearch}
          onChange={(event) => setEmployerSearch(event.target.value)}
        />
        {!facets ? (
          <FacetSkeleton chips />
        ) : employerFacet.length > 0 ? (
          <ExpandableChips
            items={employerFacet}
            isActive={(value) => employerName === value}
            onToggle={(value) => {
              setEmployerName(employerName === value ? null : value);
              setOffset(0);
            }}
          />
        ) : (
          <p className="empty-facet">нет данных</p>
        )}
      </FacetSection>

      <FacetSection title="Источник">
        {!facets ? (
          <FacetSkeleton />
        ) : (
          <>
            <div className="checkbox-list">
              {["hh", "telegram"].map((value) => {
                const count = facets.summary.source_breakdown[value] ?? 0;
                return (
                  <label className="option" key={value}>
                    <input
                      checked={source.has(value)}
                      type="checkbox"
                      onChange={() => {
                        if (source.size === 1 && source.has(value)) return;
                        toggleSet(setSource, source, value, setOffset);
                      }}
                    />
                    {formatSource(value)} · {formatInt(count)}
                  </label>
                );
              })}
            </div>
            <MiniBars data={facets.facets.source} />
          </>
        )}
      </FacetSection>
    </aside>
  );
}
