from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from src.ingest.raw_lake import RawRecord, write_batch
from src.transform.slim_export import (
    SALARY_OUTLIER_CEILING,
    SALARY_OUTLIER_FLOOR,
    SLIM_ACTIVE_SCHEMA,
    _clamp_salary_outliers,
    apply_cross_source_dedup,
    build_slim_active,
    write_slim_active,
)


def _empty_details_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "vacancy_id": pl.String,
            "description_teaser": pl.String,
            "description_fts": pl.String,
        }
    )


@pytest.fixture(autouse=True)
def _stub_default_hh_details_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.transform.slim_export.read_details_cache",
        lambda _path: _empty_details_frame(),
    )


def test_clamp_salary_outliers_nulls_below_floor():
    """salary=1 RUB and similar typos drop out, not clamp to 10k."""
    assert _clamp_salary_outliers(1, 200_000) == (None, 200_000)
    assert _clamp_salary_outliers(150, 250_000) == (None, 250_000)
    assert _clamp_salary_outliers(SALARY_OUTLIER_FLOOR - 1, None) == (None, None)


def test_clamp_salary_outliers_nulls_above_ceiling():
    """salary=10_000_000 TG placeholders and daily/hourly rates parsed as
    monthly drop out — they poison aggregate medians and p90."""
    assert _clamp_salary_outliers(150_000, 10_000_000) == (150_000, None)
    assert _clamp_salary_outliers(SALARY_OUTLIER_CEILING + 1, None) == (None, None)


def test_clamp_salary_outliers_keeps_plausible_range():
    """Realistic RU IT band stays untouched: junior 60k → C-level 3.5M."""
    assert _clamp_salary_outliers(60_000, 90_000) == (60_000, 90_000)
    # 4.5M is inside ceiling; 6M is above and drops
    assert _clamp_salary_outliers(3_500_000, 4_500_000) == (3_500_000, 4_500_000)
    assert _clamp_salary_outliers(3_500_000, 6_000_000) == (3_500_000, None)
    assert _clamp_salary_outliers(SALARY_OUTLIER_FLOOR, SALARY_OUTLIER_CEILING) == (
        SALARY_OUTLIER_FLOOR,
        SALARY_OUTLIER_CEILING,
    )


def test_clamp_salary_outliers_handles_nulls():
    assert _clamp_salary_outliers(None, None) == (None, None)
    assert _clamp_salary_outliers(None, 300_000) == (None, 300_000)
    assert _clamp_salary_outliers(120_000, None) == (120_000, None)


def test_clamp_salary_outliers_uses_explicit_bounds(monkeypatch):
    monkeypatch.setattr(
        "src.transform.slim_export._salary_bounds",
        lambda: (_ for _ in ()).throw(AssertionError("config loaded")),
    )

    assert _clamp_salary_outliers(50, 150, floor=100, ceiling=200) == (None, 150)


def _shards_item(vid: int, **overrides: Any) -> dict:
    base: dict[str, Any] = {
        "vacancyId": vid,
        "name": f"Data Analyst {vid}",
        "company": {"id": 100, "name": "ACME", "visibleName": "ACME Inc."},
        "compensation": {
            "from": 100000,
            "to": 200000,
            "currencyCode": "RUR",
            "gross": False,
        },
        "area": {"@id": 1, "name": "Москва", "path": ".113.1."},
        "address": {"city": "Москва"},
        "links": {"desktop": f"https://hh.ru/vacancy/{vid}"},
        "workFormats": [{"workFormatsElement": ["REMOTE"]}],
        "professionalRoles": [{"id": "156", "name": "BI-аналитик, аналитик данных"}],
        "publicationTime": "2026-04-25T12:00:00+03:00",
    }
    base.update(overrides)
    return base


def _seed_lake(lake_root: Path, items_at: list[tuple[datetime, list[dict]]]) -> None:
    for fetched_at, items in items_at:
        records = [RawRecord.from_hh_shards_item(it, fetched_at, market_scope="it") for it in items]
        if records:
            write_batch(records, lake_root)


