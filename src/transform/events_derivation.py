from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import duckdb
import polars as pl


EVENT_TYPES = {"appeared", "seen", "closed", "salary_changed", "desc_changed", "republished"}


def derive_events(
    previous: pl.DataFrame,
    current: pl.DataFrame,
    ts: datetime,
    *,
    closed_grace_days: int = 0,
    source: str = "hh",
) -> pl.DataFrame:
    """Diff two snapshots → events DataFrame.

    `previous` and `current` must have columns (vacancy_id, employer_id,
    content_hash, raw_json). Both can be empty.

    `source` propagates into every emitted event row (default "hh" for
    backwards compat with existing HH callers). TG ingest passes
    `source="tg"` so `market_pulse` can count both feeds; pre-2026-05-17
    `_ingest_telegram` skipped this step entirely, leaving the chart
    HH-only — see commit body.

    Emits:
      - appeared: vacancy_id in current but not previous
      - closed: vacancy_id in previous but not current
      - desc_changed | salary_changed: vacancy_id in both, content_hash differs
      - republished: appeared again after a 'closed' (handled by caller via
        cross-day correlation; not detected here)
    """
    schema = _events_schema()
    if current.is_empty() and previous.is_empty():
        return pl.DataFrame(schema=schema)

    prev_ids = set(previous["vacancy_id"].to_list()) if not previous.is_empty() else set()
    curr_ids = set(current["vacancy_id"].to_list()) if not current.is_empty() else set()

    appeared_ids = curr_ids - prev_ids
    closed_ids = prev_ids - curr_ids
    common_ids = prev_ids & curr_ids

    events: list[dict] = []

    if appeared_ids and not current.is_empty():
        appeared_rows = current.filter(pl.col("vacancy_id").is_in(list(appeared_ids)))
        for r in appeared_rows.iter_rows(named=True):
            events.append(
                _make_event("appeared", r["vacancy_id"], r.get("employer_id"), ts, source=source)
            )

    if closed_ids and not previous.is_empty():
        closed_rows = previous.filter(pl.col("vacancy_id").is_in(list(closed_ids)))
        for r in closed_rows.iter_rows(named=True):
            events.append(
                _make_event("closed", r["vacancy_id"], r.get("employer_id"), ts, source=source)
            )

    if common_ids:
        prev_idx = {r["vacancy_id"]: r for r in previous.iter_rows(named=True)}
        curr_idx = {r["vacancy_id"]: r for r in current.iter_rows(named=True)}
        for vid in common_ids:
            prev_row = prev_idx[vid]
            curr_row = curr_idx[vid]
            if prev_row["content_hash"] == curr_row["content_hash"]:
                continue
            event_type, payload = _classify_change(prev_row, curr_row)
            events.append(
                _make_event(
                    event_type,
                    vid,
                    curr_row.get("employer_id"),
                    ts,
                    payload=payload,
                    source=source,
                )
            )

    if not events:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(events, schema=schema).sort("ts", "type", "vacancy_id")


def _classify_change(prev: dict, curr: dict) -> tuple[str, dict | None]:
    prev_payload = json.loads(prev["raw_json"]) if prev.get("raw_json") else {}
    curr_payload = json.loads(curr["raw_json"]) if curr.get("raw_json") else {}
    prev_salary = _extract_salary_payload(prev_payload)
    curr_salary = _extract_salary_payload(curr_payload)
    if prev_salary != curr_salary:
        return "salary_changed", {
            "old": _salary_summary(prev_payload),
            "new": _salary_summary(curr_payload),
        }
    # desc_changed payload was {"prev_hash": ..., "new_hash": ...} until
    # 2026-05-18 (session 19). At 430k events that was ~28 MB of dead JSON in
    # events.duckdb — no consumer reads it (web routes use aggregates, weekly
    # builders don't filter by event type, Quarto reports skip the column).
    # The `type='desc_changed'` row itself is what /trends counts; the hash
    # diff is already captured by content_hash in the raw lake. Drop the
    # payload to save ~12 MB/month of DB growth.
    return "desc_changed", None


def _extract_salary_payload(payload: dict) -> tuple | None:
    if isinstance(payload.get("compensation"), dict):
        comp = payload["compensation"]
        return (comp.get("from"), comp.get("to"), comp.get("currencyCode"), comp.get("mode"))
    if isinstance(payload.get("salary"), dict):
        sal = payload["salary"]
        return (sal.get("from"), sal.get("to"), sal.get("currency"), sal.get("gross"))
    return None


def _salary_summary(payload: dict) -> dict | None:
    if isinstance(payload.get("compensation"), dict):
        return payload["compensation"]
    if isinstance(payload.get("salary"), dict):
        return payload["salary"]
    return None


def _make_event(
    event_type: str,
    vacancy_id: str,
    employer_id: str | None,
    ts: datetime,
    *,
    payload: dict | None = None,
    source: str = "hh",
) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type}")
    return {
        "event_id": str(uuid.uuid4()),
        "vacancy_id": vacancy_id,
        "employer_id": employer_id,
        "ts": ts,
        "type": event_type,
        "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else None,
        "source": source,
    }


def _events_schema() -> dict:
    return {
        "event_id": pl.String,
        "vacancy_id": pl.String,
        "employer_id": pl.String,
        "ts": pl.Datetime,
        "type": pl.String,
        "payload": pl.String,
        "source": pl.String,
    }


def append_events(events: pl.DataFrame, db_path: Path) -> int:
    """Append events DataFrame to master/events.duckdb. Returns row count appended."""
    if events.is_empty():
        return 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id    VARCHAR PRIMARY KEY,
                vacancy_id  VARCHAR NOT NULL,
                employer_id VARCHAR,
                ts          TIMESTAMP NOT NULL,
                type        VARCHAR NOT NULL,
                payload     VARCHAR,
                source      VARCHAR NOT NULL
            )
            """
        )
        con.register("events_in", events.to_arrow())
        con.execute("INSERT INTO events SELECT * FROM events_in")
        con.unregister("events_in")
    finally:
        con.close()
    return len(events)


def event_counts_by_type(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()
        return {t: c for t, c in rows}
    finally:
        con.close()
