from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import duckdb
import polars as pl

from src.cli import _ingest_hh
from src.ingest.raw_lake import RawRecord, utcnow, write_batch


def _args(*, detect_closed: bool = False) -> Namespace:
    return Namespace(
        transport="shards",
        dry=False,
        scope=None,
        area=113,
        per_page=50,
        pages=1,
        page_start=1,
        overlap_pages=0,
        detect_closed=detect_closed,
    )


def _page(vacancies: list[dict], *, last_page: int = 0) -> dict:
    return {
        "vacancySearchResult": {
            "vacancies": vacancies,
            "totalResults": len(vacancies),
            "paging": {"lastPage": {"page": last_page}},
        }
    }


def _vacancy(vid: int, *, salary_from: int = 100) -> dict:
    return {
        "vacancyId": vid,
        "name": f"Vacancy {vid}",
        "company": {"id": f"emp-{vid}"},
        "compensation": {
            "from": salary_from,
            "to": salary_from + 100,
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
        "        name: DevOps-инженер\n"
        "  - id: '2'\n"
        "    name: Продажи\n"
        "    roles:\n"
        "      - id: '70'\n"
        "        name: Менеджер по продажам\n",
        encoding="utf-8",
    )


def _install_fake_shards(monkeypatch, pages_by_run: list[list[dict]]) -> None:
    class FakeHHShardsClient:
        def __init__(self, _cfg):
            self._pages = pages_by_run.pop(0)

        def iter_pages(self, **_kwargs):
            for page in self._pages:
                yield page

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)


def _event_counts(db_path: Path) -> dict[str, int]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute("SELECT type, COUNT(*) FROM events GROUP BY type").fetchall()
        return {event_type: count for event_type, count in rows}
    finally:
        con.close()


