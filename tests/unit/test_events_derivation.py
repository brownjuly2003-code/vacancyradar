from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from src.transform.events_derivation import (
    EVENT_TYPES,
    _make_event,
    append_events,
    derive_events,
    event_counts_by_type,
)


def _row(vid: str, ch: str, *, employer="111", raw=None) -> dict:
    return {
        "vacancy_id": vid,
        "employer_id": employer,
        "content_hash": ch,
        "raw_json": raw or json.dumps({"id": vid}),
    }


def _df(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            schema={
                "vacancy_id": pl.String,
                "employer_id": pl.String,
                "content_hash": pl.String,
                "raw_json": pl.String,
            }
        )
    return pl.DataFrame(rows)


def test_empty_inputs_produce_empty_events():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    out = derive_events(_df([]), _df([]), ts)
    assert out.is_empty()


def test_appeared_when_only_in_current():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev = _df([_row("hh:1", "h1")])
    curr = _df([_row("hh:1", "h1"), _row("hh:2", "h2"), _row("hh:3", "h3")])

    events = derive_events(prev, curr, ts)

    appeared = events.filter(pl.col("type") == "appeared")
    assert sorted(appeared["vacancy_id"].to_list()) == ["hh:2", "hh:3"]


def test_closed_when_only_in_previous():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev = _df([_row("hh:1", "h1"), _row("hh:2", "h2")])
    curr = _df([_row("hh:2", "h2")])

    events = derive_events(prev, curr, ts)

    closed = events.filter(pl.col("type") == "closed")
    assert closed["vacancy_id"].to_list() == ["hh:1"]


def test_no_event_when_content_hash_unchanged():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev = _df([_row("hh:1", "h1")])
    curr = _df([_row("hh:1", "h1")])

    events = derive_events(prev, curr, ts)
    assert events.is_empty()


def test_salary_change_detected():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps({"id": "1", "salary": {"from": 100, "to": 200}}, sort_keys=True)
    curr_raw = json.dumps({"id": "1", "salary": {"from": 150, "to": 250}}, sort_keys=True)
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    assert events.height == 1
    e = events.to_dicts()[0]
    assert e["type"] == "salary_changed"
    payload = json.loads(e["payload"])
    assert payload["old"]["from"] == 100
    assert payload["new"]["from"] == 150


def test_salary_changed_detected_for_shards_compensation_shape():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps(
        {"id": "1", "compensation": {"from": 100, "to": 200, "currencyCode": "RUR"}},
        sort_keys=True,
    )
    curr_raw = json.dumps(
        {"id": "1", "compensation": {"from": 150, "to": 200, "currencyCode": "RUR"}},
        sort_keys=True,
    )
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    assert events.height == 1
    e = events.to_dicts()[0]
    assert e["type"] == "salary_changed"
    payload = json.loads(e["payload"])
    assert payload["old"]["from"] == 100
    assert payload["new"]["from"] == 150


def test_salary_unchanged_for_identical_compensation():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps(
        {
            "id": "1",
            "name": "Senior Data Analyst",
            "compensation": {"from": 100, "to": 200, "currencyCode": "RUR"},
        },
        sort_keys=True,
    )
    curr_raw = json.dumps(
        {
            "id": "1",
            "name": "Lead Data Analyst",
            "compensation": {"from": 100, "to": 200, "currencyCode": "RUR"},
        },
        sort_keys=True,
    )
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    e = events.to_dicts()[0]
    assert e["type"] == "desc_changed"


def test_desc_change_when_hash_differs_but_salary_same():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps({"id": "1", "salary": None, "name": "Senior Data Analyst"}, sort_keys=True)
    curr_raw = json.dumps({"id": "1", "salary": None, "name": "Lead Data Analyst"}, sort_keys=True)
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    e = events.to_dicts()[0]
    assert e["type"] == "desc_changed"


def test_desc_changed_has_null_payload():
    """desc_changed events carry no payload — only the type row is observable
    by /trends and weekly aggregates; the prev_hash/new_hash dict that used
    to land here was dead storage (no consumer read it) and grew ~12 MB/month.

    Dropped 2026-05-18, see commit body for the headcount.
    """
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps({"id": "1", "name": "Old"}, sort_keys=True)
    curr_raw = json.dumps({"id": "1", "name": "New"}, sort_keys=True)
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    e = events.to_dicts()[0]
    assert e["type"] == "desc_changed"
    assert e["payload"] is None


