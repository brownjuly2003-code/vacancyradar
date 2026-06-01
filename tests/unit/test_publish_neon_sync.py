"""Unit tests for src.publish.neon_sync end-to-end sync path.

The shrinkage guard itself has its own pure-function suite in
test_publish_neon_shrinkage.py. This file covers the surrounding glue:

- `_pg_array_literal` — Postgres array escaping (skills column).
- `_row_to_copy_record` — Polars row → COPY tuple mapping with defaults.
- `sync_parquet_to_neon` — full flow against a MagicMock psycopg connection:
  dry mode, missing parquet, happy path, init_schema, shrinkage abort,
  CSV escaping of titles with delimiters/quotes/newlines.
- `main` — argparse-level exit codes (1 missing DSN, 4 guard error, 0 ok).
- `apply_schema` — verifies neon_schema.sql contents reach the cursor.

Kimi audit 2026-05-17 P0-1 flagged this module as 0-coverage in the
end-to-end path. Existing parity test (`tests/integration/test_neon_parity.py`)
covers real-Neon behavior when NEON_DATABASE_URL is set; these are the
gate-free unit tests.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import polars as pl
import pytest

from src.publish import neon_sync
from src.publish.neon_sync import (
    COLUMNS,
    ShrinkageGuardError,
    _pg_array_literal,
    _row_to_copy_record,
    apply_schema,
    main,
    sync_parquet_to_neon,
)


# --- _pg_array_literal -----------------------------------------------------


def test_pg_array_literal_none_returns_empty() -> None:
    assert _pg_array_literal(None) == "{}"


def test_pg_array_literal_empty_list_returns_empty() -> None:
    assert _pg_array_literal([]) == "{}"


def test_pg_array_literal_simple() -> None:
    assert _pg_array_literal(["python", "sql"]) == '{"python","sql"}'


def test_pg_array_literal_escapes_double_quote() -> None:
    # Postgres array literal: backslash-escape embedded double quotes.
    assert _pg_array_literal(['c++ "modern"']) == '{"c++ \\"modern\\""}'


def test_pg_array_literal_escapes_backslash() -> None:
    # Backslash must be doubled before the quote-escape pass runs.
    assert _pg_array_literal(["a\\b"]) == '{"a\\\\b"}'


# --- _row_to_copy_record ---------------------------------------------------


def _full_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {c: None for c in COLUMNS}
    base.update({
        "vacancy_id": "hh:111",
        "title": "Data Engineer",
        "employer_name": "Acme",
        "skills": ["python", "sql"],
        "source": "hh",
        "first_seen_at": dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
        "last_seen_at": dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
    })
    base.update(overrides)
    return base


def test_row_to_copy_record_preserves_position_and_count() -> None:
    rec = _row_to_copy_record(_full_row())
    assert len(rec) == len(COLUMNS)
    assert rec[0] == "hh:111"
    assert rec[1] == "Data Engineer"


def test_row_to_copy_record_skills_list_serialised_as_pg_array() -> None:
    rec = _row_to_copy_record(_full_row(skills=["python", "go"]))
    skills_index = COLUMNS.index("skills")
    assert rec[skills_index] == '{"python","go"}'


def test_row_to_copy_record_skills_iterable_serialised_as_pg_array() -> None:
    rec = _row_to_copy_record(_full_row(skills=("python", "go")))
    skills_index = COLUMNS.index("skills")
    assert rec[skills_index] == '{"python","go"}'


def test_row_to_copy_record_none_skills_become_empty_pg_array() -> None:
    rec = _row_to_copy_record(_full_row(skills=None))
    skills_index = COLUMNS.index("skills")
    assert rec[skills_index] == "{}"


def test_row_to_copy_record_none_title_becomes_empty_string() -> None:
    # vacancy_id required; title fills to "" so COPY never emits NULL there.
    rec = _row_to_copy_record(_full_row(title=None))
    assert rec[1] == ""


def test_row_to_copy_record_defaults_remote_and_seniority_to_unknown() -> None:
    rec = _row_to_copy_record(_full_row(remote_type=None, seniority=None))
    remote_index = COLUMNS.index("remote_type")
    seniority_index = COLUMNS.index("seniority")
    assert rec[remote_index] == "unknown"
    assert rec[seniority_index] == "unknown"


def test_row_to_copy_record_defaults_source_to_hh() -> None:
    rec = _row_to_copy_record(_full_row(source=None))
    source_index = COLUMNS.index("source")
    assert rec[source_index] == "hh"


# --- sync_parquet_to_neon helpers -----------------------------------------


def _write_test_parquet(path: Path, rows: int = 2) -> Path:
    """Write a minimal slim_active.parquet at `path` with `rows` records."""
    records = []
    for i in range(rows):
        records.append({
            "vacancy_id": f"hh:{i}",
            "title": f"Data Engineer {i}",
            "employer_id": "hh:1",
            "employer_name": "Acme",
            "salary_rub_min": 200000,
            "salary_rub_max": 300000,
            "salary_currency": "RUB",
            "salary_disclosed": True,
            "city": "Москва",
            "region": "Центральный",
            "remote_type": "remote",
            "seniority": "senior",
            "description_teaser": "Python, SQL",
            "skills": ["python", "sql"],
            "source": "hh",
            "market_scope": "it",
            "professional_role_id": "96",
            "source_url": f"https://hh.ru/vacancy/{i}",
            "first_seen_at": dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            "last_seen_at": dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
            "posted_at": dt.datetime(2026, 4, 30, tzinfo=dt.UTC),
        })
    df = pl.DataFrame(records)
    df.write_parquet(path)
    return path


def _make_fake_psycopg(
    *,
    staged_count: int,
    staged_max_seen: dt.datetime | None,
    current_count: int,
    current_max_seen: dt.datetime | None,
    upserted: int,
    deleted: int,
) -> tuple[MagicMock, MagicMock, MagicMock, list[str]]:
    """Build a MagicMock psycopg.connect drop-in.

    Returns (connect_mock, conn_mock, cursor_mock, copy_lines) where
    `copy_lines` is a list that captures everything written into the COPY
    pipe so tests can assert CSV escaping.
    """
    copy_lines: list[str] = []
    copy_ctx = MagicMock()
    copy_ctx.write = lambda line: copy_lines.append(line)
    copy_mock = MagicMock()
    copy_mock.__enter__ = MagicMock(return_value=copy_ctx)
    copy_mock.__exit__ = MagicMock(return_value=False)

    cursor = MagicMock()
    cursor.copy.return_value = copy_mock
    # Queries inside sync_parquet_to_neon (after COPY) fetchone() in order:
    # 1. SELECT COUNT(*) FROM stage_vacancies
    # 2. SELECT MAX(last_seen_at) FROM stage_vacancies
    # 3. SELECT COUNT(*), MAX(last_seen_at) FROM vacancies
    cursor.fetchone.side_effect = [
        (staged_count,),
        (staged_max_seen,),
        (current_count, current_max_seen),
    ]
    # rowcount is read exactly twice in sync_parquet_to_neon — once after the
    # upsert, once after the delete. Drive it from a 2-element iterator.
    cursor._rowcounts = iter([upserted, deleted])
    type(cursor).rowcount = property(lambda self: next(self._rowcounts))

    cursor_ctx = MagicMock()
    cursor_ctx.__enter__ = MagicMock(return_value=cursor)
    cursor_ctx.__exit__ = MagicMock(return_value=False)

    conn = MagicMock()
    conn.cursor.return_value = cursor_ctx

    conn_ctx = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    connect_mock = MagicMock(return_value=conn_ctx)
    return connect_mock, conn, cursor, copy_lines


# --- sync_parquet_to_neon --------------------------------------------------


def test_sync_dry_mode_skips_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=3)
    connect_mock = MagicMock()
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    stats = sync_parquet_to_neon(pq, "postgresql://ignored", dry=True)

    assert stats == {"rows_read": 3, "rows_upserted": 0, "rows_deleted": 0}
    connect_mock.assert_not_called()


def test_sync_missing_parquet_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing parquet"):
        sync_parquet_to_neon(tmp_path / "absent.parquet", "postgresql://ignored")


def test_sync_happy_path_returns_stats_and_runs_full_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=2)
    connect_mock, _conn, cursor, copy_lines = _make_fake_psycopg(
        staged_count=2,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=2,
        current_max_seen=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
        upserted=2,
        deleted=0,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    stats = sync_parquet_to_neon(pq, "postgresql://x")

    assert stats == {"rows_read": 2, "rows_upserted": 2, "rows_deleted": 0}
    connect_mock.assert_called_once_with("postgresql://x")
    # Two rows piped through COPY in CSV form, each terminated by \n.
    assert len(copy_lines) == 2
    assert all(line.endswith("\n") for line in copy_lines)
    # Sequence sanity: CREATE TEMP TABLE before any SELECT/INSERT/DELETE.
    executed_sql = [
        c.args[0] if c.args else None
        for c in cursor.execute.call_args_list
    ]
    first_query = str(executed_sql[0])
    assert "CREATE TEMP TABLE stage_vacancies" in first_query
    # DELETE happens last (after shrinkage guard + upsert).
    assert "DELETE FROM vacancies" in str(executed_sql[-1])


def test_sync_copy_uses_bounded_batches_with_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=3)
    connect_mock, conn, cursor, copy_lines = _make_fake_psycopg(
        staged_count=3,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=3,
        current_max_seen=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
        upserted=3,
        deleted=0,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    stats = sync_parquet_to_neon(pq, "postgresql://x", copy_batch_size=2)

    assert stats == {"rows_read": 3, "rows_upserted": 3, "rows_deleted": 0}
    assert cursor.copy.call_count == 2
    assert len(copy_lines) == 3
    assert conn.commit.call_count == 4


def test_sync_init_schema_applies_schema_before_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=1)
    connect_mock, _conn, cursor, _copy = _make_fake_psycopg(
        staged_count=1,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=0,
        current_max_seen=None,
        upserted=1,
        deleted=0,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    sync_parquet_to_neon(pq, "postgresql://x", init_schema=True)

    schema_text = neon_sync.SCHEMA_PATH.read_text(encoding="utf-8")
    # First execute call is the full schema string.
    assert cursor.execute.call_args_list[0] == call(schema_text)


def test_sync_treats_missing_current_count_row_as_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=1)
    staged_seen = dt.datetime(2026, 5, 17, tzinfo=dt.UTC)
    connect_mock, _conn, cursor, _copy = _make_fake_psycopg(
        staged_count=1,
        staged_max_seen=staged_seen,
        current_count=0,
        current_max_seen=None,
        upserted=1,
        deleted=0,
    )
    cursor.fetchone.side_effect = [
        (1,),
        (staged_seen,),
        None,
    ]
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    stats = sync_parquet_to_neon(pq, "postgresql://x")

    assert stats == {"rows_read": 1, "rows_upserted": 1, "rows_deleted": 0}


def test_sync_shrinkage_abort_raises_before_destructive_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=1)
    # staged=1 vs current=67000 → 99.99% shrinkage, guard must abort.
    connect_mock, _conn, cursor, _copy = _make_fake_psycopg(
        staged_count=1,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=67000,
        current_max_seen=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
        upserted=0,
        deleted=0,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    with pytest.raises(ShrinkageGuardError, match="shrink vacancies"):
        sync_parquet_to_neon(pq, "postgresql://x")

    # Critical: DELETE FROM vacancies must NEVER reach the cursor on abort.
    executed = [
        str(c.args[0]) if c.args else "" for c in cursor.execute.call_args_list
    ]
    assert not any("DELETE FROM vacancies" in q for q in executed)


def test_sync_shrinkage_force_bypasses_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=1)
    connect_mock, _conn, cursor, _copy = _make_fake_psycopg(
        staged_count=1,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=67000,
        current_max_seen=dt.datetime(2026, 5, 16, tzinfo=dt.UTC),
        upserted=1,
        deleted=66999,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    stats = sync_parquet_to_neon(pq, "postgresql://x", force=True)

    assert stats["rows_deleted"] == 66999
    executed = [
        str(c.args[0]) if c.args else "" for c in cursor.execute.call_args_list
    ]
    assert any("DELETE FROM vacancies" in q for q in executed)


def test_sync_csv_escapes_titles_with_commas_quotes_and_newlines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq_path = tmp_path / "slim.parquet"
    df = pl.DataFrame([
        {
            "vacancy_id": "hh:1",
            "title": 'Data, "Senior"\nEngineer',  # comma + quote + newline
            "employer_id": "hh:1",
            "employer_name": "Acme",
            "salary_rub_min": None,
            "salary_rub_max": None,
            "salary_currency": None,
            "salary_disclosed": False,
            "city": None,
            "region": None,
            "remote_type": None,
            "seniority": None,
            "description_teaser": None,
            "skills": None,
            "source": "hh",
            "market_scope": "it",
            "professional_role_id": None,
            "source_url": None,
            "first_seen_at": dt.datetime(2026, 5, 1, tzinfo=dt.UTC),
            "last_seen_at": dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
            "posted_at": None,
        },
    ])
    df.write_parquet(pq_path)
    connect_mock, _conn, _cursor, copy_lines = _make_fake_psycopg(
        staged_count=1,
        staged_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        current_count=0,
        current_max_seen=None,
        upserted=1,
        deleted=0,
    )
    monkeypatch.setattr(neon_sync.psycopg, "connect", connect_mock)

    sync_parquet_to_neon(pq_path, "postgresql://x")

    assert len(copy_lines) == 1
    line = copy_lines[0]
    # Title field wrapped in double quotes; embedded quote doubled.
    assert '"Data, ""Senior""\nEngineer"' in line
    # None salary_rub_min → \N literal (Postgres NULL marker).
    assert "\\N" in line
    # Boolean False rendered as 'f'.
    assert ",f," in line
    # Trailing newline terminates the COPY record.
    assert line.endswith("\n")


# --- apply_schema ----------------------------------------------------------


def test_apply_schema_executes_full_file_then_commits() -> None:
    cursor = MagicMock()
    cursor_ctx = MagicMock()
    cursor_ctx.__enter__ = MagicMock(return_value=cursor)
    cursor_ctx.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cursor_ctx

    apply_schema(conn)

    expected = neon_sync.SCHEMA_PATH.read_text(encoding="utf-8")
    cursor.execute.assert_called_once_with(expected)
    conn.commit.assert_called_once()


# --- main (CLI exit codes) -------------------------------------------------


def test_main_missing_neon_database_url_returns_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    assert main() == 1


def test_main_shrinkage_error_returns_4(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://fake")

    def _raise(*_a: Any, **_kw: Any) -> None:
        raise ShrinkageGuardError("staged would shrink vacancies by 99%")

    monkeypatch.setattr(neon_sync, "sync_parquet_to_neon", _raise)

    assert main() == 4


def test_main_success_returns_0_and_prints_stats(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEON_DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(
        neon_sync,
        "sync_parquet_to_neon",
        lambda *a, **kw: {"rows_read": 100, "rows_upserted": 80, "rows_deleted": 20},
    )

    rc = main(init=True, dry=False, force=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "rows_read=100" in out
    assert "upserted=80" in out
    assert "deleted=20" in out


# --- retry on transient psycopg.OperationalError ---------------------------


def test_sync_retries_transient_operational_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Neon serverless drops idle/long COPY connections.

    Real cron failure 2026-05-18 16:57 — COPY ran 15 min then died with
    `flushing failed: server closed the connection unexpectedly`. The whole
    transaction must retry from scratch (fresh connect + COPY + commit).
    """
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=2)

    # First connect attempt: raise OperationalError (mid-COPY drop). Second
    # attempt: full happy-path mock.
    happy_connect, _conn, _cursor, _copy_lines = _make_fake_psycopg(
        staged_count=2,
        staged_max_seen=dt.datetime(2026, 5, 18, tzinfo=dt.UTC),
        current_count=2,
        current_max_seen=dt.datetime(2026, 5, 17, tzinfo=dt.UTC),
        upserted=2,
        deleted=0,
    )

    call_count = {"n": 0}

    def _flaky_connect(url: str) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise neon_sync.psycopg.OperationalError(
                "flushing failed: server closed the connection unexpectedly"
            )
        return happy_connect(url)

    monkeypatch.setattr(neon_sync.psycopg, "connect", _flaky_connect)

    sleep_calls: list[float] = []

    stats = sync_parquet_to_neon(
        pq,
        "postgresql://x",
        max_attempts=3,
        backoff_base=0.0,  # no real wait in tests
        sleep=sleep_calls.append,
    )

    assert stats == {"rows_read": 2, "rows_upserted": 2, "rows_deleted": 0}
    assert call_count["n"] == 2, "should connect twice (1 fail + 1 success)"
    assert len(sleep_calls) == 1, "one backoff between attempts 1 and 2"


def test_sync_raises_when_all_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After max_attempts OperationalError raises through — cron sees exit 1."""
    pq = _write_test_parquet(tmp_path / "slim.parquet", rows=1)

    call_count = {"n": 0}

    def _always_fails(url: str) -> Any:
        call_count["n"] += 1
        raise neon_sync.psycopg.OperationalError(
            "server closed the connection unexpectedly"
        )

    monkeypatch.setattr(neon_sync.psycopg, "connect", _always_fails)

    sleep_calls: list[float] = []

    with pytest.raises(neon_sync.psycopg.OperationalError, match="server closed"):
        sync_parquet_to_neon(
            pq,
            "postgresql://x",
            max_attempts=3,
            backoff_base=0.0,
            sleep=sleep_calls.append,
        )

    assert call_count["n"] == 3, "all 3 attempts run before giving up"
    assert len(sleep_calls) == 2, "backoff between attempts 1->2 and 2->3, none after final"
