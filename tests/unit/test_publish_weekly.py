"""Freshness gate for `vradar publish weekly`.

Empty aggregate parquets break the storefront trends silently: the builder
reads a zero-row Parquet and renders an empty chart, no error is raised.
Audit 2026-04-27 caught this on weekly_skill_velocity.parquet.

The gate must:
  - default mode: log a warning for any empty aggregate, exit 0;
  - strict mode (`--strict`): exit non-zero so the scheduled task / CI run
    surfaces the failure.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import polars as pl
import pytest

import src.cli as cli
import src.transform.weekly_aggregates as weekly_aggregates


def _args(*, strict: bool = False) -> Namespace:
    return Namespace(target="weekly", dry=False, strict=strict, dedup=False)


@pytest.fixture
def patched_publish(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    # Fast-path: _publish_weekly reads derived/slim_active.parquet written by
    # the preceding `publish slim` step instead of rebuilding from the lake.
    slim_path = tmp_path / "derived" / "slim_active.parquet"
    slim_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"vacancy_id": ["hh:1"]}).write_parquet(slim_path)

    fake_aggregates = {
        # Two empty + two non-empty — proves gate warns on empties without
        # tanking the entire run.
        "weekly_market_pulse": pl.DataFrame(
            schema={"date": pl.Date, "total_active": pl.Int64},
        ),
        "weekly_employer_top": pl.DataFrame(
            {"week_start": ["2026-04-21"], "employer_id": ["x"], "new_vacancies": [3]},
        ),
        "weekly_skill_velocity": pl.DataFrame(
            schema=weekly_aggregates.WEEKLY_SKILL_VELOCITY_SCHEMA,
        ),
        "weekly_role_salary": pl.DataFrame(
            {
                "role_canonical": ["analyst"],
                "n_vacancies": [10],
                "salary_rub_median": [150000],
            }
        ),
    }
    monkeypatch.setattr(
        "src.transform.weekly_aggregates.build_all_weekly",
        lambda _events_db, _slim: fake_aggregates,
    )

    written_paths: list[Path] = []

    def fake_write(aggregates: dict[str, pl.DataFrame], out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for name, df in aggregates.items():
            path = out_dir / f"{name}.parquet"
            df.write_parquet(path)
            result.append(path)
            written_paths.append(path)
        return result

    monkeypatch.setattr(
        "src.transform.weekly_aggregates.write_weekly_aggregates",
        fake_write,
    )
    return written_paths


def test_publish_weekly_warns_on_empty_aggregates_by_default(patched_publish, capsys):
    exit_code = cli._publish_weekly(_args())

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "weekly_market_pulse: 0 rows" in captured.err
    assert "weekly_skill_velocity: 0 rows" in captured.err
    assert "[warn] 2/4 weekly aggregates empty" in captured.err
    # Non-empty aggregates are reported on stdout.
    assert "weekly_employer_top: 1 rows" in captured.out
    assert "weekly_role_salary: 1 rows" in captured.out


def test_publish_weekly_strict_exits_nonzero_on_empty(patched_publish, capsys):
    exit_code = cli._publish_weekly(_args(strict=True))

    assert exit_code == 4
    # All aggregates are still written locally (strict failure ≠ rolling back
    # the files the HF mirror step would pick up on the next good run).
    assert len(patched_publish) == 4
    captured = capsys.readouterr()
    assert "weekly --strict" in captured.err


def test_publish_weekly_reuses_slim_parquet(patched_publish, capsys):
    cli._publish_weekly(_args())

    captured = capsys.readouterr()
    assert "reusing" in captured.out
    assert "slim_active.parquet" in captured.out


def test_publish_weekly_dry_skips_build(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def boom(*_a, **_kw):
        raise AssertionError("dry run must not build aggregates")

    monkeypatch.setattr("src.transform.weekly_aggregates.build_all_weekly", boom)

    exit_code = cli._publish_weekly(Namespace(target="weekly", dry=True, strict=False))

    assert exit_code == 0
    assert "[dry] publish weekly" in capsys.readouterr().out


def test_publish_weekly_rebuilds_when_slim_missing(monkeypatch, tmp_path, capsys):
    """If derived/slim_active.parquet is absent, fall back to lake rebuild.

    Covers ad-hoc local use where someone runs `publish weekly` without first
    running `publish slim`. The collection runner enforces the cron sequence.
    """
    monkeypatch.chdir(tmp_path)

    rebuild_calls: list[Path] = []

    def fake_build(lake: Path) -> pl.DataFrame:
        rebuild_calls.append(lake)
        return pl.DataFrame({"vacancy_id": ["hh:1"]})

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build)
    monkeypatch.setattr(
        "src.transform.weekly_aggregates.build_all_weekly",
        lambda _events_db, _slim: {
            "weekly_market_pulse": pl.DataFrame(
                {"date": ["2026-05-17"], "new_vacancies": [1]}
            ),
        },
    )

    def fake_write(aggregates: dict[str, pl.DataFrame], out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for name, df in aggregates.items():
            path = out_dir / f"{name}.parquet"
            df.write_parquet(path)
            result.append(path)
        return result

    monkeypatch.setattr(
        "src.transform.weekly_aggregates.write_weekly_aggregates",
        fake_write,
    )

    exit_code = cli._publish_weekly(_args())

    assert exit_code == 0
    assert rebuild_calls == [Path("master/vacancies_raw.parquet")]
    captured = capsys.readouterr()
    assert "slim_active.parquet missing" in captured.err
