from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import polars as pl
import pytest

from src.ingest.tg_client import TGMessage
from src.ingest.raw_lake import (
    RawRecord,
    _extract_professional_role_id,
    content_hash,
    latest_snapshot,
    latest_snapshot_meta,
    load_raw_json_for,
    read_lake,
    scan_lake,
    snapshot_at,
    write_batch,
)


def test_extract_professional_role_id_from_shards_payload():
    """hh shards uses professionalRoleIds with nested professionalRoleId list."""
    item = {"professionalRoleIds": [{"professionalRoleId": [156]}]}
    assert _extract_professional_role_id(item) == 156


def test_extract_professional_role_id_from_api_payload():
    """hh api uses professional_roles list with id objects."""
    item = {"professional_roles": [{"id": "165", "name": "Архитектор"}]}
    assert _extract_professional_role_id(item) == 165


def test_extract_professional_role_id_missing_returns_none():
    assert _extract_professional_role_id({"name": "Vacancy"}) is None



def _hh_item(vid: str, salary_min: int = 100_000, name: str = "Data Analyst") -> dict:
    return {
        "id": vid,
        "name": name,
        "employer": {"id": "111", "name": "ACME"},
        "salary": {"from": salary_min, "to": salary_min + 50_000, "currency": "RUR"},
        "published_at": "2026-04-25T10:00:00+0300",
    }


def test_content_hash_stable_across_key_order():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert content_hash(a) == content_hash(b)


def test_content_hash_changes_when_value_changes():
    a = {"x": 1}
    b = {"x": 2}
    assert content_hash(a) != content_hash(b)


def test_from_hh_item_namespaces_id_and_extracts_employer():
    fetched = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    rec = RawRecord.from_hh_item(_hh_item("12345"), fetched)

    assert rec.vacancy_id == "hh:12345"
    assert rec.source == "hh"
    assert rec.employer_id == "111"
    assert rec.posted_at is not None
    assert rec.posted_at.year == 2026


def test_from_hh_item_extracts_scope_and_professional_role():
    fetched = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    item = {
        **_hh_item("12345"),
        "professional_roles": [{"id": "156", "name": "BI-аналитик"}],
    }

    rec = RawRecord.from_hh_item(item, fetched, market_scope="it")

    assert rec.market_scope == "it"
    assert rec.professional_role_id == 156


def test_from_hh_shards_item_extracts_scope_and_professional_role():
    fetched = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    item = {
        "vacancyId": 12345,
        "name": "Developer",
        "company": {"id": 111},
        "professionalRoles": [{"id": "96", "name": "Программист, разработчик"}],
    }

    rec = RawRecord.from_hh_shards_item(item, fetched, market_scope="it")

    assert rec.market_scope == "it"
    assert rec.professional_role_id == 96


def test_from_hh_item_handles_missing_employer():
    fetched = datetime(2026, 4, 26, tzinfo=timezone.utc)
    item = {"id": "999", "name": "X"}
    rec = RawRecord.from_hh_item(item, fetched)

    assert rec.vacancy_id == "hh:999"
    assert rec.employer_id is None
    assert rec.posted_at is None


def test_from_telegram_message_round_trip():
    fetched = datetime(2026, 4, 28, 13, tzinfo=timezone.utc)
    posted = datetime(2026, 4, 28, 12, tzinfo=timezone.utc)
    msg = TGMessage(
        channel="jobs",
        message_id=42,
        date=posted,
        text="Data analyst\nЗП 200-300к",
        views=100,
    )

    rec = RawRecord.from_telegram_message(msg, fetched)
    payload = json.loads(rec.raw_json)

    assert rec.vacancy_id == "tg:jobs:42"
    assert rec.source == "telegram"
    assert rec.fetched_at == fetched
    assert rec.posted_at == posted
    assert rec.employer_id is None
    assert payload == {
        "channel": "jobs",
        "message_id": 42,
        "date": "2026-04-28T12:00:00+00:00",
        "text": "Data analyst\nЗП 200-300к",
        "views": 100,
    }