def test_ingest_hh_emits_closed_when_detect_closed_flag_and_id_disappears(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_fake_shards(
        monkeypatch,
        [
            [_page([_vacancy(1), _vacancy(2)])],
            [_page([_vacancy(1)])],
        ],
    )

    assert _ingest_hh(_args(detect_closed=True)) == 0
    assert _ingest_hh(_args(detect_closed=True)) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["closed"] == 1


def test_ingest_hh_scope_resolution_failure_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def boom(_scope_name):
        raise ValueError("unknown market scope 'missing'")

    monkeypatch.setattr("src.config.load_settings", lambda: object())
    monkeypatch.setattr("src.cli._resolve_hh_scope_role_ids", boom)

    args = _args()
    args.scope = "missing"

    assert _ingest_hh(args) == 2
    assert "unknown market scope 'missing'" in capsys.readouterr().err


def test_ingest_hh_new_id_after_existing_snapshot_emits_appeared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_fake_shards(
        monkeypatch,
        [
            [_page([_vacancy(1)])],
            [_page([_vacancy(2)])],
        ],
    )

    assert _ingest_hh(_args()) == 0
    assert _ingest_hh(_args()) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["appeared"] == 2


def test_ingest_hh_scope_duplicate_role_batches_emit_event_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    _install_fake_shards(monkeypatch, [[_page([_vacancy(1)])]])

    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["appeared"] == 1


def test_ingest_hh_detect_closed_noop_when_all_previous_ids_seen(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_fake_shards(
        monkeypatch,
        [
            [_page([_vacancy(1)])],
            [_page([_vacancy(1)])],
        ],
    )

    assert _ingest_hh(_args(detect_closed=True)) == 0
    assert _ingest_hh(_args(detect_closed=True)) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts == {"appeared": 1}


def test_ingest_hh_scope_empty_role_pages_returns_without_lake(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        seen_roles: list[int] = []

        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            FakeHHShardsClient.seen_roles.append(kwargs["professional_role"])
            return
            yield

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)

    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    assert FakeHHShardsClient.seen_roles == [156, 160]
    assert "[done] empty page" in capsys.readouterr().out
    assert not (tmp_path / "master" / "vacancies_raw.parquet").exists()


def test_ingest_hh_survives_missing_or_null_last_page(tmp_path, monkeypatch):
    """Regression: hh shards may return `paging.lastPage` as None or omit it.
    Sweep must keep going (role_complete just stays False) instead of crashing.
    """
    monkeypatch.chdir(tmp_path)

    page_null_lastpage = {
        "vacancySearchResult": {
            "vacancies": [_vacancy(11)],
            "totalResults": 1,
            "paging": {"lastPage": None},
        }
    }
    page_no_lastpage = {
        "vacancySearchResult": {
            "vacancies": [_vacancy(12)],
            "totalResults": 1,
            "paging": {},
        }
    }
    page_null_paging = {
        "vacancySearchResult": {
            "vacancies": [_vacancy(13)],
            "totalResults": 1,
            "paging": None,
        }
    }
    _install_fake_shards(
        monkeypatch,
        [[page_null_lastpage, page_no_lastpage, page_null_paging]],
    )

    args = _args()
    args.pages = 3
    assert _ingest_hh(args) == 0

    df = pl.read_parquet("master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet")
    assert sorted(df["vacancy_id"].to_list()) == ["hh:11", "hh:12", "hh:13"]


def test_ingest_hh_skips_role_on_transient_error_and_keeps_collected(tmp_path, monkeypatch, capsys):
    """A persistent Cloudflare 403 (HHTransientError after retries) on one role
    must not abort the whole ingest. Pages collected before the error are still
    written, the run exits 0, and a warning is logged.
    """
    from src.ingest.hh_shards import HHTransientError

    monkeypatch.chdir(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **_kwargs):
            yield _page([_vacancy(1)])
            raise HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)

    args = _args()
    args.pages = 3
    assert _ingest_hh(args) == 0

    assert "transient error" in capsys.readouterr().err
    df = pl.read_parquet("master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet")
    assert df["vacancy_id"].to_list() == ["hh:1"]


def test_ingest_hh_scope_one_role_transient_error_other_role_continues(tmp_path, monkeypatch):
    """When one IT role hits a transient edge ban, the sweep continues to the
    next role. The failed role keeps the sweep marked incomplete (so a later
    --detect-closed run is rejected), but the healthy role's rows still land.
    """
    from src.ingest.hh_shards import HHTransientError

    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            if role_id == 156:
                raise HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")
            item = {
                **_vacancy(role_id),
                "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
            }
            yield _page([item], last_page=0)

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    df = pl.read_parquet("master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet")
    assert df["vacancy_id"].to_list() == ["hh:160"]

    # The failed role keeps the sweep from being recorded as complete, so a
    # later --detect-closed run is rejected (no false closed events).
    state_path = tmp_path / "master" / "run_state" / "hh_completed_sweeps.json"
    assert not state_path.exists()


def test_ingest_hh_scope_all_roles_transient_error_fails_loudly(tmp_path, monkeypatch, capsys):
    """When EVERY attempted role is skipped on a transient error and nothing is
    collected, the run must exit non-zero (not the silent OK that masked a 6-day
    Cloudflare/captcha block). This is the total-failure counterpart to C1's
    partial-failure graceful skip: 0 collected + a transient skip is a hard
    collection failure, so the cron verdict logs FAIL and the publish gate blocks
    a stale republish. Distinguished from a legitimate empty sweep (no error),
    which still exits 0.
    """
    from src.ingest.hh_shards import HHTransientError

    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **_kwargs):
            raise HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")
            yield  # pragma: no cover - generator marker

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 3

    err = capsys.readouterr().err
    assert "collected 0 vacancies" in err
    assert not (tmp_path / "master" / "vacancies_raw.parquet").exists()


def test_ingest_hh_does_not_emit_closed_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_fake_shards(
        monkeypatch,
        [
            [_page([_vacancy(1), _vacancy(2)])],
            [_page([_vacancy(1)])],
        ],
    )

    assert _ingest_hh(_args()) == 0
    assert _ingest_hh(_args()) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert "closed" not in counts


