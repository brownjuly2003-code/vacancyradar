from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb
import polars as pl
import pytest

from src.transform.slim_export import SLIM_ACTIVE_SCHEMA
from src.transform.weekly_aggregates import (
    WEEKLY_EMPLOYER_TOP_SCHEMA,
    WEEKLY_MARKET_PULSE_SCHEMA,
    WEEKLY_ROLE_SALARY_SCHEMA,
    WEEKLY_SKILL_VELOCITY_SCHEMA,
    build_all_weekly,
    build_weekly_employer_top,
    build_weekly_market_pulse,
    build_weekly_role_salary,
    build_weekly_skill_velocity,
    parse_role,
    write_weekly_aggregates,
)


# ---- helpers ----


def _make_events_db(path: Path, events: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    try:
        con.execute(
            """
            CREATE TABLE events (
                event_id    VARCHAR PRIMARY KEY,
                vacancy_id  VARCHAR NOT NULL,
                employer_id VARCHAR,
                ts          TIMESTAMP NOT NULL,
                type        VARCHAR NOT NULL,
                payload     VARCHAR,
                source      VARCHAR NOT NULL
            )
            """
        )
        for e in events:
            con.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    e.get("event_id", str(uuid.uuid4())),
                    e["vacancy_id"],
                    e.get("employer_id"),
                    e["ts"],
                    e["type"],
                    json.dumps(e["payload"]) if e.get("payload") else None,
                    e.get("source", "hh"),
                ],
            )
    finally:
        con.close()
    return path


def _slim_row(**overrides) -> dict:
    base = {
        "vacancy_id": "hh:1",
        "title": "Data Analyst",
        "employer_id": "hh:100",
        "employer_name": "ACME",
        "salary_rub_min": 200_000,
        "salary_rub_max": 300_000,
        "salary_currency": "RUR",
        "salary_disclosed": True,
        "city": "Москва",
        "region": "ЦФО",
        "remote_type": "office",
        "seniority": "middle",
        "description_teaser": None,
        "skills": ["Python"],
        "source": "hh",
        "source_url": "https://hh.ru/vacancy/1",
        "first_seen_at": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
        "posted_at": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return base


def _slim_df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SLIM_ACTIVE_SCHEMA)


# ---- parse_role ----


def test_parse_role_data_specialisations():
    assert parse_role("Data Analyst") == "data_analyst"
    assert parse_role("Senior Data Engineer") == "data_engineer"
    assert parse_role("Дата сайентист") == "data_scientist"
    assert parse_role("Аналитик данных") == "data_analyst"


def test_parse_role_engineer_disciplines():
    assert parse_role("Backend Developer") == "backend_engineer"
    assert parse_role("Frontend Engineer") == "frontend_engineer"
    assert parse_role("DevOps инженер") == "devops"
    assert parse_role("QA Automation Engineer") == "qa_engineer"


def test_parse_role_other_for_non_tech():
    assert parse_role("Курьер") == "other"
    assert parse_role("Бортпроводник") == "other"
    assert parse_role(None) == "other"
    assert parse_role("") == "other"


def test_parse_role_falls_back_to_generic_engineer():
    assert parse_role("Software Developer") == "engineer"
    assert parse_role("Программист 1С") == "engineer"


# ---- market_pulse ----


def test_market_pulse_empty_when_no_events(tmp_path: Path):
    db = tmp_path / "events.duckdb"  # не существует
    df = build_weekly_market_pulse(db, _slim_df([]), today=date(2026, 4, 27))
    assert df.is_empty()
    assert dict(df.schema) == WEEKLY_MARKET_PULSE_SCHEMA


def test_market_pulse_counts_posted_closed(tmp_path: Path):
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [
            {"vacancy_id": "v1", "ts": datetime(2026, 4, 25, 10, 0), "type": "appeared"},
            {"vacancy_id": "v2", "ts": datetime(2026, 4, 25, 11, 0), "type": "appeared"},
            {"vacancy_id": "v1", "ts": datetime(2026, 4, 26, 10, 0), "type": "closed"},
        ],
    )
    slim = _slim_df([
        _slim_row(vacancy_id="hh:1", posted_at=datetime(2026, 4, 25, 10, tzinfo=timezone.utc)),
        _slim_row(vacancy_id="hh:2", posted_at=datetime(2026, 4, 25, 11, tzinfo=timezone.utc)),
    ])
    df = build_weekly_market_pulse(db, slim, today=date(2026, 4, 27))
    assert dict(df.schema) == WEEKLY_MARKET_PULSE_SCHEMA
    by_date = {r["date"]: r for r in df.to_dicts()}
    assert by_date[date(2026, 4, 25)]["new_vacancies"] == 2
    assert by_date[date(2026, 4, 25)]["closed_vacancies"] == 0
    assert by_date[date(2026, 4, 26)]["new_vacancies"] == 0
    assert by_date[date(2026, 4, 26)]["closed_vacancies"] == 1


