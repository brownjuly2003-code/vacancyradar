"""Pytest setup + shared fixtures.

`tests/` is a peer of `src/` rather than a package, so we splice the project
root onto `sys.path` before any imports. Shared fixtures live here so each
test file can pull in the minimal set it needs without duplicating boilerplate
(polars frame builders, fake CBR rate maps).
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def slim_active_frame() -> pl.DataFrame:
    """Minimal valid slim_active.parquet shape (1 row).

    Mirrors `SLIM_ACTIVE_SCHEMA` in `src/transform/slim_export.py`. Use as a
    template — extend via `.vstack(extra_df)` for multi-row scenarios.
    """
    now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
    return pl.DataFrame(
        {
            "vacancy_id": ["hh:1"],
            "title": ["Senior Python Developer"],
            "employer_id": ["hh-emp:1"],
            "employer_name": ["Acme"],
            "salary_rub_min": [200_000],
            "salary_rub_max": [300_000],
            "salary_currency": ["RUR"],
            "salary_disclosed": [True],
            "city": ["Москва"],
            "region": ["central"],
            "remote_type": ["remote"],
            "seniority": ["senior"],
            "description_teaser": ["Python, FastAPI, PostgreSQL"],
            "skills": [["python", "fastapi", "postgresql"]],
            "source": ["hh"],
            "market_scope": ["it"],
            "professional_role_id": [96],
            "source_url": ["https://hh.ru/vacancy/1"],
            "first_seen_at": [now],
            "last_seen_at": [now],
            "posted_at": [now],
        },
        schema={
            "vacancy_id": pl.String,
            "title": pl.String,
            "employer_id": pl.String,
            "employer_name": pl.String,
            "salary_rub_min": pl.Int64,
            "salary_rub_max": pl.Int64,
            "salary_currency": pl.String,
            "salary_disclosed": pl.Boolean,
            "city": pl.String,
            "region": pl.String,
            "remote_type": pl.String,
            "seniority": pl.String,
            "description_teaser": pl.String,
            "skills": pl.List(pl.String),
            "source": pl.String,
            "market_scope": pl.String,
            "professional_role_id": pl.Int64,
            "source_url": pl.String,
            "first_seen_at": pl.Datetime("us", "UTC"),
            "last_seen_at": pl.Datetime("us", "UTC"),
            "posted_at": pl.Datetime("us", "UTC"),
        },
    )


@pytest.fixture()
def cbr_rates() -> dict[str, float]:
    """Stable USD/EUR rates anchored to 2026-04-27 CBR rates for repeatable tests."""
    return {"USD": 81.5, "EUR": 92.4, "RUR": 1.0, "RUB": 1.0}


@pytest.fixture()
def settings_with_salary_bounds(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, int]]:
    """Override `Settings.salary` bounds via load_settings cache reset.

    Yields the (floor, ceiling) tuple in effect. Use when testing custom
    outlier thresholds; the original cache is restored on teardown.
    """
    from src import config

    config.load_settings.cache_clear()
    monkeypatch.setattr(
        config,
        "_load",
        lambda _path: config.Settings(
            salary=config.SalarySettings(outlier_floor=5_000, outlier_ceiling=10_000_000)
        ),
    )
    yield (5_000, 10_000_000)
    config.load_settings.cache_clear()


def assert_polars_eq(left: pl.DataFrame, right: pl.DataFrame, *, check_row_order: bool = True) -> None:
    """Polars-shape-aware equality assert helper.

    Polars' built-in `assert_frame_equal` is strict on dtype/column order. This
    wrapper:
      * normalizes column order (sorts both lexicographically),
      * normalizes row order when `check_row_order=False` (sorts on all columns).
    Useful for tests that don't care about projection or shuffle order.
    """
    from polars.testing import assert_frame_equal

    cols = sorted(set(left.columns) | set(right.columns))
    left_n = left.select(cols)
    right_n = right.select(cols)
    if not check_row_order:
        left_n = left_n.sort(cols)
        right_n = right_n.sort(cols)
    assert_frame_equal(left_n, right_n)


@pytest.fixture()
def polars_eq() -> Any:
    """Expose assert_polars_eq as a fixture-style callable."""
    return assert_polars_eq


@pytest.fixture()
def clean_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir to a tmp directory so tests that touch relative paths don't pollute repo.

    Use when CLI code calls `Path("master/...")` or `derived/...` — the fixture
    isolates writes inside `tmp_path`.
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path
