"""Freshness gate for `vradar publish weekly`.

Empty aggregate parquets break the /trends frontend silently: DuckDB reads a
zero-row Parquet, recharts renders an empty chart, no error is raised.
Audit 2026-04-27 caught this on weekly_skill_velocity.parquet.

The gate must:
  - default mode: skip upload of any empty aggregate (keep previous-good
    Blob copy), log a warning, exit 0;
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
from src.publish.blob_push import BlobConfig, BlobUploadResult


def _args(*, strict: bool = False) -> Namespace:
    return Namespace(target="weekly", dry=False, strict=strict, dedup=False)


@pytest.fixture
def patched_publish(monkeypatch, tmp_path, blob_env):
    monkeypatch.chdir(tmp_path)

    # Fast-path: _publish_weekly reads derived/slim_active.parquet written by
    # the preceding `publish slim` step instead of rebuilding from the lake.
    slim_path = tmp_path / "derived" / "slim_active.parquet"
    slim_path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"vacancy_id": ["hh:1"]}).write_parquet(slim_path)

    fake_aggregates = {
        # Two empty + two non-empty — proves gate skips empties without
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

    upload_calls: list[tuple[Path, str]] = []

    def fake_upload(local_path: Path, pathname: str, cfg: BlobConfig, **_kwargs):
        upload_calls.append((local_path, pathname))
        return BlobUploadResult(
            pathname=pathname,
            url=f"https://blob.example/{pathname}",
            public_url=f"https://blob.example/{pathname}",
            content_type="application/octet-stream",
            response={},
        )

    monkeypatch.setattr("src.publish.blob_push.upload_file", fake_upload)
    return upload_calls


def test_publish_weekly_skips_empty_aggregates_by_default(patched_publish, capsys):
    upload_calls = patched_publish

    exit_code = cli._publish_weekly(_args())

    assert exit_code == 0
    uploaded_names = sorted(name for _, name in upload_calls)
    assert uploaded_names == [
        "agg/weekly_employer_top.parquet",
        "agg/weekly_role_salary.parquet",
    ]
    captured = capsys.readouterr()
    assert "weekly_market_pulse: 0 rows — SKIPPED" in captured.err
    assert "weekly_skill_velocity: 0 rows — SKIPPED" in captured.err
    assert "[warn] 2/4 weekly aggregates empty" in captured.err


def test_publish_weekly_strict_exits_nonzero_on_empty(patched_publish, capsys):
    upload_calls = patched_publish

    exit_code = cli._publish_weekly(_args(strict=True))

    assert exit_code == 4
    # Non-empty aggregates still get uploaded (preserves run idempotency
    # — strict failure ≠ rolling-back successful uploads).
    uploaded_names = sorted(name for _, name in upload_calls)
    assert uploaded_names == [
        "agg/weekly_employer_top.parquet",
        "agg/weekly_role_salary.parquet",
    ]
    captured = capsys.readouterr()
    assert "weekly --strict" in captured.err


def test_publish_weekly_reuses_slim_parquet(patched_publish, capsys):
    cli._publish_weekly(_args())

    captured = capsys.readouterr()
    assert "reusing" in captured.out
    assert "slim_active.parquet" in captured.out


def test_publish_weekly_skips_blob_upload_when_base_is_hf(
    patched_publish, monkeypatch, capsys
):
    upload_calls = patched_publish
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "")
    monkeypatch.setenv(
        "BLOB_PUBLIC_BASE_URL",
        "https://huggingface.co/datasets/your-org/vacancyradar-data/resolve/main",
    )

    exit_code = cli._publish_weekly(_args())

    assert exit_code == 0
    assert upload_calls == []
    assert "blob upload disabled" in capsys.readouterr().out


def test_publish_weekly_rebuilds_when_slim_missing(monkeypatch, tmp_path, capsys, blob_env):
    """If derived/slim_active.parquet is absent, fall back to lake rebuild.

    Covers ad-hoc local use where someone runs `publish weekly` without first
    running `publish slim`. Cron sequence is enforced by daily_refresh.ps1.
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
    monkeypatch.setattr(
        "src.publish.blob_push.upload_file",
        lambda *args, **kwargs: BlobUploadResult(
            pathname="x",
            url="x",
            public_url="x",
            content_type="application/octet-stream",
            response={},
        ),
    )

    exit_code = cli._publish_weekly(_args())

    assert exit_code == 0
    assert rebuild_calls == [Path("master/vacancies_raw.parquet")]
    captured = capsys.readouterr()
    assert "slim_active.parquet missing" in captured.err
