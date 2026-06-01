"use client";

import { useEffect, useState } from "react";

import { TabNav } from "@/components/TabNav";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type MarketPulseRow = {
  date: string;
  total_active: number | null;
  new_vacancies: number;
  closed_vacancies: number;
  salary_disclosure_rate: number | null;
  median_active_age_days: number | null;
};

type EmployerTopRow = {
  week_start: string;
  employer_id: string;
  employer_name: string | null;
  new_vacancies: number;
  closed_vacancies: number;
  disclosure_rate: number;
};

type SkillVelocityRow = {
  week_start: string;
  skill: string;
  mentions_this_week: number;
  mentions_prev_week: number;
  delta_pct: number | null;
  rank_this_week: number;
};

type RoleSalaryRow = {
  week_start: string;
  role_canonical: string;
  seniority: string;
  city: string | null;
  n_vacancies: number;
  salary_rub_p25: number;
  salary_rub_median: number;
  salary_rub_p75: number;
};

type ApiResponse<T> = { rows: T[]; refreshed_at: string; error?: string };

const RUB = new Intl.NumberFormat("ru-RU");
const COUNT = new Intl.NumberFormat("ru-RU");

type TrendInsight = {
  label: string;
  value: string;
  detail: string;
};

function useApi<T>(path: string) {
  const [data, setData] = useState<ApiResponse<T> | null>(null);
  const [loading, setLoading] = useState(true);
  const [fatal, setFatal] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetch(path)
      .then((r) => r.json() as Promise<ApiResponse<T>>)
      .then((d) => {
        if (active) setData(d);
      })
      .catch((e: Error) => {
        if (active) setFatal(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [path]);

  return { data, loading, fatal };
}

function ChartCard({
  title,
  subtitle,
  insight,
  loading,
  empty,
  children,
}: {
  title: string;
  subtitle?: string;
  insight?: TrendInsight;
  loading: boolean;
  empty: boolean;
  children: React.ReactNode;
}) {
  return (
    <section className="trend-card">
      <header className="trend-card__head">
        <h2>{title}</h2>
        {subtitle ? <p>{subtitle}</p> : null}
      </header>
      {!loading && !empty && insight ? (
        <div className="trend-card__insight">
          <span>{insight.label}</span>
          <strong>{insight.value}</strong>
          <small>{insight.detail}</small>
        </div>
      ) : null}
      <div className="trend-card__body">
        {loading ? <div className="trend-card__placeholder">загрузка…</div> : null}
        {!loading && empty ? (
          <div className="trend-card__placeholder">нет данных за период</div>
        ) : null}
        {!loading && !empty ? children : null}
      </div>
    </section>
  );
}

function TrendsBrief() {
  const { data, loading } = useApi<MarketPulseRow>("/api/trends/market_pulse");
  const rows = data?.rows ?? [];
  const latest = rows.at(-1);
  const previous = rows.at(-2);
  const recentRows = rows.slice(-7);
  const weeklyNew = recentRows.reduce((total, row) => total + row.new_vacancies, 0);
  const delta = latest && previous ? latest.new_vacancies - previous.new_vacancies : null;
  const disclosure =
    latest?.salary_disclosure_rate === null || latest?.salary_disclosure_rate === undefined
      ? null
      : `${Math.round(latest.salary_disclosure_rate * 100)}%`;

  return (
    <section className="trends-brief">
      <div className="trends-brief__main">
        <span className="trends-brief__eyebrow">Недельный вывод</span>
        <h2>
          {loading || !latest
            ? "Собираю агрегаты рынка"
            : `${COUNT.format(weeklyNew)} новых сигналов за последние 7 дат`}
        </h2>
        <p>
          {loading || !latest
            ? "После загрузки здесь появится краткий управленческий срез по притоку, закрытиям и качеству зарплатных данных."
            : `Последняя точка ${latest.date}: ${COUNT.format(latest.new_vacancies)} новых, ${COUNT.format(latest.closed_vacancies)} закрытых.`}
        </p>
      </div>
      <div className="trends-brief__signals">
        <div>
          <span>Темп</span>
          <strong>{delta === null ? "—" : `${delta >= 0 ? "+" : ""}${COUNT.format(delta)}`}</strong>
          <small>к предыдущей точке</small>
        </div>
        <div>
          <span>Активные</span>
          <strong>{latest?.total_active ? COUNT.format(latest.total_active) : "—"}</strong>
          <small>последний снимок</small>
        </div>
        <div>
          <span>Зарплаты</span>
          <strong>{disclosure ?? "—"}</strong>
          <small>доля с вилкой</small>
        </div>
      </div>
    </section>
  );
}

function MarketPulseChart() {
  const { data, loading } = useApi<MarketPulseRow>("/api/trends/market_pulse");
  const rows = data?.rows ?? [];
  const latest = rows.at(-1);
  const previous = rows.at(-2);
  const delta = latest && previous ? latest.new_vacancies - previous.new_vacancies : null;
  const disclosure =
    latest?.salary_disclosure_rate === null || latest?.salary_disclosure_rate === undefined
      ? "зарплатная база пока не раскрыта"
      : `зарплата раскрыта в ${Math.round(latest.salary_disclosure_rate * 100)}% активных`;
  return (
    <ChartCard
      title="Market Pulse"
      subtitle="новые / закрытые IT-вакансии за день"
      insight={
        latest
          ? {
              label: "Вывод",
              value: `${COUNT.format(latest.new_vacancies)} новых за ${latest.date}`,
              detail:
                delta === null
                  ? `${disclosure}; закрытых ${COUNT.format(latest.closed_vacancies)}`
                  : `${delta >= 0 ? "+" : ""}${COUNT.format(delta)} к прошлой точке; ${disclosure}`,
            }
          : undefined
      }
      loading={loading}
      empty={rows.length === 0}
    >
      <ResponsiveContainer width="100%" height={260}>
        <LineChart data={rows} margin={{ top: 16, right: 24, bottom: 16, left: 8 }}>
          <CartesianGrid stroke="#e5e7eb" strokeDasharray="2 4" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} />
          <Tooltip />
          <Legend wrapperStyle={{ fontSize: 12 }} />
          <Line type="monotone" dataKey="new_vacancies" name="new" stroke="#2563eb" strokeWidth={2} dot={false} />
          <Line type="monotone" dataKey="closed_vacancies" name="closed" stroke="#9ca3af" strokeWidth={2} dot={false} />
        </LineChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function EmployerTopChart() {
  const { data, loading } = useApi<EmployerTopRow>("/api/trends/employer_top");
  const rows = (data?.rows ?? []).slice(0, 10);
  const week = rows[0]?.week_start;
  const leader = rows[0];
  const top3 = rows.slice(0, 3).reduce((total, row) => total + row.new_vacancies, 0);
  return (
    <ChartCard
      title="Top Hirers"
      subtitle={week ? `неделя ${week}` : "кто больше всех нанимает в IT"}
      insight={
        leader
          ? {
              label: "Вывод",
              value: `${leader.employer_name ?? leader.employer_id}: ${COUNT.format(leader.new_vacancies)} новых`,
              detail: `топ-3 работодателя дают ${COUNT.format(top3)} новых вакансий; раскрытие у лидера ${Math.round(leader.disclosure_rate * 100)}%`,
            }
          : undefined
      }
      loading={loading}
      empty={rows.length === 0}
    >
      <ResponsiveContainer width="100%" height={Math.max(260, rows.length * 22)}>
        <BarChart data={rows} layout="vertical" margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke="#e5e7eb" strokeDasharray="2 4" />
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis
            type="category"
            dataKey="employer_name"
            width={140}
            tick={{ fontSize: 11 }}
            tickFormatter={(value) => {
              const text = String(value ?? "");
              return text.length > 18 ? `${text.slice(0, 17)}...` : text;
            }}
            interval={0}
          />
          <Tooltip />
          <Bar dataKey="new_vacancies" name="new" fill="#2563eb" radius={[0, 4, 4, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

function SkillVelocityChart() {
  const { data, loading } = useApi<SkillVelocityRow>("/api/trends/skill_velocity");
  const rows = (data?.rows ?? []).slice(0, 20);
  const topSkill = rows[0];
  const fastest = rows
    .filter((row) => row.delta_pct !== null)
    .sort((a, b) => (b.delta_pct ?? 0) - (a.delta_pct ?? 0))[0];
  return (
    <ChartCard
      title="Skill Velocity"
      subtitle="топ IT-скиллов недели + изменение к прошлой"
      insight={
        topSkill
          ? {
              label: "Вывод",
              value: `${topSkill.skill}: ${COUNT.format(topSkill.mentions_this_week)} упоминаний`,
              detail: fastest
                ? `самый резкий прирост: ${fastest.skill} +${(fastest.delta_pct ?? 0).toFixed(0)}%`
                : "прирост к прошлой неделе пока не рассчитан",
            }
          : undefined
      }
      loading={loading}
      empty={rows.length === 0}
    >
      <table className="trend-table">
        <thead>
          <tr>
            <th>#</th>
            <th>skill</th>
            <th className="num">mentions</th>
            <th className="num">prev</th>
            <th className="num">Δ%</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.skill}>
              <td>{r.rank_this_week}</td>
              <td>{r.skill}</td>
              <td className="num">{r.mentions_this_week}</td>
              <td className="num">{r.mentions_prev_week}</td>
              <td className="num">
                {r.delta_pct === null ? "—" : `${r.delta_pct > 0 ? "+" : ""}${r.delta_pct.toFixed(0)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </ChartCard>
  );
}

function RoleSalaryChart() {
  const { data, loading } = useApi<RoleSalaryRow>("/api/trends/role_salary");
  const rows = data?.rows ?? [];
  // национальный rollup (city=null) для последней недели по медианам
  const national = rows.filter((r) => r.city === null).slice(0, 12);
  const leader = national[0];
  return (
    <ChartCard
      title="Role Salary"
      subtitle="IT-медианы по роли+seniority (national rollup)"
      insight={
        leader
          ? {
              label: "Вывод",
              value: `${leader.role_canonical} · ${leader.seniority}: ${RUB.format(leader.salary_rub_median)} ₽`,
              detail: `выборка ${COUNT.format(leader.n_vacancies)} вакансий; p25–p75 ${RUB.format(leader.salary_rub_p25)}–${RUB.format(leader.salary_rub_p75)} ₽`,
            }
          : undefined
      }
      loading={loading}
      empty={national.length === 0}
    >
      <ResponsiveContainer width="100%" height={Math.max(260, national.length * 24)}>
        <BarChart data={national} layout="vertical" margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke="#e5e7eb" strokeDasharray="2 4" />
          <XAxis
            type="number"
            tick={{ fontSize: 11 }}
            tickFormatter={(v) => `${(v / 1000).toFixed(0)}K`}
          />
          <YAxis
            type="category"
            dataKey={(r: RoleSalaryRow) => `${r.role_canonical} · ${r.seniority}`}
            width={170}
            tick={{ fontSize: 11 }}
            interval={0}
          />
          <Tooltip
            formatter={(value: number) => `${RUB.format(value)} ₽`}
            labelFormatter={(label) => label as string}
          />
          <Bar dataKey="salary_rub_median" name="median" radius={[0, 4, 4, 0]}>
            {national.map((_, idx) => (
              <Cell key={idx} fill="#2563eb" />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}

export default function TrendsPage() {
  return (
    <main className="trends">
      <div className="trends__inner">
        <header className="trends__header">
          <div>
            <h1 className="trends__title">VacancyRadar</h1>
            <p className="trends__subtitle">недельная агрегация IT-рынка</p>
          </div>
        </header>
        <TabNav />
        <TrendsBrief />

        <div className="trends__grid">
          <MarketPulseChart />
          <EmployerTopChart />
          <SkillVelocityChart />
          <RoleSalaryChart />
        </div>
      </div>
    </main>
  );
}