def test_market_pulse_ignores_appeared_backfill_spike_for_new_vacancies(tmp_path: Path):
    today = date(2026, 4, 27)
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [
            {"vacancy_id": f"v{i}", "ts": datetime(2026, 4, 27, 10, 0), "type": "appeared"}
            for i in range(50)
        ],
    )
    slim = _slim_df([
        _slim_row(
            vacancy_id="hh:1",
            first_seen_at=datetime(2026, 4, 27, 10, tzinfo=timezone.utc),
            posted_at=datetime(2026, 4, 20, 10, tzinfo=timezone.utc),
        ),
    ])

    df = build_weekly_market_pulse(db, slim, today=today)
    by_date = {r["date"]: r for r in df.to_dicts()}

    assert by_date[date(2026, 4, 20)]["new_vacancies"] == 1
    assert by_date[today]["new_vacancies"] == 0
    assert by_date[today]["total_active"] == 1


def test_market_pulse_today_carries_active_metrics(tmp_path: Path):
    today = date(2026, 4, 27)
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [{"vacancy_id": "v1", "ts": datetime(2026, 4, 27, 10, 0), "type": "appeared"}],
    )
    slim = _slim_df([
        _slim_row(vacancy_id="hh:1", salary_disclosed=True),
        _slim_row(vacancy_id="hh:2", salary_disclosed=False),
    ])
    df = build_weekly_market_pulse(db, slim, today=today)
    today_row = df.filter(pl.col("date") == today).to_dicts()[0]
    assert today_row["total_active"] == 2
    assert today_row["salary_disclosure_rate"] == 0.5


# ---- employer_top ----


def test_employer_top_empty_when_no_events(tmp_path: Path):
    db = tmp_path / "events.duckdb"
    df = build_weekly_employer_top(db, _slim_df([]), today=date(2026, 4, 27))
    assert df.is_empty()
    assert dict(df.schema) == WEEKLY_EMPLOYER_TOP_SCHEMA


def test_employer_top_aggregates_per_week(tmp_path: Path):
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [
            # Неделя 2026-04-20 (понедельник)
            {"vacancy_id": "v1", "employer_id": "100", "ts": datetime(2026, 4, 20, 10), "type": "appeared"},
            {"vacancy_id": "v2", "employer_id": "100", "ts": datetime(2026, 4, 21, 10), "type": "appeared"},
            {"vacancy_id": "v3", "employer_id": "200", "ts": datetime(2026, 4, 22, 10), "type": "appeared"},
            # Неделя 2026-04-27
            {"vacancy_id": "v4", "employer_id": "100", "ts": datetime(2026, 4, 27, 10), "type": "appeared"},
        ],
    )
    slim = _slim_df([
        _slim_row(vacancy_id="hh:v1", employer_id="hh:100", employer_name="ACME"),
        _slim_row(vacancy_id="hh:v2", employer_id="hh:200", employer_name="BetaCorp"),
    ])
    df = build_weekly_employer_top(db, slim, today=date(2026, 4, 27))
    assert df.height == 3  # ACME×2 weeks + BetaCorp×1 week
    week1_acme = df.filter(
        (pl.col("week_start") == date(2026, 4, 20))
        & (pl.col("employer_id") == "hh:100")
    ).to_dicts()[0]
    assert week1_acme["new_vacancies"] == 2
    assert week1_acme["employer_name"] == "ACME"