def _seed_legacy_lake(lake_root: Path, fetched_at: datetime, items: list[dict]) -> None:
    records = [RawRecord.from_hh_shards_item(it, fetched_at) for it in items]
    path = write_batch(records, lake_root)
    df = pl.read_parquet(path).drop(["market_scope", "professional_role_id"])
    df.write_parquet(path)


def test_empty_lake_yields_empty_frame_with_schema(tmp_path: Path):
    df = build_slim_active(tmp_path)
    assert df.is_empty()
    assert dict(df.schema) == SLIM_ACTIVE_SCHEMA


def test_schema_matches_contract(tmp_path: Path):
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1)])])
    df = build_slim_active(tmp_path)
    assert dict(df.schema) == SLIM_ACTIVE_SCHEMA


def test_build_slim_active_avoids_full_eager_lake_read(tmp_path: Path, monkeypatch):
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1)])])

    def fail_full_read(*args, **kwargs):
        raise AssertionError("build_slim_active must not eager-read the full raw lake")

    monkeypatch.setattr("src.transform.slim_export.read_lake", fail_full_read, raising=False)

    df = build_slim_active(tmp_path)

    assert df.height == 1
    assert df.to_dicts()[0]["vacancy_id"] == "hh:1"


def test_build_slim_active_limit_caps_before_mapping(tmp_path: Path):
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1), _shards_item(2), _shards_item(3)])])

    df = build_slim_active(tmp_path, limit=2)

    assert df.height == 2


def test_build_slim_active_limit_zero_returns_empty_before_loading_rates(
    tmp_path: Path, monkeypatch
):
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1)])])
    monkeypatch.setattr(
        "src.transform.slim_export.load_rates_for",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rates loaded")),
    )

    df = build_slim_active(tmp_path, limit=0)

    assert df.is_empty()
    assert dict(df.schema) == SLIM_ACTIVE_SCHEMA


def test_active_window_filters_stale_rows(tmp_path: Path):
    """active_window_days=N оставляет только vacancies где last_seen_at >= now - N."""
    fresh = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    stale = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 12, 0, 0, tzinfo=timezone.utc)
    _seed_lake(
        tmp_path,
        [
            (fresh, [_shards_item(1), _shards_item(2)]),
            (stale, [_shards_item(3)]),
        ],
    )

    df_all = build_slim_active(tmp_path, now_utc=now)
    df_active = build_slim_active(tmp_path, active_window_days=7, now_utc=now)

    assert df_all.height == 3
    assert df_active.height == 2
    assert sorted(df_active["vacancy_id"].to_list()) == ["hh:1", "hh:2"]


def test_active_window_none_keeps_legacy_behavior(tmp_path: Path):
    """active_window_days=None (default) — никаких фильтров, backward compat."""
    fresh = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    very_old = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
    _seed_lake(
        tmp_path,
        [
            (fresh, [_shards_item(1)]),
            (very_old, [_shards_item(2)]),
        ],
    )
    df = build_slim_active(tmp_path)
    assert df.height == 2


def test_basic_mapping_fields(tmp_path: Path):
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    item = _shards_item(
        1,
        name="Senior Data Analyst",
        company={"id": 7, "name": "ACME", "visibleName": "ACME Inc."},
        compensation={"from": 250000, "to": None, "currencyCode": "RUR"},
        address={"city": "Санкт-Петербург"},
        links={"desktop": "https://hh.ru/vacancy/1"},
        workFormats=[{"workFormatsElement": ["HYBRID"]}],
    )
    _seed_lake(tmp_path, [(fetched, [item])])

    df = build_slim_active(tmp_path).sort("vacancy_id")
    row = df.to_dicts()[0]

    assert row["vacancy_id"] == "hh:1"
    assert row["title"] == "Senior Data Analyst"
    # contract v1: employer_id must be namespaced as <source>:<id>
    assert row["employer_id"] == "hh:7"
    assert row["employer_name"] == "ACME Inc."
    assert row["salary_currency"] == "RUR"
    assert row["salary_disclosed"] is True
    assert row["city"] == "Санкт-Петербург"
    assert row["remote_type"] == "hybrid"
    assert row["seniority"] == "senior"
    assert row["source"] == "hh"
    assert row["market_scope"] == "it"
    assert row["professional_role_id"] == 156
    assert row["source_url"] == "https://hh.ru/vacancy/1"
    assert row["skills"] == []
    # salary_rub_* normalised из RUR 1:1 (Phase 5 enrichment)
    assert row["salary_rub_min"] == 250_000
    assert row["salary_rub_max"] is None  # `to` was None в этом item
    # enrichment from regions.yaml (Санкт-Петербург → СЗФО)
    assert row["region"] == "СЗФО"
    assert row["description_teaser"] is None
    assert "description_fts" not in row


