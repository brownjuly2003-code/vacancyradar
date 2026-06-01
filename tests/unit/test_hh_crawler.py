from __future__ import annotations

import json
import shutil
import signal
import time
import uuid
from pathlib import Path

import polars as pl
import pytest

from src.ingest.hh_crawler import (
    PERIODS,
    SCHEDULES,
    Segment,
    _append_unique,
    _drain_segment,
    _format_elapsed,
    _load_split_dimensions,
    _maybe_log,
    _next_dimension,
    _save_progress_atomic,
    crawl,
)
from src.ingest.hh_shards import HHTransientError, RateLimited


@pytest.fixture
def workspace_tmp():
    path = Path("master") / f"test_hh_crawler_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        if path.exists():
            shutil.rmtree(path)


class FakeClient:
    def __init__(self, totals: dict[tuple, int], pages: dict[tuple, list[list[dict]]] | None = None):
        self.totals = totals
        self.pages = pages or {}
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        key = _key(kwargs)
        page = kwargs.get("page", 0)
        page_sets = self.pages.get(key, [[]])
        vacancies = page_sets[page] if page < len(page_sets) else []
        last_page = max(len(page_sets) - 1, 0)
        return {
            "vacancySearchResult": {
                "vacancies": vacancies,
                "totalResults": self.totals.get(key, len(vacancies)),
                "paging": {"lastPage": {"page": last_page}},
            }
        }


def _key(params: dict) -> tuple:
    return (
        params.get("area"),
        params.get("professional_role"),
        params.get("period"),
        params.get("schedule"),
    )


def _vacancy(i: int) -> dict:
    return {"vacancyId": i, "name": f"Vacancy {i}", "company": {"id": 10}}


def _write_refdata(tmp_path: Path, monkeypatch) -> None:
    roles = {"categories": [{"id": "1", "name": "IT", "roles": [{"id": str(i), "name": str(i)} for i in range(1, 4)]}]}
    areas = [{"id": "113", "name": "Russia", "areas": [{"id": str(i), "name": f"District {i}", "areas": []} for i in range(1, 9)]}]
    roles_path = tmp_path / "roles.yaml"
    areas_path = tmp_path / "areas.yaml"
    import yaml

    roles_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    areas_path.write_text(yaml.safe_dump(areas), encoding="utf-8")
    monkeypatch.setattr("src.ingest.hh_crawler.ROLES_PATH", roles_path)
    monkeypatch.setattr("src.ingest.hh_crawler.AREAS_PATH", areas_path)