def test_employer_top_disclosure_rate_from_slim(tmp_path: Path):
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [{"vacancy_id": "v1", "employer_id": "100", "ts": datetime(2026, 4, 20, 10), "type": "appeared"}],
    )
    slim = _slim_df([
        _slim_row(vacancy_id="hh:a", employer_id="hh:100", employer_name="ACME", salary_disclosed=True),
        _slim_row(vacancy_id="hh:b", employer_id="hh:100", employer_name="ACME", salary_disclosed=False),
    ])
    df = build_weekly_employer_top(db, slim, today=date(2026, 4, 27))
    assert df.to_dicts()[0]["disclosure_rate"] == 0.5


def test_employer_top_disclosure_excludes_outlier_nulled(tmp_path: Path):
    # salary_disclosed=True остаётся флагом employer-transparency даже
    # если outlier-policy [10k, 5M] обнулила salary_rub_*. Но
    # disclosure_rate должен считать только usable rows (KM audit
    # 2026-05-17 P2): иначе rate завышен и расходится с salary_rub_median
    # которая берётся из not-null значений.
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [{"vacancy_id": "v1", "employer_id": "100", "ts": datetime(2026, 4, 20, 10), "type": "appeared"}],
    )
    slim = _slim_df([
        # usable: disclosed + valid salary → counts
        _slim_row(vacancy_id="hh:a", employer_id="hh:100", employer_name="ACME", salary_disclosed=True),
        # outlier-nulled: disclosed но both salary_rub_* null → НЕ counts
        _slim_row(
            vacancy_id="hh:b",
            employer_id="hh:100",
            employer_name="ACME",
            salary_disclosed=True,
            salary_rub_min=None,
            salary_rub_max=None,
        ),
    ])
    df = build_weekly_employer_top(db, slim, today=date(2026, 4, 27))
    # 1 usable / 2 total = 0.5 (раньше было бы 1.0 — оба флаг True)
    assert df.to_dicts()[0]["disclosure_rate"] == 0.5


def test_market_pulse_disclosure_excludes_outlier_nulled(tmp_path: Path):
    today = date(2026, 4, 27)
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [{"vacancy_id": "v1", "ts": datetime(2026, 4, 27, 10, 0), "type": "appeared"}],
    )
    slim = _slim_df([
        # usable
        _slim_row(vacancy_id="hh:1", salary_disclosed=True),
        # outlier-nulled — раньше попадал в disclosure_rate как True
        _slim_row(
            vacancy_id="hh:2",
            salary_disclosed=True,
            salary_rub_min=None,
            salary_rub_max=None,
        ),
        # never-disclosed
        _slim_row(vacancy_id="hh:3", salary_disclosed=False),
    ])
    df = build_weekly_market_pulse(db, slim, today=today)
    today_row = df.filter(pl.col("date") == today).to_dicts()[0]
    # 1 usable / 3 total ≈ 0.333 (раньше было бы 2/3 = 0.667)
    assert today_row["total_active"] == 3
    assert today_row["salary_disclosure_rate"] == pytest.approx(1 / 3)


def test_market_pulse_disclosure_keeps_partial_salary(tmp_path: Path):
    # Outlier-policy null'ит каждое поле независимо. Если только одно из
    # min/max nulled (например, нижняя граница 5k → null, верхняя 200k →
    # сохранена) — это всё ещё usable salary. Filter использует OR.
    today = date(2026, 4, 27)
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [{"vacancy_id": "v1", "ts": datetime(2026, 4, 27, 10, 0), "type": "appeared"}],
    )
    slim = _slim_df([
        _slim_row(
            vacancy_id="hh:1",
            salary_disclosed=True,
            salary_rub_min=None,
            salary_rub_max=200_000,
        ),
    ])
    df = build_weekly_market_pulse(db, slim, today=today)
    today_row = df.filter(pl.col("date") == today).to_dicts()[0]
    assert today_row["salary_disclosure_rate"] == 1.0


# ---- skill_velocity ----


def test_skill_velocity_empty_when_no_skills():
    df = build_weekly_skill_velocity(_slim_df([]), today=date(2026, 4, 27))
    assert df.is_empty()
    assert dict(df.schema) == WEEKLY_SKILL_VELOCITY_SCHEMA