def test_ingest_hh_emits_salary_changed_for_shards_compensation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _install_fake_shards(
        monkeypatch,
        [
            [_page([_vacancy(1, salary_from=100)])],
            [_page([_vacancy(1, salary_from=150)])],
        ],
    )

    assert _ingest_hh(_args()) == 0
    assert _ingest_hh(_args()) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["salary_changed"] == 1


def test_ingest_hh_passes_one_based_page_start_to_shards_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            seen.update(kwargs)
            yield _page([_vacancy(3)])
            yield _page([_vacancy(4)])

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.pages = 2
    args.page_start = 3
    args.overlap_pages = 1

    assert _ingest_hh(args) == 0

    assert seen["start_page"] == 2
    assert seen["max_pages"] == 2


def test_ingest_hh_dry_scope_prints_it_role_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    args = _args()
    args.dry = True
    args.scope = "it"

    assert _ingest_hh(args) == 0

    out = capsys.readouterr().out
    assert "scope=it" in out
    assert "professional_role=156,160" in out
    assert "70" not in out


def test_ingest_hh_scope_fetches_each_it_role_and_labels_raw_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    seen_roles: list[int] = []

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            seen_roles.append(role_id)
            item = {
                **_vacancy(role_id),
                "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
            }
            yield _page([item])

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    assert seen_roles == [156, 160]
    df = pl.read_parquet("master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet")
    assert sorted(df["market_scope"].to_list()) == ["it", "it"]
    assert sorted(df["professional_role_id"].to_list()) == [156, 160]


def test_ingest_hh_scope_writes_each_role_as_bounded_batch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    write_sizes: list[int] = []

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            yield _page(
                [
                    {
                        **_vacancy(role_id * 10),
                        "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
                    },
                    {
                        **_vacancy(role_id * 10 + 1),
                        "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
                    },
                ]
            )

    def fake_write_batch(records, lake_root):
        records_list = list(records)
        write_sizes.append(len(records_list))
        return lake_root / f"fake_{len(write_sizes)}.parquet"

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    monkeypatch.setattr("src.ingest.raw_lake.write_batch", fake_write_batch)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    assert write_sizes == [2, 2]


