from __future__ import annotations

from argparse import Namespace

import polars as pl

import src.cli_modules.enrich as enrich_mod
from src.cli import _enrich_hh_details


def test_enrich_dispatches_known_kinds(monkeypatch):
    calls = []

    def fake_hh_details(args):
        calls.append(("hh-details", args))
        return 11

    def fake_embeddings(args):
        calls.append(("embeddings", args))
        return 12

    monkeypatch.setattr(enrich_mod, "_enrich_hh_details", fake_hh_details)
    monkeypatch.setattr(enrich_mod, "_enrich_embeddings", fake_embeddings)

    hh_args = Namespace(kind="hh-details")
    embeddings_args = Namespace(kind="embeddings")

    assert enrich_mod._enrich(hh_args) == 11
    assert enrich_mod._enrich(embeddings_args) == 12
    assert calls == [("hh-details", hh_args), ("embeddings", embeddings_args)]


def test_enrich_hh_details_reads_only_vacancy_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = {}

    def fake_read_lake(lake, source=None, *, columns=None):
        calls["read_lake"] = {"lake": lake, "source": source, "columns": columns}
        assert columns == ["vacancy_id"]
        return pl.DataFrame({"vacancy_id": ["hh:1", "hh:2"]})

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        calls["fetch"] = {"ids": ids, "cache": cache, "rate_limit_sec": rate_limit_sec}
        return 0

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_missing_details)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", tmp_path / "cache.parquet")

    assert _enrich_hh_details(Namespace(limit=2, rate=0.25, scope=None)) == 0

    assert calls["read_lake"]["source"] == "hh"
    assert set(calls["fetch"]["ids"]) == {"hh:1", "hh:2"}
    assert calls["fetch"]["rate_limit_sec"] == 0.25


def test_enrich_hh_details_prints_detail_run_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_read_lake(lake, source=None, *, columns=None):
        return pl.DataFrame({"vacancy_id": ["hh:1", "hh:2", "hh:3"]})

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        assert set(ids) == {"hh:1", "hh:2", "hh:3"}
        return 2

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_missing_details)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", tmp_path / "cache.parquet")

    assert _enrich_hh_details(Namespace(limit=None, rate=0.0, scope=None)) == 0

    assert (
        "[enrich] detail_summary attempted=3 fetched=2 failed=1 failure_rate=0.333"
        in capsys.readouterr().out
    )


def test_enrich_hh_details_scope_filters_to_market_scope(tmp_path, monkeypatch):
    """--scope it limits enrichment to vacancies labeled with that market_scope,
    so a full enrich does not fan out across legacy full-market rows.
    """
    monkeypatch.chdir(tmp_path)
    calls = {}

    def fake_read_lake(lake, source=None, *, columns=None):
        calls["read_lake"] = {"lake": lake, "source": source, "columns": columns}
        assert columns == ["vacancy_id", "market_scope"]
        return pl.DataFrame(
            {
                "vacancy_id": ["hh:1", "hh:2", "hh:legacy"],
                "market_scope": ["it", "it", None],
            }
        )

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        calls["fetch"] = {"ids": ids}
        return 0

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_missing_details)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", tmp_path / "cache.parquet")

    assert _enrich_hh_details(Namespace(limit=None, rate=1.0, scope="it")) == 0

    assert set(calls["fetch"]["ids"]) == {"hh:1", "hh:2"}
    assert "hh:legacy" not in calls["fetch"]["ids"]


def test_enrich_hh_details_scope_with_no_tagged_rows_returns_3(tmp_path, monkeypatch, capsys):
    """Empty scoped subset surfaces a clear error rather than no-oping."""
    monkeypatch.chdir(tmp_path)

    def fake_read_lake(lake, source=None, *, columns=None):
        return pl.DataFrame(
            {"vacancy_id": ["hh:1"], "market_scope": [None]},
            schema={"vacancy_id": pl.String, "market_scope": pl.String},
        )

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", tmp_path / "cache.parquet")

    assert _enrich_hh_details(Namespace(limit=None, rate=1.0, scope="it")) == 3
    assert "no hh rows tagged market_scope=it" in capsys.readouterr().err