def test_skill_velocity_counts_per_week():
    """vacancies appeared в разные недели → mentions считаются per week_start."""
    week1_dt = datetime(2026, 4, 20, 10, tzinfo=timezone.utc)
    week2_dt = datetime(2026, 4, 27, 10, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id="hh:1", first_seen_at=week1_dt, skills=["Python", "Django"]),
        _slim_row(vacancy_id="hh:2", first_seen_at=week1_dt, skills=["Python"]),
        _slim_row(vacancy_id="hh:3", first_seen_at=week2_dt, skills=["Python", "Django", "Redis"]),
    ])
    df = build_weekly_skill_velocity(slim, today=date(2026, 4, 27))
    rows = {(r["week_start"], r["skill"]): r for r in df.to_dicts()}
    assert rows[(date(2026, 4, 20), "Python")]["mentions_this_week"] == 2
    assert rows[(date(2026, 4, 27), "Python")]["mentions_this_week"] == 1
    assert rows[(date(2026, 4, 27), "Python")]["mentions_prev_week"] == 2


def test_skill_velocity_delta_pct_null_when_prev_zero():
    """Skill, появившийся первой неделей → delta_pct=NULL (не Inf)."""
    week_dt = datetime(2026, 4, 20, 10, tzinfo=timezone.utc)
    slim = _slim_df([_slim_row(skills=["Python"], first_seen_at=week_dt)])
    df = build_weekly_skill_velocity(slim, today=date(2026, 4, 27))
    row = df.to_dicts()[0]
    assert row["mentions_prev_week"] == 0
    assert row["delta_pct"] is None


def test_skill_velocity_rank_descending_by_mentions():
    week_dt = datetime(2026, 4, 27, 10, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id=f"hh:{i}", first_seen_at=week_dt, skills=["Python"])
        for i in range(5)
    ] + [
        _slim_row(vacancy_id="hh:django", first_seen_at=week_dt, skills=["Django"]),
    ])
    df = build_weekly_skill_velocity(slim, today=date(2026, 4, 27))
    by_skill = {r["skill"]: r for r in df.to_dicts()}
    assert by_skill["Python"]["rank_this_week"] == 1
    assert by_skill["Django"]["rank_this_week"] == 2


# ---- role_salary ----


def test_role_salary_empty_when_no_data():
    df = build_weekly_role_salary(_slim_df([]), today=date(2026, 4, 27))
    assert df.is_empty()
    assert dict(df.schema) == WEEKLY_ROLE_SALARY_SCHEMA


def test_role_salary_aggregates_with_min_sample():
    """min_sample=3 (default) — группа из 2 не попадает."""
    week = datetime(2026, 4, 27, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id=f"hh:{i}", title="Data Analyst", seniority="middle",
                  city="Москва", first_seen_at=week,
                  salary_rub_min=100_000 * (i + 1), salary_rub_max=200_000 * (i + 1))
        for i in range(3)
    ])
    df = build_weekly_role_salary(slim, today=date(2026, 4, 27))
    row = df.to_dicts()[0]
    assert row["role_canonical"] == "data_analyst"
    assert row["seniority"] == "middle"
    assert row["n_vacancies"] == 3
    # midpoints: 150k, 300k, 450k → median = 300k
    assert row["salary_rub_median"] == 300_000
    assert row["salary_rub_p25"] <= row["salary_rub_median"] <= row["salary_rub_p75"]


def test_role_salary_drops_groups_below_min_sample():
    week = datetime(2026, 4, 27, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id="hh:a", title="Data Analyst", first_seen_at=week),
        _slim_row(vacancy_id="hh:b", title="Backend Developer", first_seen_at=week),
    ])
    df = build_weekly_role_salary(slim, today=date(2026, 4, 27), min_sample=3)
    assert df.is_empty()


def test_role_salary_null_city_is_national_rollup_not_unknown_city_bucket():
    week = datetime(2026, 4, 27, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(
            vacancy_id=f"hh:known-{i}",
            title="Backend Developer",
            seniority="senior",
            city="Москва",
            first_seen_at=week,
            salary_rub_min=value,
            salary_rub_max=value,
        )
        for i, value in enumerate([100_000, 200_000, 300_000])
    ] + [
        _slim_row(
            vacancy_id=f"tg:unknown-{i}",
            title="Backend Developer",
            seniority="senior",
            city=None,
            first_seen_at=week,
            salary_rub_min=value,
            salary_rub_max=value,
        )
        for i, value in enumerate([1_000_000, 1_100_000, 1_200_000])
    ])

    df = build_weekly_role_salary(slim, today=date(2026, 4, 27), min_sample=3)
    national = df.filter(
        (pl.col("week_start") == date(2026, 4, 27))
        & (pl.col("role_canonical") == "backend_engineer")
        & (pl.col("seniority") == "senior")
        & pl.col("city").is_null()
    ).to_dicts()
    city_rows = df.filter(pl.col("city").is_not_null()).to_dicts()

    assert len(national) == 1
    assert national[0]["n_vacancies"] == 6
    assert {row["city"] for row in city_rows} == {"Москва"}