def test_ingest_hh_scope_records_completed_sweep_when_all_it_roles_complete(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            item = {
                **_vacancy(role_id),
                "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
            }
            yield _page([item], last_page=0)

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    state_path = tmp_path / "master" / "run_state" / "hh_completed_sweeps.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["it"]["scope"] == "it"
    assert state["it"]["source"] == "hh"
    assert state["it"]["area"] == 113
    assert state["it"]["role_ids"] == [156, 160]
    assert state["it"]["complete"] is True


def test_ingest_hh_scope_treats_last_page_none_as_role_complete(tmp_path, monkeypatch):
    """hh shards returns paging.lastPage=None on the final page (no further
    page exists). The completed-sweep guard must recognise that signal,
    otherwise no scoped sweep ever counts as complete and the closed-detection
    guard refuses every run.
    """
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            item = {
                **_vacancy(role_id),
                "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
            }
            yield {
                "vacancySearchResult": {
                    "vacancies": [item],
                    "totalResults": 1,
                    "paging": {"lastPage": None},
                }
            }

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    state_path = tmp_path / "master" / "run_state" / "hh_completed_sweeps.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["it"]["complete"] is True
    assert state["it"]["role_ids"] == [156, 160]


def test_ingest_hh_scope_detect_closed_only_for_scoped_previous_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    runs = [
        {
            156: [_page([{**_vacancy(1), "professionalRoles": [{"id": "156"}]}])],
            160: [_page([{**_vacancy(2), "professionalRoles": [{"id": "160"}]}])],
        },
        {
            156: [_page([{**_vacancy(1), "professionalRoles": [{"id": "156"}]}])],
            160: [_page([])],
        },
    ]

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            self._pages_by_role = runs.pop(0)

        def iter_pages(self, **kwargs):
            for page in self._pages_by_role[kwargs["professional_role"]]:
                yield page

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0
    write_batch(
        [RawRecord.from_hh_shards_item(_vacancy(99), utcnow(), market_scope=None)],
        tmp_path / "master" / "vacancies_raw.parquet",
    )

    detect_args = _args(detect_closed=True)
    detect_args.scope = "it"
    assert _ingest_hh(detect_args) == 0

    con = duckdb.connect(str(tmp_path / "master" / "events.duckdb"), read_only=True)
    try:
        closed_ids = [
            row[0]
            for row in con.execute(
                "SELECT vacancy_id FROM events WHERE type='closed' ORDER BY vacancy_id"
            ).fetchall()
        ]
    finally:
        con.close()
    assert closed_ids == ["hh:2"]


def test_ingest_hh_scope_detect_closed_emits_closed_when_completed_sweep_is_empty(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    runs = [
        {
            156: [_page([{**_vacancy(1), "professionalRoles": [{"id": "156"}]}])],
            160: [_page([])],
        },
        {
            156: [_page([])],
            160: [_page([])],
        },
    ]

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            self._pages_by_role = runs.pop(0)

        def iter_pages(self, **kwargs):
            for page in self._pages_by_role[kwargs["professional_role"]]:
                yield page

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    detect_args = _args(detect_closed=True)
    detect_args.scope = "it"
    assert _ingest_hh(detect_args) == 0

    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts["closed"] == 1


def _api_args(*, detect_closed: bool = False, pages: int = 1) -> Namespace:
    """Namespace mirroring _args() but with transport=api for the OAuth path."""
    return Namespace(
        transport="api",
        dry=False,
        scope=None,
        area=113,
        per_page=50,
        pages=pages,
        page_start=1,
        overlap_pages=0,
        detect_closed=detect_closed,
    )


def _api_vacancy(vid: int, *, salary_from: int = 100_000) -> dict:
    """api.hh.ru/vacancies item shape (different from shards)."""
    return {
        "id": str(vid),
        "name": f"API Vacancy {vid}",
        "employer": {"id": f"emp-{vid}", "name": "ACME"},
        "salary": {
            "from": salary_from,
            "to": salary_from + 50_000,
            "currency": "RUR",
        },
        "published_at": "2026-04-25T10:00:00+0300",
    }


def _api_page(items: list[dict], *, pages_total: int = 1, found: int | None = None) -> dict:
    return {
        "items": items,
        "pages": pages_total,
        "found": found if found is not None else len(items),
    }


def test_ingest_hh_api_transport_warns_when_token_missing_and_empty_pages(
    tmp_path, monkeypatch, capsys
):
    """api transport without HH_ACCESS_TOKEN: stderr warn + empty page early return."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HH_ACCESS_TOKEN", raising=False)
    # dotenv reads from CWD/parents; setenv to empty so load_dotenv() doesn't
    # overwrite with a real token from the dev .env.
    monkeypatch.setenv("HH_ACCESS_TOKEN", "")

    class FakeHHClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **_kwargs):
            yield _api_page([])  # one empty page → items_collected stays empty

    monkeypatch.setattr("src.ingest.hh_api.HHClient", FakeHHClient)

    assert _ingest_hh(_api_args()) == 0

    captured = capsys.readouterr()
    assert "HH_ACCESS_TOKEN not set" in captured.err
    assert "[done] empty page" in captured.out
    # Lake must not be created on empty fetch
    assert not (tmp_path / "master" / "vacancies_raw.parquet").exists()


def test_ingest_hh_api_transport_writes_records_with_token(tmp_path, monkeypatch, capsys):
    """api transport with HH_ACCESS_TOKEN set + items returned → lake write + appeared event."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HH_ACCESS_TOKEN", "fake-bearer-token")

    class FakeHHClient:
        last_kwargs: dict | None = None

        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            FakeHHClient.last_kwargs = kwargs
            yield _api_page([_api_vacancy(1001), _api_vacancy(1002)], pages_total=1)

    monkeypatch.setattr("src.ingest.hh_api.HHClient", FakeHHClient)

    assert _ingest_hh(_api_args()) == 0

    # Token-missing warning must NOT appear when the env var is set
    assert "HH_ACCESS_TOKEN not set" not in capsys.readouterr().err

    # iter_pages was called with the api-shape kwargs (area + per_page + max_pages + start_page)
    assert FakeHHClient.last_kwargs is not None
    assert FakeHHClient.last_kwargs["area"] == 113
    assert FakeHHClient.last_kwargs["per_page"] == 50
    assert FakeHHClient.last_kwargs["max_pages"] == 1
    assert FakeHHClient.last_kwargs["start_page"] == 0

    # Records landed in the lake under source=hh
    df = pl.read_parquet(
        "master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet"
    )
    assert sorted(df["vacancy_id"].to_list()) == ["hh:1001", "hh:1002"]

    # First-run appeared events were derived (Stage 2 of pipeline runs after write_batch)
    counts = _event_counts(tmp_path / "master" / "events.duckdb")
    assert counts.get("appeared", 0) == 2


def test_ingest_hh_api_transport_breaks_when_pages_cap_reached(tmp_path, monkeypatch):
    """The per-role loop must stop once page_num + 1 >= args.pages even if the
    fake client would yield more pages — args.pages is the smoke cap."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HH_ACCESS_TOKEN", "fake-bearer-token")

    pages_yielded = 0

    class FakeHHClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **_kwargs):
            nonlocal pages_yielded
            for n in range(5):  # would yield 5 pages if not capped
                pages_yielded += 1
                yield _api_page([_api_vacancy(2000 + n)], pages_total=5)

    monkeypatch.setattr("src.ingest.hh_api.HHClient", FakeHHClient)

    # args.pages=2 → loop must break after the 2nd page
    assert _ingest_hh(_api_args(pages=2)) == 0
    assert pages_yielded == 2

    df = pl.read_parquet(
        "master/vacancies_raw.parquet/year=*/month=*/source=hh/*.parquet"
    )
    assert sorted(df["vacancy_id"].to_list()) == ["hh:2000", "hh:2001"]


def test_ingest_hh_api_scope_empty_role_pages_returns_without_lake(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)
    monkeypatch.setenv("HH_ACCESS_TOKEN", "fake-bearer-token")

    class FakeHHClient:
        seen_roles: list[int] = []

        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            FakeHHClient.seen_roles.append(kwargs["professional_role"])
            return
            yield

    monkeypatch.setattr("src.ingest.hh_api.HHClient", FakeHHClient)

    args = _api_args()
    args.scope = "it"

    assert _ingest_hh(args) == 0

    assert FakeHHClient.seen_roles == [156, 160]
    assert "[done] empty page" in capsys.readouterr().out
    assert not (tmp_path / "master" / "vacancies_raw.parquet").exists()


def test_ingest_hh_scope_rejects_closed_detection_until_completed_sweep_guard(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write_it_scope_config(tmp_path)

    class FakeHHShardsClient:
        def __init__(self, _cfg):
            pass

        def iter_pages(self, **kwargs):
            role_id = kwargs["professional_role"]
            item = {
                **_vacancy(role_id),
                "professionalRoles": [{"id": str(role_id), "name": f"Role {role_id}"}],
            }
            yield _page([item], last_page=1)

    monkeypatch.setattr("src.ingest.hh_shards.HHShardsClient", FakeHHShardsClient)
    args = _args(detect_closed=True)
    args.scope = "it"
    args.dry = False

    assert _ingest_hh(args) == 2

    assert "current scoped hh sweep is incomplete" in capsys.readouterr().err
    assert not (tmp_path / "master" / "vacancies_raw.parquet").exists()