def test_employer_name_falls_back_to_name_when_visible_missing(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, company={"id": 5, "name": "Plain"})])])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["employer_name"] == "Plain"


def test_employer_id_none_when_company_id_missing(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, company={"name": "Anon"})])])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["employer_id"] is None


def test_salary_disclosed_false_when_no_bounds(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, compensation={"currencyCode": "RUR"})])])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["salary_disclosed"] is False
    assert row["salary_currency"] == "RUR"


def test_remote_type_unknown_when_no_match(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, workFormats=[])])])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["remote_type"] == "unknown"


def test_remote_type_remote_keyword(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, workFormats=[{"workFormatsElement": ["REMOTE"]}])])])
    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "remote"


def test_remote_type_office_for_field_work(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, workFormats=[{"workFormatsElement": ["FIELD_WORK"]}])])])
    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "office"


def test_remote_type_skips_malformed_work_format_elements(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _shards_item(
        1,
        workFormats=[{"workFormatsElement": [{"id": "REMOTE"}, "HYBRID"]}],
    )
    _seed_lake(tmp_path, [(fetched, [item])])

    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "hybrid"


def test_remote_type_skips_empty_work_format_entries(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _shards_item(
        1,
        workFormats=[{"workFormatsElement": []}, {"workFormatsElement": ["REMOTE"]}],
    )
    _seed_lake(tmp_path, [(fetched, [item])])

    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "remote"


def test_city_falls_back_to_area_name_when_address_missing(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _shards_item(1, address={}, area={"@id": 2, "name": "Казань", "path": ".113.4.2."})
    _seed_lake(tmp_path, [(fetched, [item])])
    assert build_slim_active(tmp_path).to_dicts()[0]["city"] == "Казань"


def test_first_last_seen_aggregates_across_batches(tmp_path: Path):
    early = datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(
        tmp_path,
        [
            (early, [_shards_item(1)]),
            (late, [_shards_item(1, name="Updated")]),
        ],
    )
    df = build_slim_active(tmp_path)
    row = df.to_dicts()[0]
    assert row["first_seen_at"] == early
    assert row["last_seen_at"] == late
    # latest snapshot wins for content fields
    assert row["title"] == "Updated"


def test_build_slim_active_handles_legacy_lake_without_scope_columns(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_legacy_lake(tmp_path, fetched, [_shards_item(1)])

    row = build_slim_active(tmp_path).to_dicts()[0]

    assert row["market_scope"] is None
    assert row["professional_role_id"] is None


def test_build_slim_active_scoped_legacy_lake_returns_empty(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_legacy_lake(tmp_path, fetched, [_shards_item(1)])

    df = build_slim_active(tmp_path, market_scope="it")

    assert df.is_empty()
    assert dict(df.schema) == SLIM_ACTIVE_SCHEMA


def test_build_slim_active_filters_by_market_scope(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    records = [
        RawRecord.from_hh_shards_item(_shards_item(1), fetched, market_scope="it"),
        RawRecord.from_hh_shards_item(_shards_item(2), fetched, market_scope=None),
    ]
    write_batch(records, tmp_path)

    scoped = build_slim_active(tmp_path, market_scope="it")
    all_rows = build_slim_active(tmp_path)

    assert scoped["vacancy_id"].to_list() == ["hh:1"]
    assert all_rows.height == 2


def test_vacancy_id_unique(tmp_path: Path):
    """Invariant 4: vacancy_id is unique in slim_active."""
    early = datetime(2026, 4, 25, 10, 0, tzinfo=timezone.utc)
    late = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)
    _seed_lake(
        tmp_path,
        [
            (early, [_shards_item(1), _shards_item(2)]),
            (late, [_shards_item(1, name="Updated"), _shards_item(3)]),
        ],
    )
    df = build_slim_active(tmp_path)
    assert df.height == 3
    assert df["vacancy_id"].n_unique() == 3


def test_first_seen_le_last_seen_invariant(tmp_path: Path):
    """Invariant 2: first_seen_at <= last_seen_at."""
    fetched = [
        datetime(2026, 4, 25, tzinfo=timezone.utc),
        datetime(2026, 4, 26, tzinfo=timezone.utc),
        datetime(2026, 4, 27, tzinfo=timezone.utc),
    ]
    _seed_lake(tmp_path, [(t, [_shards_item(1)]) for t in fetched])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["first_seen_at"] <= row["last_seen_at"]


def test_build_slim_active_keeps_one_duplicate_latest_record(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _shards_item(1)
    records = [
        RawRecord.from_hh_shards_item(item, fetched, market_scope="it"),
        RawRecord.from_hh_shards_item(item, fetched, market_scope="it"),
    ]
    write_batch(records, tmp_path)

    df = build_slim_active(tmp_path)

    assert df.height == 1
    assert df["vacancy_id"].to_list() == ["hh:1"]


def _api_item(vid: int, **overrides: Any) -> dict:
    """api.hh.ru/vacancies/search shape: id/employer/salary/area/schedule/alternate_url."""
    base: dict[str, Any] = {
        "id": str(vid),
        "name": f"Backend Engineer {vid}",
        "employer": {"id": "200", "name": "BetaCorp"},
        "salary": {"from": 150000, "to": 250000, "currency": "RUR", "gross": False},
        "area": {"id": "1", "name": "Москва"},
        "schedule": {"id": "remote", "name": "Удаленная работа"},
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "published_at": "2026-04-25T12:00:00+0300",
        "created_at": "2026-04-25T12:00:00+0300",
    }
    base.update(overrides)
    return base


def _seed_api_lake(lake_root: Path, items_at: list[tuple[datetime, list[dict]]]) -> None:
    for fetched_at, items in items_at:
        records = [RawRecord.from_hh_item(it, fetched_at, market_scope="it") for it in items]
        if records:
            write_batch(records, lake_root)


def test_api_shape_basic_mapping(tmp_path: Path):
    """api.hh.ru shape: distinct keys (employer/salary/area/alternate_url) must map."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_api_lake(tmp_path, [(fetched, [_api_item(1)])])
    row = build_slim_active(tmp_path).to_dicts()[0]

    assert row["vacancy_id"] == "hh:1"
    assert row["title"] == "Backend Engineer 1"
    assert row["employer_id"] == "hh:200"
    assert row["employer_name"] == "BetaCorp"
    assert row["salary_currency"] == "RUR"
    assert row["salary_disclosed"] is True
    assert row["city"] == "Москва"
    assert row["remote_type"] == "remote"
    assert row["source_url"] == "https://hh.ru/vacancy/1"
    assert row["source"] == "hh"


def test_api_remote_type_full_day_is_office(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _api_item(1, schedule={"id": "fullDay", "name": "Полный день"})
    _seed_api_lake(tmp_path, [(fetched, [item])])
    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "office"


def test_api_remote_type_unknown_schedule(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _api_item(1, schedule={"id": "weirdNewMode", "name": "?"})
    _seed_api_lake(tmp_path, [(fetched, [item])])
    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "unknown"


def test_api_remote_type_unknown_when_schedule_id_missing(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _api_item(1, schedule={})
    _seed_api_lake(tmp_path, [(fetched, [item])])
    assert build_slim_active(tmp_path).to_dicts()[0]["remote_type"] == "unknown"


def test_api_salary_disclosed_false_when_no_bounds(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _api_item(1, salary={"currency": "RUR", "from": None, "to": None, "gross": False})
    _seed_api_lake(tmp_path, [(fetched, [item])])
    row = build_slim_active(tmp_path).to_dicts()[0]
    assert row["salary_disclosed"] is False
    assert row["salary_currency"] == "RUR"


def test_mixed_lake_shards_and_api_both_mapped(tmp_path: Path):
    """Lake с обеими shapes (api transport + shards transport) → обе строки корректны."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(10, name="ShardsRole")])])
    _seed_api_lake(tmp_path, [(fetched, [_api_item(20, name="ApiRole")])])

    df = build_slim_active(tmp_path).sort("vacancy_id")
    rows = {r["vacancy_id"]: r for r in df.to_dicts()}

    assert rows["hh:10"]["title"] == "ShardsRole"
    assert rows["hh:10"]["employer_name"] == "ACME Inc."
    assert rows["hh:20"]["title"] == "ApiRole"
    assert rows["hh:20"]["employer_name"] == "BetaCorp"
    # обе строки имеют source_url, salary_currency, city — нет silent потери полей
    assert rows["hh:10"]["source_url"]
    assert rows["hh:20"]["source_url"]


def test_region_for_known_city(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, address={"city": "Казань"})])])
    assert build_slim_active(tmp_path).to_dicts()[0]["region"] == "ПФО"


def test_region_none_for_unknown_city(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    item = _shards_item(1, address={"city": "Зажопинск"}, area={"name": "Зажопинск"})
    _seed_lake(tmp_path, [(fetched, [item])])
    assert build_slim_active(tmp_path).to_dicts()[0]["region"] is None


def test_skills_extracted_from_title(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Python Developer (Django)")])])
    skills = build_slim_active(tmp_path).to_dicts()[0]["skills"]
    assert "Python" in skills
    assert "Django" in skills


def test_skills_extracted_from_description_fts(tmp_path: Path):
    """`description_fts` is dropped from slim schema as of v1.1, but the hh_details
    cache still provides it during build to drive skills extraction. The
    extracted skills should reach the slim row even though the fts text never
    reaches the parquet."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Backend Engineer")])])
    details = {"hh:1": ("Опыт PostgreSQL обязателен", "stack: PostgreSQL, Kafka, k8s")}
    row = build_slim_active(tmp_path, details=details).to_dicts()[0]
    assert set(row["skills"]) >= {"PostgreSQL", "Apache Kafka", "Kubernetes"}
    assert "description_fts" not in row


def test_build_slim_active_empty_details_cache_keeps_null_teaser(
    tmp_path: Path, monkeypatch
):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1)])])
    monkeypatch.setattr(
        "src.transform.slim_export.read_details_cache",
        lambda _path: pl.DataFrame(
            schema={
                "vacancy_id": pl.String,
                "description_teaser": pl.String,
                "description_fts": pl.String,
            }
        ),
    )

    row = build_slim_active(tmp_path).to_dicts()[0]

    assert row["description_teaser"] is None


def test_skills_empty_when_no_match(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Курьер")])])
    assert build_slim_active(tmp_path).to_dicts()[0]["skills"] == []


def test_seniority_parsed_from_title_shards(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Junior Data Analyst")])])
    assert build_slim_active(tmp_path).to_dicts()[0]["seniority"] == "junior"


def test_seniority_parsed_from_title_api(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_api_lake(tmp_path, [(fetched, [_api_item(1, name="Тимлид Backend")])])
    assert build_slim_active(tmp_path).to_dicts()[0]["seniority"] == "lead"


def test_seniority_unknown_when_no_keyword(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Data Analyst")])])
    assert build_slim_active(tmp_path).to_dicts()[0]["seniority"] == "unknown"


def test_seniority_uses_description_teaser(tmp_path: Path):
    """Если title нейтральный, seniority берётся из teaser."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, name="Data Engineer")])])
    details = {"hh:1": ("Ищем сеньора в команду платформы", None)}
    row = build_slim_active(tmp_path, details=details).to_dicts()[0]
    assert row["seniority"] == "senior"


def test_remote_fallback_fires_when_native_unknown(tmp_path: Path):
    """workFormats пуст → native='unknown' → fallback на teaser keyword."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path, [(fetched, [_shards_item(1, workFormats=[])])])
    details = {"hh:1": ("Полная удалёнка из любой точки", None)}
    row = build_slim_active(tmp_path, details=details).to_dicts()[0]
    assert row["remote_type"] == "remote"


def test_remote_native_wins_over_text_signal(tmp_path: Path):
    """Если hh уже отдал remote_type, текстовый fallback не перетирает."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(
        tmp_path,
        [(fetched, [_shards_item(1, workFormats=[{"workFormatsElement": ["ON_SITE"]}])])],
    )
    details = {"hh:1": ("Возможна полная удалёнка", None)}
    row = build_slim_active(tmp_path, details=details).to_dicts()[0]
    assert row["remote_type"] == "office"


def _slim_row(
    vid: str,
    source: str,
    *,
    title: str = "Senior Python Developer",
    employer: str | None = "Acme",
    city: str = "Москва",
    teaser: str | None = None,
) -> dict:
    return {
        "vacancy_id": vid,
        "title": title,
        "employer_id": f"{source}:1",
        "employer_name": employer,
        "salary_rub_min": None,
        "salary_rub_max": None,
        "salary_currency": None,
        "salary_disclosed": False,
        "city": city,
        "region": None,
        "remote_type": "unknown",
        "seniority": "senior",
        "description_teaser": teaser,
        "skills": [],
        "source": source,
        "market_scope": None,
        "professional_role_id": None,
        "source_url": None,
        "first_seen_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
        "last_seen_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
        "posted_at": datetime(2026, 4, 27, tzinfo=timezone.utc),
    }


def test_dedup_empty_frame_returns_empty():
    empty = pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)
    out, pairs = apply_cross_source_dedup(empty)
    assert out.is_empty()
    assert pairs == []


def test_dedup_drops_telegram_when_hh_twin_present():
    text = (
        "ищем сильного python разработчика опыт с fastapi django postgresql redis "
        "docker kubernetes ci cd микросервисы high load удалённая работа полная занятость"
    )
    rows = [
        _slim_row("hh:1", "hh", teaser=text),
        _slim_row("tg:ch:42", "telegram", teaser=text + " присоединяйтесь"),
        _slim_row("hh:2", "hh", title="Frontend", teaser="другой ролик React TypeScript"),
    ]
    df = pl.DataFrame(rows, schema=SLIM_ACTIVE_SCHEMA)

    out, pairs = apply_cross_source_dedup(df)
    out_ids = set(out["vacancy_id"].to_list())
    assert out_ids == {"hh:1", "hh:2"}
    assert len(pairs) == 1
    assert {pairs[0].source_a, pairs[0].source_b} == {"hh", "telegram"}


def test_dedup_no_pairs_keeps_all():
    rows = [
        _slim_row("hh:1", "hh", title="Backend", teaser="python fastapi"),
        _slim_row("tg:ch:1", "telegram", title="Designer", teaser="figma ui ux"),
    ]
    df = pl.DataFrame(rows, schema=SLIM_ACTIVE_SCHEMA)
    out, pairs = apply_cross_source_dedup(df)
    assert out.height == 2
    assert pairs == []


def test_dedup_does_not_collapse_same_source_rows():
    """hh × hh near-duplicates остаются — same-source skipped by dedup contract."""
    text = "python fastapi postgresql redis docker kubernetes удалённая senior"
    rows = [
        _slim_row("hh:1", "hh", teaser=text),
        _slim_row("hh:2", "hh", teaser=text),
    ]
    df = pl.DataFrame(rows, schema=SLIM_ACTIVE_SCHEMA)
    out, pairs = apply_cross_source_dedup(df)
    assert out.height == 2
    assert pairs == []


def test_write_slim_active_roundtrip(tmp_path: Path):
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    _seed_lake(tmp_path / "lake", [(fetched, [_shards_item(1), _shards_item(2)])])
    df = build_slim_active(tmp_path / "lake")

    out = write_slim_active(df, tmp_path / "derived" / "slim_active.parquet")

    assert out.exists()
    loaded = pl.read_parquet(out)
    assert loaded.height == 2
    assert dict(loaded.schema) == SLIM_ACTIVE_SCHEMA


def test_build_slim_active_tg_record_round_trip(tmp_path: Path):
    """End-to-end TG path: TGMessage → RawRecord → lake → slim row.

    Covers `_tg_to_slim_row` (lines 402-421 in slim_export.py): json.loads
    of raw_json, tg_parse extractors (salary/city/remote/seniority),
    `_extract_tg_title` first non-empty line, t.me source_url assembly,
    salary outlier clamping, region_for_city mapping.
    """
    from src.ingest.raw_lake import RawRecord, write_batch
    from src.ingest.tg_client import TGMessage

    posted = datetime(2026, 5, 17, 12, 30, tzinfo=timezone.utc)
    fetched = datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)
    msg = TGMessage(
        channel="data_jobs",
        message_id=4242,
        date=posted,
        text=(
            "Senior Data Engineer (Python, Spark)\n"
            "Москва, удалёнка возможна\n"
            "Зарплата: 300 000 — 450 000 ₽\n"
            "Стек: Python, Airflow, Kafka, Spark, ClickHouse"
        ),
        views=1234,
    )
    record = RawRecord.from_telegram_message(msg, fetched, market_scope="it")
    write_batch([record], tmp_path)

    df = build_slim_active(tmp_path)
    assert df.height == 1
    row = df.to_dicts()[0]

    assert row["vacancy_id"] == "tg:data_jobs:4242"
    assert row["source"] == "telegram"
    assert row["title"] == "Senior Data Engineer (Python, Spark)"
    assert row["source_url"] == "https://t.me/data_jobs/4242"
    assert row["employer_id"] is None
    assert row["employer_name"] is None
    assert row["city"] == "Москва"
    assert row["region"] is not None
    assert row["remote_type"] in {"remote", "hybrid", "office"}
    assert row["salary_rub_min"] == 300_000
    assert row["salary_rub_max"] == 450_000
    assert row["market_scope"] == "it"
    assert row["professional_role_id"] is None


def test_build_slim_active_tg_usd_salary_normalized_to_rub(tmp_path: Path):
    """Session 44: TG USD-сompensation должна конвертироваться в RUB
    через CBR rate ДО clamp'а, иначе $5000 underflow'ит RUB floor 10k.

    Pre-fix: TG path передавал raw USD numeric в `_clamp_salary_outliers`,
    значение 5000 < 10000 (RUB floor) → обнулялось. salary_disclosed=True,
    но обе границы None. Bug class: 4399 USD/EUR rows in audit.
    """
    from src.ingest.raw_lake import RawRecord, write_batch
    from src.ingest.tg_client import TGMessage

    posted = datetime(2026, 5, 17, 12, 30, tzinfo=timezone.utc)
    fetched = datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)
    msg = TGMessage(
        channel="data_jobs",
        message_id=4243,
        date=posted,
        text="Senior ML Engineer\n$5000-7000/month\nRelocation",
        views=100,
    )
    record = RawRecord.from_telegram_message(msg, fetched, market_scope="it")
    write_batch([record], tmp_path)

    # Explicit rates map для теста (избегает зависимости от cbr_rates.parquet)
    df = build_slim_active(tmp_path, rates={"RUR": 1.0, "RUB": 1.0, "USD": 70.0, "EUR": 80.0})
    assert df.height == 1
    row = df.to_dicts()[0]

    # 5000 USD * 70 = 350_000 RUB; 7000 USD * 70 = 490_000 RUB
    assert row["salary_currency"] == "USD"
    assert row["salary_disclosed"] is True
    assert row["salary_rub_min"] == 350_000
    assert row["salary_rub_max"] == 490_000


