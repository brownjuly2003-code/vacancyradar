"""`ingest hh --full-sweep` — segmented complete sweep (plan 2026-06-05).

hh shards serves at most a fixed result window per query (live 2026-06-05:
2000 items regardless of page size). The full sweep splits window-capped
roles by `experience` (exact partition), then by area (Moscow / SPb / rest of
Russia). These tests simulate the window with WINDOW=4 items at per_page=2.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import duckdb

from src.cli import _ingest_hh
from src.ingest.hh_shards import HHTransientError

WINDOW = 4
PER_PAGE = 2


def _args(*, detect_closed: bool = False, scope: str | None = "it") -> Namespace:
    return Namespace(
        transport="shards",
        dry=False,
        scope=scope,
        area=113,
        per_page=PER_PAGE,
        pages=1,
        page_start=1,
        overlap_pages=0,
        detect_closed=detect_closed,
        full_sweep=True,
    )


def _vacancy(vid: int) -> dict:
    return {
        "vacancyId": vid,
        "name": f"Vacancy {vid}",
        "company": {"id": f"emp-{vid}"},
        "compensation": {
            "from": 100,
            "to": 200,
            "currencyCode": "RUR",
            "mode": "MONTH",
        },
    }


def _write_it_scope_config(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "config.yaml").write_text(
        "market:\n"
        "  live_scope: it\n"
        "  scopes:\n"
        "    it:\n"
        "      hh:\n"
        "        category_id: 11\n"
        "        category_name: Информационные технологии\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "professional_roles.yaml").write_text(
        "categories:\n"
        "  - id: '11'\n"
        "    name: Информационные технологии\n"
        "    roles:\n"
        "      - id: '156'\n"
        "        name: BI-аналитик\n"
        "      - id: '160'\n"
        "        name: DevOps-инженер\n",
        encoding="utf-8",
    )
    # Minimal areas tree for the level-3 area partition: Moscow/SPb are
    # subjects themselves; "rest" = the two oblast ids.
    (tmp_path / "data" / "areas.yaml").write_text(
        "- id: '113'\n"
        "  parent_id: null\n"
        "  name: Россия\n"
        "  areas:\n"
        "  - {id: '1', parent_id: '113', name: Москва, areas: []}\n"
        "  - {id: '2', parent_id: '113', name: Санкт-Петербург, areas: []}\n"
        "  - {id: '1620', parent_id: '113', name: Марий Эл, areas: []}\n"
        "  - {id: '1530', parent_id: '113', name: Ростовская область, areas: []}\n",
        encoding="utf-8",
    )


def _route_key(kwargs: dict) -> tuple:
    area = kwargs.get("area")
    area_key = tuple(area) if isinstance(area, list) else area
    return (kwargs.get("professional_role"), kwargs.get("experience"), area_key)


def _install_routed_shards(monkeypatch, routes_by_run: list[dict], calls: list[dict]) -> None:
    """Fake HHShardsClient that answers per (role, experience, area) route.

    Route value: list of vacancy ids (only the first WINDOW are reachable —
    totalResults reports the full length, lastPage caps at the window, like
    the real shards endpoint) or the string "transient".
    """

    class RoutedFakeClient:
        def __init__(self, _cfg):
            self._routes = routes_by_run.pop(0)

        def iter_pages(self, *, start_page: int = 0, max_pages: int | None = None, **kwargs):
            calls.append(dict(kwargs))
            spec = self._routes[_route_key(kwargs)]
            if spec == "transient":
                raise HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")
            ids: list[int] = spec
            total = len(ids)
            per_page = int(kwargs["per_page"])
            visible = ids[:WINDOW]
            last = ((min(total, WINDOW) + per_page - 1) // per_page - 1) if total else None
            page = start_page
            yielded = 0
            while True:
                chunk = visible[page * per_page : (page + 1) * per_page]
                yield {
                    "vacancySearchResult": {
                        "vacancies": [_vacancy(v) for v in chunk],
                        "totalResults": total,
                        "paging": {
                            "lastPage": ({"page": last} if last is not None else None)
                        },
                    }
                }
                yielded += 1
                page += 1
                if max_pages is not None and yielded >= max_pages:
                    return
                if last is None or page > last:
                    return

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", RoutedFakeClient)


def _event_counts(db_path: Path) -> dict[str, int]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()
        return {event_type: count for event_type, count in rows}
    finally:
        con.close()


def test_full_sweep_segments_oversized_role_and_emits_closed(tmp_path, monkeypatch):
    """Window-capped role → experience split → area split; closed on a full sweep.

    Run 1 seeds the scoped snapshot {1..5, 99}; run 2 sweeps {1..9} through
    segmentation (99 disappears) → exactly one closed event.
    """
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    calls: list[dict] = []
    run1 = {
        (156, None, 113): [1, 2, 3],
        (160, None, 113): [4, 5, 99],
    }
    run2 = {
        # role 156: total 10 > window 4 → experience partition
        (156, None, 113): list(range(1, 11)),
        (156, "noExperience", 113): [1, 2],
        # 6 > window → area partition
        (156, "between1And3", 113): [3, 4, 5, 6, 30, 31],
        (156, "between1And3", 1): [3, 4],
        (156, "between1And3", 2): [5],
        (156, "between1And3", (1620, 1530)): [6, 30, 31],
        (156, "between3And6", 113): [7],
        (156, "moreThan6", 113): [],
        # role 160: fits (3 ≤ 4) — plain drain, 2 pages
        (160, None, 113): [8, 9],
    }
    _install_routed_shards(monkeypatch, [run1, run2], calls)

    assert _ingest_hh(_args(detect_closed=True)) == 0
    calls.clear()
    assert _ingest_hh(_args(detect_closed=True)) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["closed"] == 1  # only 99; ids 1..9 all seen by run 2
    # appeared run1: 6 ids; appeared run2: 6,30,31,7,8? — 8,9 появились? 8,9
    # отсутствовали в run1? run1 had 4,5,99 on role 160 and 1,2,3 on 156 →
    # run2 new ids: 6,30,31,7,8,9 → 6 appeared + run1 6 appeared = 12.
    assert counts["appeared"] == 12
    # the rest-of-Russia leaf was queried as one multi-area request
    rest_calls = [c for c in calls if isinstance(c.get("area"), list)]
    assert rest_calls and rest_calls[0]["area"] == [1620, 1530]
    assert rest_calls[0]["experience"] == "between1And3"
    # full sweep completed → state file written
    assert (tmp_path / "master" / "run_state" / "hh_completed_sweeps.json").exists()


def test_full_sweep_uncovered_leaf_keeps_records_skips_closed(tmp_path, monkeypatch, capsys):
    """Leaf still window-capped after max segmentation: keep data, skip closed.

    Discarding a 95%-complete sweep over one capped leaf would trade a fresh
    corpus for a stale dashboard; only the closed emission is unsafe on a
    partial view. Run 1 seeds {1,2,99}; run 2 never sees 99 but is incomplete
    → no closed event, records written, exit 0, loud warn.
    """
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    calls: list[dict] = []
    run1 = {
        (156, None, 113): [1, 2, 99],
        (160, None, 113): [],
    }
    run2 = {
        (156, None, 113): list(range(1, 11)),  # oversized
        (156, "noExperience", 113): [1, 2, 3, 4, 5, 6],  # oversized leaf
        (156, "noExperience", 1): [1, 2, 3, 4, 5, 6],  # Moscow STILL capped
        (156, "noExperience", 2): [],
        (156, "noExperience", (1620, 1530)): [],
        (156, "between1And3", 113): [],
        (156, "between3And6", 113): [],
        (156, "moreThan6", 113): [],
        (160, None, 113): [8],
    }
    _install_routed_shards(monkeypatch, [run1, run2], calls)

    assert _ingest_hh(_args(detect_closed=True)) == 0
    assert _ingest_hh(_args(detect_closed=True)) == 0

    err = capsys.readouterr().err
    assert "uncovered hh leaf" in err
    assert "full sweep incomplete" in err
    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert "closed" not in counts  # 99 unseen, but closed is skipped
    # collected records ARE written despite the incomplete sweep
    assert (tmp_path / "master" / "vacancies_raw.parquet").exists()
    # incomplete sweep → completed-sweep state for run 2 must not be recorded
    # (run 1 was complete and wrote it; its fetched_at differs)


def test_full_sweep_transient_with_detect_closed_keeps_records(tmp_path, monkeypatch, capsys):
    """Transient hole + --detect-closed: records kept, closed skipped, exit 0."""
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    calls: list[dict] = []
    run = {
        (156, None, 113): "transient",
        (160, None, 113): [8, 9],
    }
    _install_routed_shards(monkeypatch, [run], calls)

    assert _ingest_hh(_args(detect_closed=True)) == 0

    err = capsys.readouterr().err
    assert "transient error" in err
    assert "full sweep incomplete" in err
    assert (tmp_path / "master" / "vacancies_raw.parquet").exists()
    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["appeared"] == 2  # role 160 collected


def test_full_sweep_transient_segment_keeps_rest_and_blocks_state(tmp_path, monkeypatch, capsys):
    """Transient на одном segment: роль incomplete, остальное собрано, exit 0."""
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    calls: list[dict] = []
    run = {
        (156, None, 113): list(range(1, 11)),  # oversized → split
        (156, "noExperience", 113): "transient",
        (156, "between1And3", 113): [3, 4],
        (156, "between3And6", 113): [5],
        (156, "moreThan6", 113): [],
        (160, None, 113): [8, 9],
    }
    _install_routed_shards(monkeypatch, [run], calls)

    assert _ingest_hh(_args(detect_closed=False)) == 0

    err = capsys.readouterr().err
    assert "transient error" in err
    # collected segments are written (no detect-closed deferral)
    assert (tmp_path / "master" / "vacancies_raw.parquet").exists()
    # incomplete sweep → completed-sweep state NOT written
    assert not (tmp_path / "master" / "run_state" / "hh_completed_sweeps.json").exists()


def test_full_sweep_bisects_oversized_rest_areas(tmp_path, monkeypatch):
    """rest-of-RF bucket over the window → recursive area bisection.

    Live 2026-06-05: role=121 noExperience rest[86] overflowed the fixed
    3-bucket partition; the list must split until every leaf fits.
    """
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    calls: list[dict] = []
    run = {
        (156, None, 113): list(range(1, 11)),  # oversized → exp split
        (156, "noExperience", 113): [21, 22, 23, 24, 25, 26],  # oversized leaf
        (156, "noExperience", 1): [21],
        (156, "noExperience", 2): [],
        (156, "noExperience", (1620, 1530)): [22, 23, 24, 25, 26],  # 5 > 4 → bisect
        (156, "noExperience", 1620): [22, 23],
        (156, "noExperience", 1530): [24, 25, 26],
        (156, "between1And3", 113): [],
        (156, "between3And6", 113): [],
        (156, "moreThan6", 113): [],
        (160, None, 113): [30],
    }
    _install_routed_shards(monkeypatch, [run], calls)

    assert _ingest_hh(_args(detect_closed=False)) == 0

    # bisected leaves drained as single-area scalar queries
    single_area_calls = {c.get("area") for c in calls if not isinstance(c.get("area"), list)}
    assert {1620, 1530} <= single_area_calls
    # complete sweep despite the bisection → state written
    assert (tmp_path / "master" / "run_state" / "hh_completed_sweeps.json").exists()


def test_full_sweep_requires_shards_and_page_start_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    args = _args()
    args.transport = "api"
    assert _ingest_hh(args) == 2
    assert "--full-sweep requires --transport shards" in capsys.readouterr().err

    args = _args()
    args.page_start = 2
    args.pages = 5
    assert _ingest_hh(args) == 2
    assert "--full-sweep requires --page-start 1" in capsys.readouterr().err
