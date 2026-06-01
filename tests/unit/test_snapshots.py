"""Tests for src.publish.snapshots — JSON snapshot builders.

The JSON shapes must mirror the existing /api/facets and /api/trends/*
route responses byte-for-byte; the web routes are thin pass-throughs that
just fetch + cache + return. If a shape drifts, the dashboard would either
crash on parse or render `undefined` cells silently — both bad. These tests
lock down field names + types + ordering.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import polars as pl

from src.publish.snapshots import (
    CITY_FACET_LIMIT,
    EMPLOYER_FACET_LIMIT,
    REMOTE_VALUES,
    SENIORITY_VALUES,
    SKILL_FACET_LIMIT,
    build_employer_top_snapshot,
    build_facets_snapshot,
    build_market_pulse_snapshot,
    build_role_salary_snapshot,
    build_skill_velocity_snapshot,
    build_snapshots,
    iter_blob_paths,
)


def _utc(year: int, month: int, day: int, h: int = 0, m: int = 0) -> _dt.datetime:
    return _dt.datetime(year, month, day, h, m, tzinfo=_dt.timezone.utc)


def _slim_fixture() -> pl.DataFrame:
    """Compact slim_active.parquet-like frame covering all facet branches."""
    return pl.DataFrame(
        {
            "vacancy_id": ["v1", "v2", "v3", "v4", "v5"],
            "title": ["Python", "Java", "QA", "Frontend", "DevOps"],
            "employer_name": ["Acme", "Acme", "Globex", "Initech", None],
            "city": ["Москва", "Москва", "СПб", None, "Москва"],
            "remote_type": ["remote", "office", "hybrid", "remote", "unknown"],
            "seniority": ["senior", "middle", "junior", "lead", "unknown"],
            "source": ["hh", "hh", "telegram", "hh", "telegram"],
            "skills": [
                ["python", "django"],
                ["java", "spring"],
                ["python"],
                ["react", "typescript"],
                None,
            ],
            "salary_rub_min": [200_000, 150_000, 80_000, 250_000, None],
            "salary_rub_max": [300_000, 200_000, 120_000, 350_000, None],
            "salary_disclosed": [True, True, True, True, False],
            "last_seen_at": [
                _utc(2026, 5, 15, 13, 6),
                _utc(2026, 5, 15, 12, 0),
                _utc(2026, 5, 14, 10, 0),
                _utc(2026, 5, 15, 8, 30),
                _utc(2026, 5, 13, 0, 0),
            ],
        }
    )


def test_facets_snapshot_summary_counts():
    snap = build_facets_snapshot(_slim_fixture())
    assert snap["summary"]["total_vacancies"] == 5
    assert snap["summary"]["unique_cities"] == 2  # Москва, СПб (null dropped)
    assert snap["summary"]["unique_employers"] == 3  # Acme/Globex/Initech (null dropped)
    assert snap["summary"]["unique_skills"] == 6  # python/django/java/spring/react/typescript
    assert snap["summary"]["source_breakdown"] == {"hh": 3, "telegram": 2}
    # latest_seen_at is the max of last_seen_at, ISO formatted with microsecond +Z
    assert snap["summary"]["latest_seen_at"].startswith("2026-05-15T13:06:")
    assert snap["summary"]["latest_seen_at"].endswith("Z")


def test_facets_snapshot_facet_lists_sorted_by_count_desc():
    snap = build_facets_snapshot(_slim_fixture())
    cities = snap["facets"]["city"]
    assert cities[0] == {"value": "Москва", "count": 3}  # 3 rows
    assert cities[1] == {"value": "СПб", "count": 1}

    employers = snap["facets"]["employer_name"]
    assert employers[0] == {"value": "Acme", "count": 2}
    # Globex and Initech tie at 1 → alphabetical
    assert [e["value"] for e in employers[1:]] == ["Globex", "Initech"]


def test_facets_snapshot_enum_facets_zero_filled():
    snap = build_facets_snapshot(_slim_fixture())
    remote = snap["facets"]["remote_type"]
    assert [r["value"] for r in remote] == list(REMOTE_VALUES)
    by_value = {r["value"]: r["count"] for r in remote}
    assert by_value == {"office": 1, "hybrid": 1, "remote": 2, "unknown": 1}

    seniority = snap["facets"]["seniority"]
    assert [s["value"] for s in seniority] == list(SENIORITY_VALUES)
    by_value = {s["value"]: s["count"] for s in seniority}
    # intern/principal не встречаются, но всё равно в списке с count=0
    assert by_value["intern"] == 0
    assert by_value["principal"] == 0
    assert by_value["middle"] == 1


def test_facets_snapshot_skills_explode_and_count():
    snap = build_facets_snapshot(_slim_fixture())
    skills = snap["facets"]["skills"]
    # python встречается дважды (в двух разных вакансиях)
    by_value = {s["value"]: s["count"] for s in skills}
    assert by_value["python"] == 2
    assert by_value["django"] == 1
    assert by_value["typescript"] == 1


def test_facets_snapshot_salary_range_with_pct():
    snap = build_facets_snapshot(_slim_fixture())
    sr = snap["facets"]["salary_range"]
    assert sr["min"] == 80_000
    assert sr["max"] == 350_000
    # 4 of 5 rows disclose => 80.0%
    assert sr["with_salary_pct"] == 80.0


def test_facets_snapshot_empty_slim_returns_zero_shape():
    snap = build_facets_snapshot(pl.DataFrame())
    assert snap["summary"]["total_vacancies"] == 0
    assert snap["summary"]["source_breakdown"] == {"hh": 0, "telegram": 0}
    # enum facets still zero-filled even when slim is empty
    assert [r["value"] for r in snap["facets"]["remote_type"]] == list(REMOTE_VALUES)
    assert snap["facets"]["salary_range"]["with_salary_pct"] == 0.0


def test_facets_snapshot_limits_respected():
    # build a slim with > limit unique cities/employers/skills
    n = max(CITY_FACET_LIMIT, EMPLOYER_FACET_LIMIT, SKILL_FACET_LIMIT) + 5
    big = pl.DataFrame(
        {
            "vacancy_id": [f"v{i}" for i in range(n)],
            "city": [f"city{i}" for i in range(n)],
            "employer_name": [f"emp{i}" for i in range(n)],
            "remote_type": ["remote"] * n,
            "seniority": ["senior"] * n,
            "source": ["hh"] * n,
            "skills": [[f"skill{i}"] for i in range(n)],
            "salary_rub_min": [100_000] * n,
            "salary_rub_max": [200_000] * n,
            "salary_disclosed": [True] * n,
            "last_seen_at": [_utc(2026, 5, 15)] * n,
        }
    )
    snap = build_facets_snapshot(big)
    assert len(snap["facets"]["city"]) == CITY_FACET_LIMIT
    assert len(snap["facets"]["employer_name"]) == EMPLOYER_FACET_LIMIT
    assert len(snap["facets"]["skills"]) == SKILL_FACET_LIMIT


def test_market_pulse_snapshot_empty_df():
    """Empty market_pulse aggregate → empty rows but stamped with refreshed_at."""
    snap = build_market_pulse_snapshot(pl.DataFrame())
    assert snap["rows"] == []
    assert "refreshed_at" in snap


def test_facets_snapshot_last_seen_naive_datetime_assumes_utc():
    """`_latest_seen_at` falls through `tzinfo is None` branch — naive
    datetimes coming out of polars must be stamped UTC, not silently dropped.
    The snapshot's `refreshed_at` is always UTC so the latest_seen_at
    must match the same convention.
    """
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1"],
            "title": ["x"],
            "city": [None],
            "employer_id": [None],
            "employer_name": [None],
            "source": ["hh"],
            "seniority": [None],
            "remote_type": [None],
            "salary_currency": ["RUR"],
            "salary_disclosed": [True],
            "salary_rub_min": [100_000],
            "salary_rub_max": [200_000],
            "skills": [["python"]],
            # Naive datetime — Polars hands one off when the parquet column
            # has no timezone metadata. Must end up as UTC, not None.
            "last_seen_at": [_dt.datetime(2026, 5, 17, 12, 0, 0)],
        },
        schema_overrides={"last_seen_at": pl.Datetime("us")},
    )
    snap = build_facets_snapshot(df)
    assert snap["summary"]["latest_seen_at"] is not None
    assert snap["summary"]["latest_seen_at"].endswith("Z")


def test_market_pulse_snapshot_shape_matches_route():
    df = pl.DataFrame(
        {
            "date": [_utc(2026, 5, 10), _utc(2026, 5, 11), _utc(2026, 5, 9)],
            "total_active": [100, 110, 90],
            "new_vacancies": [10, 15, 8],
            "closed_vacancies": [5, 4, 6],
            "salary_disclosure_rate": [0.4, 0.42, 0.38],
            "median_active_age_days": [12.5, 11.2, 13.0],
        }
    )
    snap = build_market_pulse_snapshot(df)
    # rows sorted by date ASC (route uses ORDER BY date)
    assert [r["date"] for r in snap["rows"]] == ["2026-05-09", "2026-05-10", "2026-05-11"]
    row = snap["rows"][0]
    assert set(row.keys()) == {
        "date",
        "total_active",
        "new_vacancies",
        "closed_vacancies",
        "salary_disclosure_rate",
        "median_active_age_days",
    }
    assert isinstance(row["total_active"], int)
    assert isinstance(row["salary_disclosure_rate"], float)


def test_employer_top_snapshot_filters_latest_week_and_limits():
    df = pl.DataFrame(
        {
            "week_start": [_utc(2026, 5, 11), _utc(2026, 5, 11), _utc(2026, 5, 4)],
            "employer_id": ["e1", "e2", "e3"],
            "employer_name": ["A", "B", "C"],
            "new_vacancies": [10, 20, 100],  # C has more but in older week
            "closed_vacancies": [1, 2, 3],
            "disclosure_rate": [0.5, 0.6, 0.7],
        }
    )
    snap = build_employer_top_snapshot(df)
    # only latest week (2026-05-11), sorted by new_vacancies DESC
    assert [r["employer_name"] for r in snap["rows"]] == ["B", "A"]


def test_skill_velocity_snapshot_filters_latest_week_and_sorts_by_rank():
    df = pl.DataFrame(
        {
            "week_start": [_utc(2026, 5, 11), _utc(2026, 5, 11), _utc(2026, 5, 4)],
            "skill": ["python", "rust", "go"],
            "mentions_this_week": [500, 100, 999],
            "mentions_prev_week": [400, 50, 800],
            "delta_pct": [25.0, 100.0, 24.9],
            "rank_this_week": [2, 1, 3],
        }
    )
    snap = build_skill_velocity_snapshot(df)
    # only latest week, sorted by rank ASC (rust=1 first)
    assert [r["skill"] for r in snap["rows"]] == ["rust", "python"]


def test_role_salary_snapshot_only_national_rollup():
    df = pl.DataFrame(
        {
            "week_start": [_utc(2026, 5, 11), _utc(2026, 5, 11), _utc(2026, 5, 11)],
            "role_canonical": ["python_dev", "python_dev", "data_eng"],
            "seniority": ["senior", "senior", "senior"],
            "city": [None, "Москва", None],  # second row is city-level → excluded
            "n_vacancies": [100, 30, 50],
            "salary_rub_p25": [150_000, 180_000, 200_000],
            "salary_rub_median": [200_000, 220_000, 250_000],
            "salary_rub_p75": [280_000, 300_000, 320_000],
        }
    )
    snap = build_role_salary_snapshot(df)
    assert all(r["city"] is None for r in snap["rows"])
    assert len(snap["rows"]) == 2  # city-level row excluded
    # sorted by week_start DESC then median DESC
    assert [r["role_canonical"] for r in snap["rows"]] == ["data_eng", "python_dev"]


def test_build_snapshots_writes_all_files(tmp_path: Path):
    slim = _slim_fixture()
    weekly = {
        "weekly_market_pulse": pl.DataFrame(
            {
                "date": [_utc(2026, 5, 11)],
                "total_active": [100],
                "new_vacancies": [10],
                "closed_vacancies": [5],
                "salary_disclosure_rate": [0.4],
                "median_active_age_days": [12.5],
            }
        ),
        "weekly_employer_top": pl.DataFrame(),
        "weekly_skill_velocity": pl.DataFrame(),
        "weekly_role_salary": pl.DataFrame(),
    }

    written = build_snapshots(slim, weekly, tmp_path)
    expected = {
        "facets",
        "trends/market_pulse",
        "trends/employer_top",
        "trends/skill_velocity",
        "trends/role_salary",
    }
    assert set(written.keys()) == expected
    for path in written.values():
        assert path.is_file()
        # JSON must be valid + non-empty
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(payload, dict)


def test_iter_blob_paths_default_prefix(tmp_path: Path):
    # Build a fake snapshots tree
    (tmp_path / "facets.json").write_text("{}", encoding="utf-8")
    (tmp_path / "trends").mkdir()
    (tmp_path / "trends" / "market_pulse.json").write_text("{}", encoding="utf-8")
    (tmp_path / "trends" / "skill_velocity.json").write_text("{}", encoding="utf-8")

    pairs = list(iter_blob_paths(tmp_path))
    pathnames = sorted(blob for _, blob in pairs)
    assert pathnames == [
        "slim/snapshots/facets.json",
        "slim/snapshots/trends/market_pulse.json",
        "slim/snapshots/trends/skill_velocity.json",
    ]


def test_iter_blob_paths_custom_prefix(tmp_path: Path):
    (tmp_path / "facets.json").write_text("{}", encoding="utf-8")
    pairs = list(iter_blob_paths(tmp_path, blob_prefix="custom/path"))
    _, blob = pairs[0]
    assert blob == "custom/path/facets.json"


def test_facets_snapshot_payload_is_compact_json(tmp_path: Path):
    """Egress-saving requires no whitespace bloat (~30% bigger if pretty-printed)."""
    from src.publish.snapshots import write_snapshot

    out = tmp_path / "facets.json"
    write_snapshot(build_facets_snapshot(_slim_fixture()), out)
    text = out.read_text(encoding="utf-8")
    assert ": " not in text  # separator is `,` and `:` without spaces
    assert ", " not in text


def test_latest_seen_at_returns_none_when_max_is_null():
    """Column exists, но все значения NULL → max() = None → return None
    (line 133)."""
    from src.publish.snapshots import _latest_seen_at

    df = pl.DataFrame({"last_seen_at": [None, None]}, schema={"last_seen_at": pl.Datetime("us", "UTC")})
    assert _latest_seen_at(df) is None


def test_latest_seen_at_stringifies_non_datetime_max():
    """Max value не `datetime` (defensive fallback) → str(value) (line 138)."""
    from src.publish.snapshots import _latest_seen_at

    # Если в фрейме last_seen_at внезапно строкой (drift в pipeline) — не падать,
    # а сериализовать "as-is".
    df = pl.DataFrame({"last_seen_at": ["2026-05-18T12:00:00Z"]})
    assert _latest_seen_at(df) == "2026-05-18T12:00:00Z"


def test_fmt_date_handles_none_and_non_datetime():
    """_fmt_date: None → None (210), date → iso (213-214), unknown → str (215)."""
    from src.publish.snapshots import _fmt_date

    assert _fmt_date(None) is None
    assert _fmt_date(_dt.date(2026, 5, 18)) == "2026-05-18"
    assert _fmt_date("2026-05-18") == "2026-05-18"  # str fallback (line 215)


def test_latest_week_filter_returns_unchanged_when_column_missing():
    """`week_start` column отсутствует → DF возвращается as-is (line 246)."""
    from src.publish.snapshots import _latest_week_filter

    df = pl.DataFrame({"some_other_col": [1, 2, 3]})
    result = _latest_week_filter(df)
    assert result.height == 3
    assert "some_other_col" in result.columns