def test_build_slim_active_tg_disclosed_resets_when_clamp_nulls_both(tmp_path: Path):
    """Session 44: clamp removed both bounds → salary_disclosed=False.

    Pre-fix: parse_salary returned disclosed=True, clamp obnulled out-of-range
    values, но `salary_disclosed` оставался True. Inconsistent state: факт
    раскрытия зарплаты не подтверждается числом в slim. Audit нашёл
    5009 такого. После fix: disclosed = original_disclosed AND
    (min is not None OR max is not None).
    """
    from src.ingest.raw_lake import RawRecord, write_batch
    from src.ingest.tg_client import TGMessage

    posted = datetime(2026, 5, 17, 12, 30, tzinfo=timezone.utc)
    fetched = datetime(2026, 5, 18, 6, 0, tzinfo=timezone.utc)
    # RUB-вакансия с зарплатой ниже RUB floor 10k → parse_salary disclose=True,
    # clamp обнуляет.
    msg = TGMessage(
        channel="data_jobs",
        message_id=4244,
        date=posted,
        text="Стажёр-аналитик\nЗП: от 7 000 ₽\nГибрид",
        views=50,
    )
    record = RawRecord.from_telegram_message(msg, fetched, market_scope="it")
    write_batch([record], tmp_path)

    df = build_slim_active(tmp_path, rates={"RUR": 1.0, "RUB": 1.0, "USD": 70.0, "EUR": 80.0})
    assert df.height == 1
    row = df.to_dicts()[0]

    assert row["salary_rub_min"] is None
    assert row["salary_rub_max"] is None
    # Bug fix: disclosed reset to False because clamp removed both
    assert row["salary_disclosed"] is False


