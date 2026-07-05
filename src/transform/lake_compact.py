"""Compact closed-month raw-lake partitions into single parquet files.

WHY: каждый ingest-батч пишет отдельный `fetched_<ts>_<uuid>.parquet`. За два
месяца сбора в лейке накопилось ~19.7k мелких файлов, и каждый
`latest_snapshot_meta` (events diff в начале ingest hh/telegram) открывает все
футеры — 3-8 минут на запуск на iMac. Компакция закрытых месяцев (туда никто
больше не пишет: партиционирование по fetched_at) сводит партицию к одному
файлу без изменения данных.

Crash-safety (читатели корректны на каждом шаге):
1. merge пишется в `.compact_<hex>.tmp` — не матчится глобом `*.parquet`,
   читатели его не видят; обрыв оставляет только мусорный tmp.
2. verify: row count tmp == row count исходников (стриминговый счёт).
3. rename tmp → `compacted_<hex>.parquet` — с этого момента строки временно
   задублированы (compacted + оригиналы). Это безопасно: все читатели лейка
   дедуплицируют `unique(subset=["vacancy_id"], keep="last")`, а дубликаты
   побайтово идентичны.
4. оригиналы переезжают в trash-директорию (вне lake_root — глоб их не видит).
   Обрыв между 3 и 4 самовосстанавливается: партиция снова содержит >1 файла
   и будет перекомпачена следующим прогоном (дедуп схлопнет дубли и в merge).

Имя `compacted_*` сортируется раньше `fetched_*` ('c' < 'f'), поэтому порядок
конкатенации файлов сохраняет хронологию — tie-break `_row_order` в
`latest_snapshot_meta` остаётся стабильным.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import polars as pl

from src.ingest.raw_lake import _LAKE_SCHEMA


@dataclass(frozen=True)
class PartitionPlan:
    """Партиция year=Y/month=M/source=S, подлежащая компакции."""

    partition_dir: Path
    files: tuple[Path, ...]


def plan_compaction(
    lake_root: Path, *, now: datetime | None = None
) -> list[PartitionPlan]:
    """Партиции строго раньше текущего UTC (year, month) с >1 parquet-файлом.

    Текущий месяц не трогаем: в него пишут активные ingest-прогоны, и партиция
    ещё растёт — компакция дала бы мало и могла бы гоняться с writer'ом.
    """
    moment = now or datetime.now(timezone.utc)
    plans: list[PartitionPlan] = []
    for src_dir in sorted(lake_root.glob("year=*/month=*/source=*")):
        if not src_dir.is_dir():
            continue
        try:
            year = int(src_dir.parent.parent.name.split("=", 1)[1])
            month = int(src_dir.parent.name.split("=", 1)[1])
        except (IndexError, ValueError):
            continue
        if (year, month) >= (moment.year, moment.month):
            continue
        files = tuple(sorted(p for p in src_dir.glob("*.parquet") if p.is_file()))
        if len(files) > 1:
            plans.append(PartitionPlan(partition_dir=src_dir, files=files))
    return plans


def _scan_files(files: tuple[Path, ...]) -> pl.LazyFrame:
    """Scan с канонической схемой — как scan_lake: старые файлы без новых
    колонок дополняются null'ами, посторонние колонки игнорируются (оригиналы
    остаются в trash, потери нет)."""
    return pl.scan_parquet(
        [str(p) for p in files],
        schema=_LAKE_SCHEMA,
        missing_columns="insert",
        extra_columns="ignore",
    )


def _count_rows(files: tuple[Path, ...]) -> int:
    """Стриминговый row count (projection pushdown — raw_json не читается)."""
    out = _scan_files(files).select(pl.len()).collect().item()
    return int(out)


def compact_partition(
    plan: PartitionPlan, lake_root: Path, trash_root: Path
) -> tuple[int, int]:
    """Скомпактить одну партицию. Возвращает (rows, files_compacted).

    Raises RuntimeError при несовпадении row count (tmp удаляется, оригиналы
    не тронуты).
    """
    expected = _count_rows(plan.files)
    tmp = plan.partition_dir / f".compact_{uuid4().hex}.tmp"
    try:
        _scan_files(plan.files).sink_parquet(tmp)
        actual = int(pl.scan_parquet(str(tmp)).select(pl.len()).collect().item())
        if actual != expected:
            raise RuntimeError(
                f"compaction row count mismatch in {plan.partition_dir}: "
                f"sources={expected} compacted={actual}; originals untouched"
            )
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    final = plan.partition_dir / f"compacted_{uuid4().hex}.parquet"
    tmp.rename(final)

    # Оригиналы → trash с сохранением Hive-структуры (восстановимо вручную).
    rel = plan.partition_dir.relative_to(lake_root)
    trash_dir = trash_root / rel
    trash_dir.mkdir(parents=True, exist_ok=True)
    for f in plan.files:
        f.rename(trash_dir / f.name)
    return expected, len(plan.files)


def compact_lake(
    lake_root: Path,
    trash_root: Path,
    *,
    dry: bool = False,
    now: datetime | None = None,
) -> list[tuple[PartitionPlan, int, int]]:
    """Скомпактить все закрытые партиции. Возвращает [(plan, rows, files)].

    В dry-режиме ничего не пишет; rows считается (дёшево — только футеры),
    files — сколько файлов слилось бы.
    """
    results: list[tuple[PartitionPlan, int, int]] = []
    for plan in plan_compaction(lake_root, now=now):
        if dry:
            results.append((plan, _count_rows(plan.files), len(plan.files)))
            continue
        rows, n_files = compact_partition(plan, lake_root, trash_root)
        results.append((plan, rows, n_files))
    return results