def test_write_batch_creates_hive_partition(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    records = [RawRecord.from_hh_item(_hh_item(str(i)), fetched) for i in range(3)]

    path = write_batch(records, tmp_path)

    assert path.exists()
    assert "year=2026" in str(path)
    assert "month=04" in str(path)
    assert "source=hh" in str(path)
    df = pl.read_parquet(path)
    assert df.height == 3
    assert "market_scope" in df.columns
    assert "professional_role_id" in df.columns


def test_write_batch_persists_scope_columns(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    item = {
        **_hh_item("a"),
        "professional_roles": [{"id": "156", "name": "BI-аналитик"}],
    }

    path = write_batch([RawRecord.from_hh_item(item, fetched, market_scope="it")], tmp_path)
    df = pl.read_parquet(path)

    assert df["market_scope"].to_list() == ["it"]
    assert df["professional_role_id"].to_list() == [156]


def test_write_batch_rejects_mixed_fetched_at(tmp_path: Path):
    rec_a = RawRecord.from_hh_item(_hh_item("a"), datetime(2026, 4, 26, tzinfo=timezone.utc))
    rec_b = RawRecord.from_hh_item(_hh_item("b"), datetime(2026, 4, 27, tzinfo=timezone.utc))

    with pytest.raises(ValueError, match="fetched_at"):
        write_batch([rec_a, rec_b], tmp_path)


def test_write_batch_rejects_empty(tmp_path: Path):
    with pytest.raises(ValueError, match="empty"):
        write_batch([], tmp_path)


def test_read_lake_returns_empty_df_when_no_files(tmp_path: Path):
    df = read_lake(tmp_path)
    assert df.is_empty()
    assert "vacancy_id" in df.columns


def test_read_lake_projects_requested_columns(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a"), fetched)], tmp_path)

    df = read_lake(tmp_path, source="hh", columns=["vacancy_id", "content_hash"])

    assert df.columns == ["vacancy_id", "content_hash"]
    assert df["vacancy_id"].to_list() == ["hh:a"]


def test_latest_snapshot_takes_most_recent_fetched_at(tmp_path: Path):
    t1 = datetime(2026, 4, 25, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 100_000), t1)], tmp_path)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 200_000), t2)], tmp_path)
    write_batch([RawRecord.from_hh_item(_hh_item("b"), t2)], tmp_path)

    snap = latest_snapshot(tmp_path, source="hh")

    assert snap.height == 2
    a_row = snap.filter(pl.col("vacancy_id") == "hh:a").to_dicts()[0]
    # fetched_at hits >=t2 means 200_000 salary version wins (different content_hash)
    assert "200000" in a_row["raw_json"] or "200_000" in a_row["raw_json"]


def test_latest_snapshot_keeps_last_overlap_row_for_same_fetched_at(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    records = [
        RawRecord.from_hh_item(_hh_item("a", 100_000), fetched),
        RawRecord.from_hh_item(_hh_item("a", 200_000), fetched),
    ]
    write_batch(records, tmp_path)

    snap = latest_snapshot(tmp_path, source="hh")

    assert snap.height == 1
    a_row = snap.to_dicts()[0]
    assert "200000" in a_row["raw_json"] or "200_000" in a_row["raw_json"]


def test_scan_lake_returns_empty_lazy_frame_when_no_files(tmp_path: Path):
    lf = scan_lake(tmp_path)
    df = lf.collect()
    assert df.is_empty()
    assert "vacancy_id" in df.columns


def test_scan_lake_pushes_projection_into_plan(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a"), fetched)], tmp_path)

    lf = scan_lake(tmp_path, source="hh", columns=["vacancy_id", "content_hash"])

    # Lazy schema (без collect) уже отражает projection — Polars optimiser
    # сужает scan до запрошенных колонок.
    schema_names = lf.collect_schema().names()
    assert schema_names == ["vacancy_id", "content_hash"]
    assert "raw_json" not in schema_names

    df = lf.collect()
    assert df.columns == ["vacancy_id", "content_hash"]


def test_latest_snapshot_meta_excludes_raw_json(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a"), fetched)], tmp_path)

    meta = latest_snapshot_meta(tmp_path, source="hh")

    assert "raw_json" not in meta.columns
    assert "fetched_at" not in meta.columns
    assert set(meta.columns) == {"vacancy_id", "employer_id", "content_hash"}
    assert meta.height == 1


def test_latest_snapshot_meta_picks_latest_per_vacancy(tmp_path: Path):
    t1 = datetime(2026, 4, 25, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 100_000), t1)], tmp_path)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 200_000), t2)], tmp_path)

    meta = latest_snapshot_meta(tmp_path, source="hh")

    assert meta.height == 1
    # Хэш должен совпадать с содержимым вакансии в t2 (200_000).
    expected_hash = content_hash(_hh_item("a", 200_000))
    assert meta["content_hash"].to_list() == [expected_hash]