def test_role_salary_invariant_p25_le_median_le_p75():
    week = datetime(2026, 4, 27, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id=f"hh:{i}", title="Backend Developer",
                  first_seen_at=week, salary_rub_min=v, salary_rub_max=v)
        for i, v in enumerate([100_000, 150_000, 200_000, 250_000, 300_000])
    ])
    df = build_weekly_role_salary(slim, today=date(2026, 4, 27))
    for row in df.to_dicts():
        assert row["salary_rub_p25"] <= row["salary_rub_median"] <= row["salary_rub_p75"]


# ---- public entrypoint ----


def test_build_all_weekly_returns_4_dataframes(tmp_path: Path):
    db = _make_events_db(tmp_path / "events.duckdb", [])
    aggregates = build_all_weekly(db, _slim_df([]), today=date(2026, 4, 27))
    assert set(aggregates.keys()) == {
        "weekly_market_pulse",
        "weekly_employer_top",
        "weekly_skill_velocity",
        "weekly_role_salary",
    }


def test_utc_today_returns_utc_date():
    """`_utc_today` — pure UTC date wrapper (line 96)."""
    from src.transform.weekly_aggregates import _utc_today

    today = _utc_today()
    # Не assert конкретную дату (test может бежать в полночь UTC), но shape должна
    # быть `date`, и >= 2026-01-01 (sanity range).
    assert isinstance(today, date)
    assert today >= date(2026, 1, 1)


def test_employer_top_with_events_but_empty_slim_active(tmp_path: Path):
    """events.duckdb содержит appeared, но slim_active пуст (race window между
    ingest и slim build, или edge query сценарий) → disclosure_rate=0,
    employer_name=None (line 256 branch)."""
    db = _make_events_db(
        tmp_path / "events.duckdb",
        [
            {"vacancy_id": "v1", "employer_id": "100", "ts": datetime(2026, 4, 27, 10), "type": "appeared"},
        ],
    )
    df = build_weekly_employer_top(db, _slim_df([]), today=date(2026, 4, 27))
    assert df.height == 1
    row = df.to_dicts()[0]
    assert row["employer_id"] == "hh:100"
    assert row["employer_name"] is None
    assert row["disclosure_rate"] == 0.0


def test_skill_velocity_below_min_mentions_returns_empty():
    """Все skills отфильтрованы by min_mentions → exploded.is_empty() →
    early return с empty schema (line 340-341)."""
    week_dt = datetime(2026, 4, 27, 10, tzinfo=timezone.utc)
    slim = _slim_df([
        _slim_row(vacancy_id="hh:1", first_seen_at=week_dt, skills=["RareSkill"]),
    ])
    df = build_weekly_skill_velocity(slim, today=date(2026, 4, 27), min_mentions=5)
    assert df.is_empty()
    assert dict(df.schema) == WEEKLY_SKILL_VELOCITY_SCHEMA


def test_write_weekly_aggregates_creates_4_files(tmp_path: Path):
    aggregates = {
        "weekly_market_pulse": pl.DataFrame(schema=WEEKLY_MARKET_PULSE_SCHEMA),
        "weekly_employer_top": pl.DataFrame(schema=WEEKLY_EMPLOYER_TOP_SCHEMA),
        "weekly_skill_velocity": pl.DataFrame(schema=WEEKLY_SKILL_VELOCITY_SCHEMA),
        "weekly_role_salary": pl.DataFrame(schema=WEEKLY_ROLE_SALARY_SCHEMA),
    }
    written = write_weekly_aggregates(aggregates, tmp_path / "agg")
    assert len(written) == 4
    for p in written:
        assert p.exists()
        # roundtrip — empty schema preserved
        loaded = pl.read_parquet(p)
        assert loaded.is_empty()
