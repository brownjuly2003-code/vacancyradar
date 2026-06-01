"""Shrinkage guard for neon_sync.sync_parquet_to_neon.

Pure-function unit tests — the guard itself is decoupled from psycopg, so we
don't need a real Neon connection. The integration parity test in
tests/integration/test_neon_parity.py covers the SQL path against live Neon
when NEON_DATABASE_URL is set.

Root cause: full-snapshot sync used to `DELETE FROM vacancies WHERE
vacancy_id NOT IN (SELECT vacancy_id FROM stage_vacancies)` without any
shrinkage or freshness check. A truncated `derived/slim_active.parquet`
(disk full mid-write, IT-scope filter regression, etc.) would silently wipe
prod search rows. Flagged by both CX and KM in the 2026-05-17 audit.
"""
from __future__ import annotations

import datetime as dt

import pytest

from src.publish.neon_sync import (
    SHRINKAGE_THRESHOLD,
    ShrinkageGuardError,
    check_shrinkage_guard,
)


def _ts(hour: int = 12) -> dt.datetime:
    return dt.datetime(2026, 5, 17, hour, 0, 0, tzinfo=dt.UTC)


def test_first_run_no_current_rows_passes() -> None:
    check_shrinkage_guard(
        staged_count=0,
        current_count=0,
        staged_max_seen=None,
        current_max_seen=None,
    )
    check_shrinkage_guard(
        staged_count=67000,
        current_count=0,
        staged_max_seen=_ts(),
        current_max_seen=None,
    )


def test_growth_passes() -> None:
    check_shrinkage_guard(
        staged_count=70000,
        current_count=67000,
        staged_max_seen=_ts(13),
        current_max_seen=_ts(12),
    )


def test_within_threshold_passes() -> None:
    # 5% threshold: 67000 * 0.95 = 63650; staged at 64000 is fine.
    check_shrinkage_guard(
        staged_count=64000,
        current_count=67000,
        staged_max_seen=_ts(13),
        current_max_seen=_ts(12),
    )


def test_excessive_shrinkage_blocks() -> None:
    # Staged 50000 vs current 67000 = 25% loss, well over 5% threshold.
    with pytest.raises(ShrinkageGuardError, match="shrink vacancies"):
        check_shrinkage_guard(
            staged_count=50000,
            current_count=67000,
            staged_max_seen=_ts(13),
            current_max_seen=_ts(12),
        )


def test_excessive_shrinkage_bypassed_with_force(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        check_shrinkage_guard(
            staged_count=50000,
            current_count=67000,
            staged_max_seen=_ts(13),
            current_max_seen=_ts(12),
            force=True,
        )
    assert any("shrinkage guard bypassed" in r.message for r in caplog.records)


def test_last_seen_regression_blocks() -> None:
    # Same row count, but staged timestamps are older — stale parquet.
    with pytest.raises(ShrinkageGuardError, match="regressed"):
        check_shrinkage_guard(
            staged_count=67000,
            current_count=67000,
            staged_max_seen=_ts(10),
            current_max_seen=_ts(12),
        )


def test_last_seen_regression_bypassed_with_force() -> None:
    check_shrinkage_guard(
        staged_count=67000,
        current_count=67000,
        staged_max_seen=_ts(10),
        current_max_seen=_ts(12),
        force=True,
    )


def test_null_staged_max_seen_does_not_trigger_regression() -> None:
    # Empty staged max(last_seen_at) shouldn't be compared against current —
    # row-count guard already covers the empty-staged scenario.
    with pytest.raises(ShrinkageGuardError, match="shrink vacancies"):
        check_shrinkage_guard(
            staged_count=0,
            current_count=67000,
            staged_max_seen=None,
            current_max_seen=_ts(12),
        )


def test_threshold_is_inclusive_boundary() -> None:
    # At exactly current * (1 - threshold), the guard should pass (not block).
    # int(67000 * 0.95) = 63650; staged=63650 passes, staged=63649 blocks.
    check_shrinkage_guard(
        staged_count=63650,
        current_count=67000,
        staged_max_seen=_ts(13),
        current_max_seen=_ts(12),
    )
    with pytest.raises(ShrinkageGuardError):
        check_shrinkage_guard(
            staged_count=63649,
            current_count=67000,
            staged_max_seen=_ts(13),
            current_max_seen=_ts(12),
        )


def test_custom_threshold_overrides_default() -> None:
    # Strict 1% threshold rejects 3% loss.
    with pytest.raises(ShrinkageGuardError):
        check_shrinkage_guard(
            staged_count=65000,
            current_count=67000,
            staged_max_seen=_ts(13),
            current_max_seen=_ts(12),
            threshold=0.01,
        )


def test_threshold_default_value() -> None:
    assert SHRINKAGE_THRESHOLD == pytest.approx(0.05)