def test_extract_tg_title_falls_back_to_untitled_for_blank_text():
    """All-blank TG text → '(untitled)' placeholder, not crash.

    Covers `_extract_tg_title` line 451 (the `return "(untitled)"` fallback
    after the for-loop finds no non-empty line)."""
    from src.transform.slim_export import _extract_tg_title

    assert _extract_tg_title("") == "(untitled)"
    assert _extract_tg_title("\n   \n\t\n") == "(untitled)"


def test_extract_tg_title_truncates_long_first_line():
    """First non-empty line is the title, capped at 200 chars."""
    from src.transform.slim_export import _extract_tg_title

    long_line = "Senior Data Engineer " * 20  # ~420 chars
    title = _extract_tg_title(long_line + "\nrest of body\n")
    assert len(title) == 200
    assert title == long_line[:200]


def test_lake_has_column_non_hive_layout_fallback(tmp_path: Path):
    """Flat parquet layout (no `year=*/month=*` Hive partitioning) → fallback
    rglob branch in `_lake_has_column` (lines 485-489).

    Ad-hoc fixtures occasionally write flat parquets — the function must still
    detect column presence rather than returning False (which would force
    `build_slim_active` to skip the lake entirely on a market_scope query).
    """
    from src.transform.slim_export import _lake_has_column

    df = pl.DataFrame({"vacancy_id": ["hh:1"], "market_scope": ["it"]})
    flat_path = tmp_path / "flat.parquet"
    df.write_parquet(flat_path)

    assert _lake_has_column(tmp_path, "market_scope") is True
    assert _lake_has_column(tmp_path, "professional_role_id") is False


def test_lake_has_column_skips_empty_newer_hive_dirs(tmp_path: Path):
    from src.transform.slim_export import _lake_has_column

    (tmp_path / "year=2027").mkdir()
    (tmp_path / "year=2026" / "month=06").mkdir(parents=True)
    older_month = tmp_path / "year=2026" / "month=05"
    older_month.mkdir(parents=True)
    pl.DataFrame({"vacancy_id": ["hh:1"], "market_scope": ["it"]}).write_parquet(
        older_month / "batch.parquet"
    )

    assert _lake_has_column(tmp_path, "market_scope") is True
    assert _lake_has_column(tmp_path, "professional_role_id") is False


def test_lake_has_column_empty_lake_returns_false(tmp_path: Path):
    """No parquet files anywhere → False (covers `if not files: return False`
    in the non-Hive fallback)."""
    from src.transform.slim_export import _lake_has_column

    assert _lake_has_column(tmp_path, "market_scope") is False