def test_under_cap_no_split(workspace_tmp: Path, monkeypatch):
    _write_refdata(workspace_tmp, monkeypatch)
    key = (1, None, None, None)
    client = FakeClient(
        {key: 500},
        {key: [[_vacancy(i) for i in range(100)], [_vacancy(i) for i in range(100, 200)]]},
    )

    stats = crawl(
        Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert stats["stats"]["vacancies_fetched"] == 200
    assert stats["stats"]["segments_done"] == 1
    assert [c["page"] for c in client.calls] == [0, 1]


def test_crawl_sigint_handler_saves_progress_and_restores_previous(
    workspace_tmp: Path, monkeypatch
):
    _write_refdata(workspace_tmp, monkeypatch)
    progress_path = workspace_tmp / "progress.json"
    previous_handler = signal.getsignal(signal.SIGINT)

    def fake_crawl_segment(*_args, **_kwargs):
        handler = signal.getsignal(signal.SIGINT)
        assert handler != previous_handler
        handler(signal.SIGINT, None)

    monkeypatch.setattr("src.ingest.hh_crawler._crawl_segment", fake_crawl_segment)

    with pytest.raises(KeyboardInterrupt):
        crawl(
            Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
            max_depth=4,
            max_vacancies=2_000_000,
            rate_limit_sec=0,
            progress_path=progress_path,
            lake_root=workspace_tmp / "lake",
            client=FakeClient({}),
        )

    assert signal.getsignal(signal.SIGINT) == previous_handler
    progress = json.loads(progress_path.read_text(encoding="utf-8"))
    assert progress["stats"]["last_update"]


def test_over_cap_splits_by_area(workspace_tmp: Path, monkeypatch):
    _write_refdata(workspace_tmp, monkeypatch)
    totals = {(113, None, None, None): 20_000}
    for area in range(1, 9):
        totals[(area, None, None, None)] = 0
    client = FakeClient(totals)

    crawl(
        Segment(area=113, professional_role=None, period=None, schedule=None, depth=0),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    requested_areas = [c["area"] for c in client.calls[1:]]
    assert requested_areas == list(range(1, 9))


def test_over_cap_max_depth_writes_uncovered(workspace_tmp: Path, monkeypatch):
    _write_refdata(workspace_tmp, monkeypatch)
    segment = Segment(area=1, professional_role=1, period=180, schedule="fullDay", depth=4)
    client = FakeClient({(1, 1, 180, "fullDay"): 20_000})

    progress = crawl(
        segment,
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert progress["uncovered"] == [{**segment.to_dict(), "total": 20_000}]
    assert progress["stats"]["segments_done"] == 0


def test_resume_skips_closed_segments(workspace_tmp: Path, monkeypatch):
    _write_refdata(workspace_tmp, monkeypatch)
    progress_path = workspace_tmp / "progress.json"
    segment = Segment(area=1, professional_role=1, period=1, schedule="fullDay", depth=4)
    progress_path.write_text(
        json.dumps(
            {
                "started_at": "2026-04-27T12:00:00Z",
                "root": {"area": 1},
                "max_depth": 4,
                "max_vacancies": 2_000_000,
                "rate_limit_sec": 1.0,
                "closed_segments": [segment.to_dict(include_depth=False)],
                "uncovered": [],
                "stats": {"requests": 0, "vacancies_fetched": 0, "segments_done": 1, "last_update": "2026-04-27T12:00:00Z"},
            }
        ),
        encoding="utf-8",
    )
    client = FakeClient({(1, 1, 1, "fullDay"): 100})

    crawl(
        segment,
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=progress_path,
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert client.calls == []


def test_progress_atomic_write(workspace_tmp: Path, monkeypatch):
    calls = []

    def fake_replace(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        Path(dst).write_text(Path(src).read_text(encoding="utf-8"), encoding="utf-8")
        Path(src).unlink()

    monkeypatch.setattr("src.ingest.hh_crawler.os.replace", fake_replace)

    _save_progress_atomic(workspace_tmp / "progress.json", {"stats": {"requests": 1}})

    assert calls == [("progress.json.tmp", "progress.json")]
    assert json.loads((workspace_tmp / "progress.json").read_text(encoding="utf-8"))["stats"]["requests"] == 1
    assert not (workspace_tmp / "progress.json.tmp").exists()


class _Failing403Client:
    """Имитирует Cloudflare 403 после max_retries в hh_shards."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        raise HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")


class _RateLimitedClient:
    def __init__(self, totals: dict[tuple, int]) -> None:
        self.totals = totals
        self.calls: list[dict] = []
        self.search_first = True

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        key = _key(kwargs)
        page = kwargs.get("page", 0)
        if page == 0:
            return {
                "vacancySearchResult": {
                    "vacancies": [_vacancy(i) for i in range(100)],
                    "totalResults": self.totals.get(key, 100),
                    "paging": {"lastPage": {"page": 1}},
                }
            }
        raise RateLimited(retry_after_sec=1.0)


def test_failed_search_recorded_and_continues(workspace_tmp: Path, monkeypatch):
    """403/transient на page=0 → сегмент в failed_segments, остальные area-children выполняются."""
    _write_refdata(workspace_tmp, monkeypatch)
    client = _Failing403Client()

    progress = crawl(
        Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert len(progress["failed_segments"]) == 1
    failed = progress["failed_segments"][0]
    assert failed["area"] == 1
    assert "403" in failed["reason"]
    assert progress["stats"]["segments_done"] == 0


def test_failed_drain_recorded(workspace_tmp: Path, monkeypatch):
    """Transient на странице >0 (drain) → сегмент в failed_segments, page=0 fetched всё равно учитывается."""
    _write_refdata(workspace_tmp, monkeypatch)
    key = (1, None, None, None)
    client = _RateLimitedClient({key: 200})

    progress = crawl(
        Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert len(progress["failed_segments"]) == 1
    assert "drain" in progress["failed_segments"][0]["reason"]
    assert progress["stats"]["segments_done"] == 0


def test_drain_segment_pagination(workspace_tmp: Path):
    segment = Segment(area=1, professional_role=156, period=1, schedule=None, depth=3)
    key = (1, 156, 1, None)
    client = FakeClient(
        {key: 250},
        {key: [[_vacancy(i) for i in range(100)], [_vacancy(i) for i in range(100, 200)], [_vacancy(i) for i in range(200, 250)]]},
    )

    count, requests = _drain_segment(segment, client, workspace_tmp / "lake")

    assert count == 250
    assert requests == 3
    assert [c["page"] for c in client.calls] == [0, 1, 2]
    df = pl.read_parquet(str(workspace_tmp / "lake" / "**" / "*.parquet"))
    assert df.height == 250


class _RateLimitMidDrainClient:
    """Returns 100 vacancies/page for pages 0..fail_at-1, then raises RateLimited."""

    def __init__(self, fail_at: int, total: int) -> None:
        self.fail_at = fail_at
        self.total = total
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        page = kwargs.get("page", 0)
        if page >= self.fail_at:
            raise RateLimited(retry_after_sec=1.0)
        # synthetic vacancyIds so each page has 100 unique entries
        vacancies = [_vacancy(page * 100 + i) for i in range(100)]
        last_page_idx = max((self.total // 100) - 1, 0)
        return {
            "vacancySearchResult": {
                "vacancies": vacancies,
                "totalResults": self.total,
                "paging": {"lastPage": {"page": last_page_idx}},
            }
        }


def test_drain_segment_mid_drain_429_writes_nothing(workspace_tmp: Path):
    """KM re-audit 2026-05-17 P1: до atomic-фикса write_batch с интервалом 2000
    мог оставить partial pages в lake при 429 на page 25 (после 25 страниц =
    2500 records → один flush). На next sweep сегмент перефетчился с 0 →
    дубли. После фикса lake остаётся пустым на mid-drain exception."""
    segment = Segment(area=1, professional_role=None, period=None, schedule=None, depth=1)
    client = _RateLimitMidDrainClient(fail_at=25, total=10_000)

    with pytest.raises(RateLimited):
        _drain_segment(segment, client, workspace_tmp / "lake")

    # No parquet files written despite 25 successful pages (2500 records collected).
    parquet_files = list((workspace_tmp / "lake").rglob("*.parquet"))
    assert parquet_files == []


# ---------------------------------------------------------------------------
# Pure helpers — Segment, _next_dimension, _append_unique, _format_elapsed.
# ---------------------------------------------------------------------------


def test_segment_to_dict_include_depth_flag():
    """`include_depth=True` сохраняет depth для diagnostic dumps (uncovered/failed
    payloads), `include_depth=False` (default) — нет, чтобы search_kwargs не
    проталкивал depth в hh.ru API."""
    segment = Segment(area=1, professional_role=42, period=7, schedule="remote", depth=3)
    base = segment.to_dict()
    with_depth = segment.to_dict(include_depth=True)
    assert "depth" not in base
    assert with_depth["depth"] == 3
    assert with_depth["area"] == 1
    assert with_depth["professional_role"] == 42
    assert with_depth["period"] == 7
    assert with_depth["schedule"] == "remote"


def test_next_dimension_russia_at_depth0_splits_by_area():
    """Special-case for area=113 + depth=0 — federated area split (lines 206-210),
    обходит role split last-resort branch."""
    root = Segment(area=113, professional_role=None, period=None, schedule=None, depth=0)
    children = _next_dimension(root, depth=0, areas=[1, 2, 3], roles=[10, 20], periods=PERIODS, schedules=SCHEDULES)
    assert [c.area for c in children] == [1, 2, 3]
    assert all(c.depth == 1 for c in children)
    assert all(c.professional_role is None and c.period is None and c.schedule is None for c in children)


def test_next_dimension_splits_by_period_when_unset():
    """Non-Russia (или post-area-split) сегмент с period=None — period split
    (lines 211-221), schedule остаётся None для следующего уровня."""
    segment = Segment(area=1, professional_role=None, period=None, schedule=None, depth=1)
    children = _next_dimension(segment, depth=1, areas=[1, 2], roles=[10], periods=PERIODS, schedules=SCHEDULES)
    assert [c.period for c in children] == PERIODS
    assert all(c.area == 1 for c in children)
    assert all(c.schedule is None for c in children)
    assert all(c.depth == 2 for c in children)


def test_next_dimension_splits_by_schedule_when_period_set():
    """period set, schedule=None — schedule split (lines 222-232)."""
    segment = Segment(area=1, professional_role=None, period=7, schedule=None, depth=2)
    children = _next_dimension(segment, depth=2, areas=[1], roles=[10], periods=PERIODS, schedules=SCHEDULES)
    assert [c.schedule for c in children] == SCHEDULES
    assert all(c.period == 7 for c in children)
    assert all(c.depth == 3 for c in children)


def test_next_dimension_splits_by_role_last_resort():
    """period+schedule set, role=None — last-resort role split (lines 233-243).
    Это самый дорогой split (33 категории), поэтому идёт после period/schedule."""
    segment = Segment(area=1, professional_role=None, period=7, schedule="remote", depth=3)
    children = _next_dimension(segment, depth=3, areas=[1], roles=[10, 20, 30], periods=PERIODS, schedules=SCHEDULES)
    assert [c.professional_role for c in children] == [10, 20, 30]
    assert all(c.depth == 4 for c in children)


def test_next_dimension_all_set_returns_empty():
    """Все 4 dimensions заданы → дальше делить некуда (line 244). Caller
    пишет сегмент в uncovered."""
    segment = Segment(area=1, professional_role=10, period=7, schedule="remote", depth=4)
    assert _next_dimension(segment, depth=4, areas=[1], roles=[10], periods=PERIODS, schedules=SCHEDULES) == []


def test_append_unique_skips_duplicate():
    """Дубликат не добавляется (false-branch line 372)."""
    items: list[dict] = [{"area": 1}]
    _append_unique(items, {"area": 1})
    _append_unique(items, {"area": 2})
    assert items == [{"area": 1}, {"area": 2}]


def test_format_elapsed_handles_none_and_minute_hour_boundaries():
    """started_at=None → '0m' (line 399-400); <60min → 'Nm'; ≥60min → 'NhMm'."""
    assert _format_elapsed(None) == "0m"
    assert _format_elapsed("") == "0m"

    from datetime import datetime as _dt
    from datetime import timezone as _tz

    now = _dt.now(_tz.utc)
    # ~5 minutes ago
    five_min_ago = (now.replace(microsecond=0) - __import__("datetime").timedelta(minutes=5)).isoformat()
    formatted = _format_elapsed(five_min_ago.replace("+00:00", "Z"))
    assert formatted.endswith("m") and "h" not in formatted

    # ~2h 30min ago
    two_h_ago = (now.replace(microsecond=0) - __import__("datetime").timedelta(hours=2, minutes=30)).isoformat()
    formatted_h = _format_elapsed(two_h_ago.replace("+00:00", "Z"))
    assert "h" in formatted_h and formatted_h.endswith("m")


def test_maybe_log_emits_print_after_30_seconds(capsys):
    """`_maybe_log` печатает прогресс не чаще 30s (lines 386-395). Чтобы не ждать
    реальное окно, подкручиваем last_log['at'] в прошлое."""
    last_log = {"at": time.monotonic() - 100}
    progress = {
        "started_at": "2026-05-18T12:00:00Z",
        "current": "area=1 period=7",
        "stats": {"requests": 5, "vacancies_fetched": 250, "segments_done": 1, "last_update": "x"},
    }
    _maybe_log(progress, last_log)
    captured = capsys.readouterr()
    assert "[crawler]" in captured.out
    assert "requests=5" in captured.out
    assert "vacancies=250" in captured.out
    # last_log["at"] двинут вперёд — повторный вызов в том же тике не печатает.
    last_log["at"] = time.monotonic()
    _maybe_log(progress, last_log)
    captured = capsys.readouterr()
    assert captured.out == ""


# ---------------------------------------------------------------------------
# _load_split_dimensions — fallback fetch + empty raises.
# ---------------------------------------------------------------------------


def test_load_split_dimensions_fetches_when_yaml_missing(monkeypatch, tmp_path):
    """ROLES/AREAS yaml отсутствуют → fetch_areas/fetch_professional_roles +
    save (lines 315-318)."""
    roles_path = tmp_path / "roles.yaml"
    areas_path = tmp_path / "areas.yaml"
    monkeypatch.setattr("src.ingest.hh_crawler.ROLES_PATH", roles_path)
    monkeypatch.setattr("src.ingest.hh_crawler.AREAS_PATH", areas_path)

    fetched_areas_payload = [
        {
            "id": "113",
            "name": "Russia",
            "areas": [
                {"id": "1", "name": "Moscow", "areas": []},
                {"id": "2", "name": "SPB", "areas": []},
            ],
        }
    ]
    fetched_roles_payload = {
        "categories": [
            {"id": "1", "name": "IT", "roles": [{"id": "10", "name": "DA"}, {"id": "20", "name": "DE"}]}
        ]
    }
    call_order: list[str] = []

    def fake_fetch_areas():
        call_order.append("fetch_areas")
        return fetched_areas_payload

    def fake_fetch_roles():
        call_order.append("fetch_roles")
        return fetched_roles_payload

    monkeypatch.setattr("src.ingest.hh_crawler.fetch_areas", fake_fetch_areas)
    monkeypatch.setattr("src.ingest.hh_crawler.fetch_professional_roles", fake_fetch_roles)

    areas, roles = _load_split_dimensions()
    assert areas == [1, 2]
    assert roles == [10, 20]
    assert call_order == ["fetch_areas", "fetch_roles"]
    # yaml-кэши записаны для следующего запуска.
    assert areas_path.exists()
    assert roles_path.exists()


def test_load_split_dimensions_empty_areas_raises(monkeypatch, tmp_path):
    """russia_subjects вернул [] (defensive guard на случай если refdata-схема
    эволюционирует) → ValueError (line 326). В текущей реализации russia_subjects
    сам raises раньше для типичных пустых случаев — мокаем его прямо, чтобы
    проверить именно hh_crawler-side guard."""
    import yaml

    roles_path = tmp_path / "roles.yaml"
    areas_path = tmp_path / "areas.yaml"
    areas_path.write_text(
        yaml.safe_dump(
            [{"id": "113", "name": "Russia", "areas": [{"id": "1", "name": "X", "areas": []}]}]
        ),
        encoding="utf-8",
    )
    roles_path.write_text(
        yaml.safe_dump({"categories": [{"id": "1", "name": "IT", "roles": [{"id": "10", "name": "DA"}]}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.ingest.hh_crawler.ROLES_PATH", roles_path)
    monkeypatch.setattr("src.ingest.hh_crawler.AREAS_PATH", areas_path)
    monkeypatch.setattr("src.ingest.hh_crawler.russia_subjects", lambda _: [])

    with pytest.raises(ValueError, match="area split"):
        _load_split_dimensions()


def test_load_split_dimensions_empty_roles_raises(monkeypatch, tmp_path):
    """categories пуст → ValueError (line 328)."""
    import yaml

    roles_path = tmp_path / "roles.yaml"
    areas_path = tmp_path / "areas.yaml"
    areas_path.write_text(
        yaml.safe_dump(
            [{"id": "113", "name": "Russia", "areas": [{"id": "1", "name": "Moscow", "areas": []}]}]
        ),
        encoding="utf-8",
    )
    roles_path.write_text(yaml.safe_dump({"categories": []}), encoding="utf-8")
    monkeypatch.setattr("src.ingest.hh_crawler.ROLES_PATH", roles_path)
    monkeypatch.setattr("src.ingest.hh_crawler.AREAS_PATH", areas_path)

    with pytest.raises(ValueError, match="professional_role split"):
        _load_split_dimensions()


# ---------------------------------------------------------------------------
# crawl() error paths — generic exceptions, max_vacancies cap, children empty.
# ---------------------------------------------------------------------------


class _SearchGenericExceptionClient:
    """Non-transient Exception в search() (не HHTransientError, не RateLimited).
    Crawler ловит и записывает в failed_segments с `type(exc).__name__` префиксом."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        raise RuntimeError("unexpected boom")


def test_crawl_search_generic_exception_records_failure(workspace_tmp: Path, monkeypatch):
    """Generic Exception (не transient/rate) в client.search → failed_segments
    с type(exc).__name__ префиксом (lines 132-136)."""
    _write_refdata(workspace_tmp, monkeypatch)
    client = _SearchGenericExceptionClient()

    progress = crawl(
        Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert len(progress["failed_segments"]) == 1
    failed = progress["failed_segments"][0]
    assert failed["area"] == 1
    assert "search:" in failed["reason"]
    assert "RuntimeError" in failed["reason"]


class _DrainGenericExceptionClient:
    """Page 0 OK, page 1+ raises generic Exception → drain catch line 176-180."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(dict(kwargs))
        page = kwargs.get("page", 0)
        if page == 0:
            return {
                "vacancySearchResult": {
                    "vacancies": [_vacancy(i) for i in range(100)],
                    "totalResults": self.total,
                    "paging": {"lastPage": {"page": 1}},
                }
            }
        raise RuntimeError("network reset mid-drain")


def test_crawl_drain_generic_exception_records_failure(workspace_tmp: Path, monkeypatch):
    """Generic Exception в drain page>0 → failed_segments + lake пустой
    (atomic semantics, lines 176-180)."""
    _write_refdata(workspace_tmp, monkeypatch)
    client = _DrainGenericExceptionClient(total=200)

    progress = crawl(
        Segment(area=1, professional_role=None, period=None, schedule=None, depth=1),
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert len(progress["failed_segments"]) == 1
    failed = progress["failed_segments"][0]
    assert "drain:" in failed["reason"]
    assert "RuntimeError" in failed["reason"]
    # Atomic: ни одного parquet, хоть page=0 fetched 100 vacancies.
    assert list((workspace_tmp / "lake").rglob("*.parquet")) == []


def test_crawl_max_vacancies_cap_skips_remaining_children(workspace_tmp: Path, monkeypatch):
    """vacancies_fetched ≥ max_vacancies → ранний выход (line 119) на каждом
    последующем child-сегменте после area-split."""
    _write_refdata(workspace_tmp, monkeypatch)
    totals: dict[tuple, int] = {(113, None, None, None): 20_000}
    pages: dict[tuple, list[list[dict]]] = {}
    for area in range(1, 9):
        totals[(area, None, None, None)] = 50
        pages[(area, None, None, None)] = [[_vacancy(area * 100 + i) for i in range(50)]]
    client = FakeClient(totals, pages)

    progress = crawl(
        Segment(area=113, professional_role=None, period=None, schedule=None, depth=0),
        max_depth=4,
        max_vacancies=50,  # cap = первая area-выборка (50)
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    # 50 fetched, остальные 7 area-children skipped через line 119 (без search-вызова).
    assert progress["stats"]["vacancies_fetched"] == 50
    fetched_areas = [c["area"] for c in client.calls if c.get("area") != 113]
    assert fetched_areas == [1]


def test_crawl_over_cap_children_empty_writes_uncovered(workspace_tmp: Path, monkeypatch):
    """Over-cap на сегменте где все 4 dimensions заполнены и depth<max_depth →
    _next_dimension возвращает [] → uncovered (lines 148-151). max_depth check
    (line 141) НЕ срабатывает, потому что depth=2 < max_depth=4."""
    _write_refdata(workspace_tmp, monkeypatch)
    segment = Segment(area=1, professional_role=10, period=7, schedule="remote", depth=2)
    client = FakeClient({(1, 10, 7, "remote"): 20_000})

    progress = crawl(
        segment,
        max_depth=4,
        max_vacancies=2_000_000,
        rate_limit_sec=0,
        progress_path=workspace_tmp / "progress.json",
        lake_root=workspace_tmp / "lake",
        client=client,
    )

    assert progress["uncovered"] == [{**segment.to_dict(), "total": 20_000}]
    assert progress["stats"]["segments_done"] == 0