def test_enrich_hh_details_empty_lake_returns_3(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_read_lake(lake, source=None, *, columns=None):
        return pl.DataFrame(schema={"vacancy_id": pl.String})

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", tmp_path / "cache.parquet")

    assert _enrich_hh_details(Namespace(limit=None, rate=1.0, scope=None)) == 3
    assert "empty lake" in capsys.readouterr().err


def test_enrich_hh_details_filters_cached_before_limit(tmp_path, monkeypatch):
    """Cached vacancy_ids dropped before --limit slice — limit budget must spend
    on non-cached ids, not on cache-hits that fetch_missing_details would skip.
    """
    monkeypatch.chdir(tmp_path)
    cache_path = tmp_path / "hh_details.parquet"
    pl.DataFrame({"vacancy_id": ["hh:1", "hh:2"]}).write_parquet(cache_path)
    calls = {}

    def fake_read_lake(lake, source=None, *, columns=None):
        return pl.DataFrame({"vacancy_id": ["hh:1", "hh:2", "hh:3", "hh:4"]})

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        calls["ids"] = list(ids)
        return 0

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_missing_details)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", cache_path)

    assert _enrich_hh_details(Namespace(limit=2, rate=0.0, scope=None)) == 0
    # Only 2 non-cached survive after filter; limit slice keeps both.
    assert set(calls["ids"]) == {"hh:3", "hh:4"}


def test_enrich_hh_details_slim_without_unknown_keeps_all_ids_unprioritized(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    cache_path = tmp_path / "hh_details.parquet"
    derived = tmp_path / "derived"
    derived.mkdir()
    pl.DataFrame(
        {
            "vacancy_id": ["hh:1", "hh:2"],
            "source": ["hh", "hh"],
            "seniority": ["middle", "senior"],
        }
    ).write_parquet(derived / "slim_active.parquet")

    def fake_read_lake(lake, source=None, *, columns=None):
        return pl.DataFrame({"vacancy_id": ["hh:1", "hh:2", "hh:3"]})

    captured = {}

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        captured["ids"] = list(ids)
        return 0

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_missing_details)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", cache_path)

    assert _enrich_hh_details(Namespace(limit=None, rate=0.0, scope=None)) == 0
    assert set(captured["ids"]) == {"hh:1", "hh:2", "hh:3"}
    assert "unknown_priority" not in capsys.readouterr().out


def test_enrich_hh_details_prioritizes_unknown_seniority(tmp_path, monkeypatch, capsys):
    """HH-unknown ids in slim_active.parquet jump to front of queue so --limit
    budget hits the unknown-seniority backlog first, not arbitrary lake order.
    """
    monkeypatch.chdir(tmp_path)
    cache_path = tmp_path / "hh_details.parquet"
    derived = tmp_path / "derived"
    derived.mkdir()
    pl.DataFrame(
        {
            "vacancy_id": ["hh:knownA", "hh:unknownB"],
            "source": ["hh", "hh"],
            "seniority": ["senior", "unknown"],
        }
    ).write_parquet(derived / "slim_active.parquet")

    def fake_read_lake(lake, source=None, *, columns=None):
        # Lake order intentionally puts known before unknown — priority must flip.
        return pl.DataFrame({"vacancy_id": ["hh:knownA", "hh:unknownB", "hh:other"]})

    def fake_fetch_missing_details(ids, cache, *, rate_limit_sec):
        # Capture exact ORDER, not just membership.
        return 0

    captured = {}

    def fake_fetch_capture(ids, cache, *, rate_limit_sec):
        captured["ids"] = list(ids)
        return 0

    monkeypatch.setattr("src.ingest.raw_lake.read_lake", fake_read_lake)
    monkeypatch.setattr("src.ingest.hh_detail.fetch_missing_details", fake_fetch_capture)
    monkeypatch.setattr("src.ingest.hh_detail.HH_DETAILS_PATH_DEFAULT", cache_path)

    assert _enrich_hh_details(Namespace(limit=2, rate=0.0, scope=None)) == 0
    assert captured["ids"][0] == "hh:unknownB"
    assert "unknown_priority=1" in capsys.readouterr().out