def test_load_raw_json_for_filters_to_requested_ids(tmp_path: Path):
    fetched = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    records = [
        RawRecord.from_hh_item(_hh_item("a"), fetched),
        RawRecord.from_hh_item(_hh_item("b"), fetched),
        RawRecord.from_hh_item(_hh_item("c"), fetched),
    ]
    write_batch(records, tmp_path)

    df = load_raw_json_for(tmp_path, ["hh:a", "hh:c"], source="hh")

    assert df.height == 2
    assert sorted(df["vacancy_id"].to_list()) == ["hh:a", "hh:c"]
    assert all(s and "salary" in s for s in df["raw_json"].to_list())


def test_load_raw_json_for_returns_empty_when_no_ids(tmp_path: Path):
    df = load_raw_json_for(tmp_path, [], source="hh")
    assert df.is_empty()
    assert "raw_json" in df.columns


def test_load_raw_json_for_picks_latest_when_multiple_fetches(tmp_path: Path):
    t1 = datetime(2026, 4, 25, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 100_000), t1)], tmp_path)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 200_000), t2)], tmp_path)

    df = load_raw_json_for(tmp_path, ["hh:a"], source="hh")

    assert df.height == 1
    raw = df["raw_json"][0]
    assert "200000" in raw or "200_000" in raw
    assert "100000" not in raw and "100_000" not in raw


def test_snapshot_at_excludes_future_records(tmp_path: Path):
    t1 = datetime(2026, 4, 25, 12, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 26, 12, tzinfo=timezone.utc)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 100_000), t1)], tmp_path)
    write_batch([RawRecord.from_hh_item(_hh_item("a", 200_000), t2)], tmp_path)

    snap = snapshot_at(tmp_path, t1)

    assert snap.height == 1
    a_row = snap.to_dicts()[0]
    assert "100000" in a_row["raw_json"] or "100_000" in a_row["raw_json"]


# ---------------------------------------------------------------------------
# Coverage gaps: _role_id_from_value, _shards_iso, _parse_iso, read_lake.
# ---------------------------------------------------------------------------


def test_role_id_from_value_returns_none_for_unparseable_string():
    """str без int-shape → TypeError/ValueError → None (lines 166-169)."""
    from src.ingest.raw_lake import _role_id_from_value

    assert _role_id_from_value("not-a-number") is None
    assert _role_id_from_value({}) is None  # dict без id-ключей


def test_role_id_from_value_returns_none_when_list_has_no_resolvable_item():
    """list без resolvable role_id во всех элементах → None (line 158)."""
    from src.ingest.raw_lake import _role_id_from_value

    assert _role_id_from_value([{}, "garbage"]) is None
    # А вот list с одним valid item → попадает в line 156 branch (return role_id)
    assert _role_id_from_value(["abc", "42", "def"]) == 42


def test_shards_iso_returns_none_for_unexpected_type():
    """`_shards_iso`: не str, не dict, не None → return None (line 180)."""
    from src.ingest.raw_lake import _shards_iso

    assert _shards_iso(12345) is None
    assert _shards_iso([1, 2, 3]) is None


def test_parse_iso_returns_none_for_invalid_string():
    """`_parse_iso`: garbage string → ValueError catch → None (lines 188-189)."""
    from src.ingest.raw_lake import _parse_iso

    assert _parse_iso("not-iso") is None
    assert _parse_iso("") is None  # empty → early return None (line 184-185)
    assert _parse_iso(None) is None


def test_read_lake_returns_empty_on_compute_error(tmp_path: Path, monkeypatch):
    """ComputeError при scan_lake (corrupted parquet / schema mismatch) →
    fallback на empty DF с правильным schema (lines 289-290)."""

    def fake_scan(*args, **kwargs):
        raise pl.exceptions.ComputeError("synthetic schema mismatch")

    monkeypatch.setattr("src.ingest.raw_lake.scan_lake", fake_scan)
    df = read_lake(tmp_path, source="hh")
    assert df.is_empty()
    # Schema присутствует (empty но typed), чтобы downstream могли apply column ops.
    assert "vacancy_id" in df.columns
