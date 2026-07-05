"""Tests for the static storefront artifact builder (`static-proto/build_artifact.py`).

The storefront is the live read path, so the builder needs the same guard
discipline as the rest of the pipeline: clean titles, compact rows, and an
absolute floor that refuses to publish a truncated/empty artifact.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import os

import polars as pl
import pytest

_BUILD_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "static-proto",
    "build_artifact.py",
)
_spec = importlib.util.spec_from_file_location("vr_build_artifact", _BUILD_PY)
assert _spec and _spec.loader
ba = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ba)


def _df(rows: list[dict]) -> pl.DataFrame:
    """Build a slim-shaped frame; missing keys default to typed nulls/empties."""
    base = {
        "vacancy_id": "id0",
        "title": "Title",
        "employer_name": "Acme",
        "city": "Москва",
        "region": "Москва",
        "salary_rub_min": None,
        "salary_rub_max": None,
        "remote_type": None,
        "seniority": None,
        "source": "hh",
        "source_url": "https://hh.ru/v/1",
        "skills": [],
        "last_seen_at": dt.datetime(2026, 6, 25),
        "posted_at": dt.datetime(2026, 6, 20),
        "description_teaser": "",
    }
    return pl.DataFrame([{**base, **r} for r in rows])


# ---- _clean -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**Python** developer", "Python developer"),
        ("[Senior](https://x.io) Data Engineer", "Senior Data Engineer"),
        ("Analyst https://t.me/jobs now", "Analyst now"),
        ("  #Hiring  ", "Hiring"),
        ("Data ​Engineer", "Data Engineer"),  # zero-width space removed
        ("", ""),
        (None, ""),
    ],
)
def test_clean_strips_markdown_links_urls(raw, expected):
    assert ba._clean(raw) == expected


def test_clean_strips_emoji():
    assert ba._clean("🚀 Python 🔥") == "Python"


# ---- build_rows -------------------------------------------------------------

def test_build_rows_uses_compact_keys_and_drops_empties():
    rows = ba.build_rows(_df([{"vacancy_id": "v1", "title": "Go Dev"}]))
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "v1"
    assert r["t"] == "Go Dev"
    assert r["src"] == "hh"
    assert r["ls"] == "2026-06-25"
    # null salary / empty skills must not appear as keys
    assert "smin" not in r and "smax" not in r and "sk" not in r


def test_build_rows_rescues_title_from_teaser_when_title_is_noise():
    rows = ba.build_rows(
        _df([{"vacancy_id": "v2", "title": "🔥🔥", "description_teaser": "Middle Data Analyst"}])
    )
    assert len(rows) == 1
    assert rows[0]["t"] == "Middle Data Analyst"


def test_build_rows_drops_row_with_no_usable_title():
    rows = ba.build_rows(
        _df([{"vacancy_id": "v3", "title": "✨", "description_teaser": ""}])
    )
    assert rows == []


def test_build_rows_dedups_teaser_that_repeats_title():
    rows = ba.build_rows(
        _df([{"vacancy_id": "v4", "title": "Python Dev", "description_teaser": "Python Dev — Acme, удалёнка"}])
    )
    assert rows[0]["t"] == "Python Dev"
    assert rows[0].get("ds", "").startswith("Acme")


# ---- floor guard ------------------------------------------------------------

def test_main_refuses_truncated_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(ba, "load", lambda _p: _df([{"vacancy_id": "only1"}]))
    monkeypatch.setattr(ba.sys, "argv", ["build_artifact.py", "ignored.parquet", str(tmp_path)])
    monkeypatch.setattr(ba, "MIN_ROWS", 100)
    with pytest.raises(ba.ArtifactTooSmallError):
        ba.main()
    # nothing written
    assert not (tmp_path / "data.json.gz").exists()


def test_main_writes_artifact_above_floor(tmp_path, monkeypatch):
    big = _df([{"vacancy_id": f"v{i}", "title": f"Job {i}"} for i in range(120)])
    # load() is called for the parquet AND for the 3 trend aggregates; return a
    # vacancy frame for the first call, empty trend frames after.
    calls = {"n": 0}
    def fake_load(_p):
        calls["n"] += 1
        return big if calls["n"] == 1 else pl.DataFrame()
    monkeypatch.setattr(ba, "load", fake_load)
    monkeypatch.setattr(ba, "build_trends", lambda *_: {"salary": [], "salary_by_level": {"weeks": [], "series": {}}, "skills": {}})
    monkeypatch.setattr(ba.sys, "argv", ["build_artifact.py", "ignored.parquet", str(tmp_path)])
    monkeypatch.setattr(ba, "MIN_ROWS", 100)
    assert ba.main() == 0
    assert (tmp_path / "data.json.gz").exists()
    assert (tmp_path / "trends.json.gz").exists()


def test_main_survives_trends_failure(tmp_path, monkeypatch):
    """A weekly-aggregate hiccup must not fail the main artifact."""
    big = _df([{"vacancy_id": f"v{i}", "title": f"Job {i}"} for i in range(120)])
    monkeypatch.setattr(ba, "load", lambda _p: big)
    def boom(*_):
        raise RuntimeError("HF down")
    monkeypatch.setattr(ba, "build_trends", boom)
    monkeypatch.setattr(ba.sys, "argv", ["build_artifact.py", "ignored.parquet", str(tmp_path)])
    monkeypatch.setattr(ba, "MIN_ROWS", 100)
    assert ba.main() == 0
    assert (tmp_path / "data.json.gz").exists()
    assert not (tmp_path / "trends.json.gz").exists()


# ---- build_trends -----------------------------------------------------------

def _salary_df(rows):
    base = {"week_start": dt.date(2026, 5, 11), "role_canonical": "analyst", "seniority": "middle",
            "city": "Москва", "n_vacancies": 10, "salary_rub_p25": 100000,
            "salary_rub_median": 150000, "salary_rub_p75": 200000}
    return pl.DataFrame([{**base, **r} for r in rows])


def _skills_df(rows):
    base = {"week_start": dt.date(2026, 6, 22), "skill": "Python", "mentions_this_week": 100,
            "mentions_prev_week": 80, "delta_pct": 25.0, "rank_this_week": 1}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_build_trends_salary_is_vacancy_weighted_per_week():
    # one week, two rows: medians 100k (n=1) and 200k (n=3) -> weighted 175k
    out = ba.build_trends(
        _salary_df([
            {"n_vacancies": 1, "salary_rub_median": 100000, "salary_rub_p25": 90000, "salary_rub_p75": 110000},
            {"n_vacancies": 3, "salary_rub_median": 200000, "salary_rub_p25": 180000, "salary_rub_p75": 220000},
        ]),
        _skills_df([]),
    )
    assert len(out["salary"]) == 1
    assert out["salary"][0]["med"] == 175000
    assert out["salary"][0]["n"] == 4


def test_build_trends_salary_by_level_drops_thin_grades():
    # middle present 3 weeks -> a line; senior only 1 week -> dropped
    rows = [{"week_start": w, "seniority": "middle", "n_vacancies": 20, "salary_rub_median": 150000 + i * 1000}
            for i, w in enumerate([dt.date(2026, 5, 11), dt.date(2026, 5, 18), dt.date(2026, 5, 25)])]
    rows.append({"week_start": dt.date(2026, 5, 11), "seniority": "senior", "n_vacancies": 20, "salary_rub_median": 250000})
    out = ba.build_trends(_salary_df(rows), _skills_df([]))
    sbl = out["salary_by_level"]
    assert sbl["weeks"] == ["2026-05-11", "2026-05-18", "2026-05-25"]
    assert "middle" in sbl["series"] and sbl["series"]["middle"][0] == 150000
    assert "senior" not in sbl["series"]


def test_build_trends_skills_latest_week_movers_filtered():
    out = ba.build_trends(
        _salary_df([]),
        _skills_df([
            {"skill": "Go", "delta_pct": 300.0, "mentions_this_week": 50},     # up, significant
            {"skill": "Perl", "delta_pct": -90.0, "mentions_this_week": 40},   # down, significant
            {"skill": "Noise", "delta_pct": 999.0, "mentions_this_week": 5},   # below threshold -> excluded
            {"skill": "Old", "delta_pct": 50.0, "mentions_this_week": 99, "week_start": dt.date(2026, 6, 15)},  # old week
        ]),
    )
    assert out["skills"]["week"] == "2026-06-22"
    up_skills = [m["s"] for m in out["skills"]["up"]]
    down_skills = [m["s"] for m in out["skills"]["down"]]
    assert up_skills[0] == "Go"
    assert "Perl" in down_skills
    assert "Noise" not in up_skills + down_skills      # mentions < 25
    assert "Old" not in up_skills + down_skills        # not the latest week


# ---- build_pulse (daily appeared intake) ------------------------------------

def _events_df(rows):
    base = {"ts": dt.datetime(2026, 6, 25, 8, 0), "type": "appeared", "source": "hh"}
    return pl.DataFrame([{**base, **r} for r in rows])


def test_build_pulse_counts_only_appeared_per_source():
    # closed is structurally broken upstream and must never be surfaced.
    ev = _events_df([
        {"ts": dt.datetime(2026, 6, 25, 8), "type": "appeared", "source": "hh"},
        {"ts": dt.datetime(2026, 6, 25, 9), "type": "appeared", "source": "tg"},
        {"ts": dt.datetime(2026, 6, 25, 10), "type": "closed", "source": "hh"},   # ignored
        {"ts": dt.datetime(2026, 6, 25, 11), "type": "desc_changed", "source": "hh"},  # ignored
    ])
    out = ba.build_pulse(ev, dt.date(2026, 6, 27), days=18)
    assert out["days"] == ["2026-06-25"]
    assert out["hh"] == [1] and out["tg"] == [1]
    assert "closed" not in out                          # never emitted


def test_build_pulse_excludes_current_partial_day():
    ev = _events_df([
        {"ts": dt.datetime(2026, 6, 25, 8), "type": "appeared", "source": "hh"},
        {"ts": dt.datetime(2026, 6, 26, 8), "type": "appeared", "source": "hh"},   # == today, partial
    ])
    out = ba.build_pulse(ev, dt.date(2026, 6, 26), days=18)
    assert out["days"] == ["2026-06-25"]               # today dropped
    assert out["asof"] == "2026-06-25"


def test_build_pulse_window_drops_older_than_days():
    ev = _events_df([
        {"ts": dt.datetime(2026, 6, 1, 8), "type": "appeared", "source": "hh"},    # before start -> dropped
        {"ts": dt.datetime(2026, 6, 25, 8), "type": "appeared", "source": "hh"},
    ])
    out = ba.build_pulse(ev, dt.date(2026, 6, 27), days=18)   # start = 2026-06-09
    assert out["days"] == ["2026-06-25"]


def test_build_pulse_missing_source_is_zeros():
    ev = _events_df([{"ts": dt.datetime(2026, 6, 25, 8), "type": "appeared", "source": "hh"}])
    out = ba.build_pulse(ev, dt.date(2026, 6, 27), days=18)
    assert out["hh"] == [1] and out["tg"] == [0]       # tg column absent -> zeros, not a crash


def test_build_pulse_ma7_is_trailing_7day_mean():
    days = [dt.date(2026, 6, 24) + dt.timedelta(days=i) for i in range(8)]   # 8 full days
    rows = [{"ts": dt.datetime(d.year, d.month, d.day, 8), "type": "appeared", "source": "hh"}
            for i, d in enumerate(days) for _ in range(i + 1)]               # 1,2,...,8 per day
    out = ba.build_pulse(_events_df(rows), dt.date(2026, 7, 2), days=18)
    assert out["hh"] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert out["ma7"][0] == 1                           # mean([1])
    assert out["ma7"][6] == 4                           # mean(1..7)=28/7
    assert out["ma7"][-1] == 5                          # mean(2..8)=35/7 trailing window of 7


def test_build_pulse_empty_events():
    out = ba.build_pulse(pl.DataFrame(), dt.date(2026, 6, 27), days=18)
    assert out == {"days": [], "hh": [], "tg": [], "ma7": [], "window_days": 18, "asof": None}