def test_salary_changed_still_carries_payload():
    """Regression guard: dropping desc_changed payload must not affect
    salary_changed, where the old/new salary dict IS the only persistent
    record of the change."""
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps(
        {"id": "1", "salary": {"from": 100, "to": 200, "currency": "RUR"}},
        sort_keys=True,
    )
    curr_raw = json.dumps(
        {"id": "1", "salary": {"from": 150, "to": 250, "currency": "RUR"}},
        sort_keys=True,
    )
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)
    e = events.to_dicts()[0]
    assert e["type"] == "salary_changed"
    assert e["payload"] is not None
    payload = json.loads(e["payload"])
    assert payload["old"]["from"] == 100
    assert payload["new"]["from"] == 150


def test_salary_changed_from_missing_salary_records_null_old_payload():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev_raw = json.dumps({"id": "1", "name": "Old"}, sort_keys=True)
    curr_raw = json.dumps(
        {"id": "1", "salary": {"from": 150, "to": 250, "currency": "RUR"}},
        sort_keys=True,
    )
    prev = _df([_row("hh:1", "h1", raw=prev_raw)])
    curr = _df([_row("hh:1", "h2", raw=curr_raw)])

    events = derive_events(prev, curr, ts)

    e = events.to_dicts()[0]
    assert e["type"] == "salary_changed"
    payload = json.loads(e["payload"])
    assert payload["old"] is None
    assert payload["new"]["from"] == 150


def test_combined_diff_emits_three_event_types_at_once():
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev = _df(
        [
            _row("hh:1", "old1", raw=json.dumps({"id": "1", "salary": {"from": 100}}, sort_keys=True)),
            _row("hh:closed", "x"),
        ]
    )
    curr = _df(
        [
            _row("hh:1", "new1", raw=json.dumps({"id": "1", "salary": {"from": 200}}, sort_keys=True)),
            _row("hh:fresh", "f"),
        ]
    )

    events = derive_events(prev, curr, ts)

    types = sorted(events["type"].to_list())
    assert types == ["appeared", "closed", "salary_changed"]


def test_event_types_constant_matches_classifier():
    assert EVENT_TYPES >= {"appeared", "closed", "desc_changed", "salary_changed"}


def test_make_event_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown event type"):
        _make_event("bogus", "hh:1", None, datetime(2026, 4, 26, tzinfo=timezone.utc))


def test_append_and_count_via_duckdb(tmp_path: Path):
    ts = datetime(2026, 4, 26, tzinfo=timezone.utc)
    prev = _df([])
    curr = _df([_row("hh:1", "h"), _row("hh:2", "h2")])
    events = derive_events(prev, curr, ts)

    db_path = tmp_path / "events.duckdb"
    appended = append_events(events, db_path)

    assert appended == 2
    counts = event_counts_by_type(db_path)
    assert counts == {"appeared": 2}


def test_append_events_idempotent_for_empty_df(tmp_path: Path):
    db_path = tmp_path / "events.duckdb"
    n = append_events(_df([]).rename({}).cast({}), db_path) if False else 0  # noqa: F841

    # Direct call with empty DataFrame
    from src.transform.events_derivation import _events_schema  # type: ignore
    empty = pl.DataFrame(schema=_events_schema())
    assert append_events(empty, db_path) == 0
    assert not db_path.exists()


def test_event_counts_by_type_returns_empty_for_missing_db(tmp_path: Path):
    assert event_counts_by_type(tmp_path / "missing.duckdb") == {}


def test_source_propagates_to_all_event_rows():
    # Pre-2026-05-17: _ingest_telegram never called derive_events, and
    # _make_event hardcoded source="hh", so any future TG caller would have
    # silently mis-tagged its events. Now the source kwarg threads through
    # appeared/closed/desc_changed/salary_changed alike.
    ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
    prev = _df([_row("tg:ch:1", "h1"), _row("tg:ch:2", "h2")])
    curr = _df([
        _row("tg:ch:1", "h1-changed"),  # → desc_changed
        _row("tg:ch:3", "h3"),  # → appeared
    ])

    events = derive_events(prev, curr, ts, source="tg")

    assert events.height == 3  # appeared + closed + desc_changed
    sources = set(events["source"].to_list())
    assert sources == {"tg"}


def test_source_default_remains_hh_for_backwards_compat():
    ts = datetime(2026, 5, 17, tzinfo=timezone.utc)
    curr = _df([_row("hh:1", "h1")])

    events = derive_events(_df([]), curr, ts)

    assert events.height == 1
    assert events["source"].to_list() == ["hh"]
