"""Tests for closed-month raw-lake compaction (src/transform/lake_compact.py)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from src.ingest.raw_lake import _LAKE_SCHEMA, latest_snapshot_meta
from src.transform import lake_compact
from src.transform.lake_compact import (
    compact_lake,
    compact_partition,
    plan_compaction,
)

NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)


def _write_batch(partition: Path, name: str, ids: list[str], fetched_at: str) -> Path:
    """Минимальный батч с канонической схемой лейка."""
    partition.mkdir(parents=True, exist_ok=True)
    rows = {
        col: [None] * len(ids) for col in _LAKE_SCHEMA if col not in ("vacancy_id", "fetched_at")
    }
    rows["vacancy_id"] = ids
    rows["fetched_at"] = [fetched_at] * len(ids)
    df = pl.DataFrame(rows, schema=_LAKE_SCHEMA)
    path = partition / name
    df.write_parquet(path)
    return path


def _build_lake(root: Path) -> dict[str, Path]:
    """Лейк: закрытый месяц 04 (3 файла hh), закрытый 05 (1 файл hh),
    текущий 06 (2 файла hh) + закрытый 04 telegram (2 файла)."""
    p04 = root / "year=2026" / "month=04" / "source=hh"
    p05 = root / "year=2026" / "month=05" / "source=hh"
    p06 = root / "year=2026" / "month=06" / "source=hh"
    p04tg = root / "year=2026" / "month=04" / "source=telegram"
    _write_batch(p04, "fetched_001_a.parquet", ["hh:1", "hh:2"], "2026-04-01T00:00:00+00:00")
    _write_batch(p04, "fetched_002_b.parquet", ["hh:2", "hh:3"], "2026-04-02T00:00:00+00:00")
    _write_batch(p04, "fetched_003_c.parquet", ["hh:4"], "2026-04-03T00:00:00+00:00")
    _write_batch(p05, "fetched_004_d.parquet", ["hh:5"], "2026-05-01T00:00:00+00:00")
    _write_batch(p06, "fetched_005_e.parquet", ["hh:6"], "2026-06-01T00:00:00+00:00")
    _write_batch(p06, "fetched_006_f.parquet", ["hh:7"], "2026-06-02T00:00:00+00:00")
    _write_batch(p04tg, "fetched_007_g.parquet", ["tg:a:1"], "2026-04-05T00:00:00+00:00")
    _write_batch(p04tg, "fetched_008_h.parquet", ["tg:a:2"], "2026-04-06T00:00:00+00:00")
    return {"p04": p04, "p05": p05, "p06": p06, "p04tg": p04tg}


def test_plan_skips_current_month_and_single_file_partitions(tmp_path):
    parts = _build_lake(tmp_path)
    plans = plan_compaction(tmp_path, now=NOW)
    planned_dirs = {p.partition_dir for p in plans}
    assert parts["p04"] in planned_dirs  # closed, 3 files
    assert parts["p04tg"] in planned_dirs  # closed, 2 files
    assert parts["p05"] not in planned_dirs  # closed but already single-file
    assert parts["p06"] not in planned_dirs  # current month


def test_plan_files_sorted_by_name(tmp_path):
    parts = _build_lake(tmp_path)
    plans = {p.partition_dir: p for p in plan_compaction(tmp_path, now=NOW)}
    names = [f.name for f in plans[parts["p04"]].files]
    assert names == sorted(names)


def test_compact_partition_merges_and_moves_originals_to_trash(tmp_path):
    lake = tmp_path / "lake"
    trash = tmp_path / "trash"
    parts = _build_lake(lake)
    plans = {p.partition_dir: p for p in plan_compaction(lake, now=NOW)}

    rows, n_files = compact_partition(plans[parts["p04"]], lake, trash)
    assert (rows, n_files) == (5, 3)

    remaining = sorted(parts["p04"].glob("*.parquet"))
    assert len(remaining) == 1
    assert remaining[0].name.startswith("compacted_")
    # Originals preserved under the same Hive-relative path.
    trashed = sorted((trash / "year=2026" / "month=04" / "source=hh").glob("*.parquet"))
    assert [p.name for p in trashed] == [
        "fetched_001_a.parquet",
        "fetched_002_b.parquet",
        "fetched_003_c.parquet",
    ]
    # No stray tmp files.
    assert list(parts["p04"].glob("*.tmp")) == []


def test_compacted_lake_reads_identical_meta(tmp_path):
    lake = tmp_path / "lake"
    trash = tmp_path / "trash"
    _build_lake(lake)
    before = latest_snapshot_meta(lake, source="hh")

    results = compact_lake(lake, trash, now=NOW)
    assert len(results) == 2  # p04 hh + p04 telegram

    after = latest_snapshot_meta(lake, source="hh")
    assert before.equals(after)


def test_compact_lake_dry_changes_nothing(tmp_path):
    lake = tmp_path / "lake"
    trash = tmp_path / "trash"
    parts = _build_lake(lake)
    files_before = sorted(str(p) for p in lake.rglob("*.parquet"))

    results = compact_lake(lake, trash, dry=True, now=NOW)

    assert sorted(str(p) for p in lake.rglob("*.parquet")) == files_before
    assert not trash.exists()
    by_dir = {plan.partition_dir: (rows, n) for plan, rows, n in results}
    assert by_dir[parts["p04"]] == (5, 3)
    assert by_dir[parts["p04tg"]] == (2, 2)


def test_compact_partition_rowcount_mismatch_keeps_originals(tmp_path, monkeypatch):
    lake = tmp_path / "lake"
    trash = tmp_path / "trash"
    parts = _build_lake(lake)
    plans = {p.partition_dir: p for p in plan_compaction(lake, now=NOW)}

    real_count = lake_compact._count_rows

    def lying_count(files):
        return real_count(files) + 1  # форсируем расхождение

    monkeypatch.setattr(lake_compact, "_count_rows", lying_count)
    with pytest.raises(RuntimeError, match="row count mismatch"):
        compact_partition(plans[parts["p04"]], lake, trash)

    # Originals untouched, no compacted file, no stray tmp.
    assert len(sorted(parts["p04"].glob("fetched_*.parquet"))) == 3
    assert list(parts["p04"].glob("compacted_*.parquet")) == []
    assert list(parts["p04"].glob("*.tmp")) == []


def test_recompaction_after_late_backfill_is_safe(tmp_path):
    """Поздний backfill в закрытый месяц → партиция снова >1 файла →
    повторная компакция сливает compacted_* + новый файл без потерь."""
    lake = tmp_path / "lake"
    trash = tmp_path / "trash"
    parts = _build_lake(lake)
    compact_lake(lake, trash, now=NOW)

    _write_batch(
        parts["p04"], "fetched_999_z.parquet", ["hh:99"], "2026-04-30T00:00:00+00:00"
    )
    results = compact_lake(lake, trash / "second", now=NOW)
    compacted_dirs = {plan.partition_dir for plan, _, _ in results}
    assert parts["p04"] in compacted_dirs

    meta = latest_snapshot_meta(lake, source="hh")
    assert "hh:99" in meta["vacancy_id"].to_list()
    assert len(sorted(parts["p04"].glob("*.parquet"))) == 1


def test_cli_prune_lake_dry_and_real(tmp_path, monkeypatch, capsys):
    lake = tmp_path / "lake"
    _build_lake(lake)
    monkeypatch.chdir(tmp_path)

    from src.cli import main

    rc = main(
        [
            "prune",
            "lake",
            "--dry",
            "--lake-root",
            str(lake),
            "--trash-dir",
            str(tmp_path / "trash"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "would compact" in out
    assert not (tmp_path / "trash").exists()

    rc = main(
        [
            "prune",
            "lake",
            "--lake-root",
            str(lake),
            "--trash-dir",
            str(tmp_path / "trash"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "compacted" in out
    assert "originals preserved" in out
    assert (tmp_path / "trash").exists()


def test_cli_prune_lake_missing_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    from src.cli import main

    rc = main(["prune", "lake", "--lake-root", str(tmp_path / "nope")])
    assert rc == 2
    assert "does not exist" in capsys.readouterr().err


def test_cli_prune_lake_nothing_to_do(tmp_path, monkeypatch, capsys):
    lake = tmp_path / "lake"
    p06 = lake / "year=2026" / "month=06" / "source=hh"
    _write_batch(p06, "fetched_1.parquet", ["hh:1"], "2026-06-01T00:00:00+00:00")
    monkeypatch.chdir(tmp_path)
    from src.cli import main

    rc = main(["prune", "lake", "--lake-root", str(lake)])
    assert rc == 0
    assert "nothing to compact" in capsys.readouterr().out
