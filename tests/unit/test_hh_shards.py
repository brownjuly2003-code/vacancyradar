from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.ingest.hh_shards import (
    HHShardsClient,
    HHShardsConfig,
    HHTransientError,
    extract_vacancies,
)
from src.ingest.raw_lake import RawRecord


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else _payload(0, last_page=0)
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise AssertionError(f"unexpected raise_for_status with {self.status_code}")

    def json(self) -> Any:
        return self._json


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []

    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append((url, dict(params) if params else None))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _payload(page: int, *, last_page: int = 0, vacancies: list[dict] | None = None,
             total: int | None = None) -> dict:
    return {
        "vacancySearchResult": {
            "vacancies": vacancies if vacancies is not None else [],
            "totalResults": total,
            "paging": {
                "lastPage": {"page": last_page},
                "previous": {"page": page - 1, "disabled": page == 0},
                "next": {"page": page + 1, "disabled": page >= last_page},
            },
        }
    }


@pytest.fixture
def fake_clock():
    state = {"now": 0.0}

    def clock() -> float:
        return state["now"]

    def sleep(secs: float) -> None:
        state["now"] += secs

    return state, clock, sleep


@pytest.fixture
def cfg() -> HHShardsConfig:
    return HHShardsConfig(
        base="https://hh.ru",
        impersonate="chrome",
        requests_per_second=10.0,
        backoff_min=0.1,
        backoff_max=1.0,
        max_retries=3,
        timeout=5.0,
    )


def _client(cfg, session, clock_pair):
    _, clock, sleep = clock_pair
    return HHShardsClient(
        cfg,
        session=session,
        clock=clock,
        sleeper=sleep,
        transient_exception_types=(ConnectionError, TimeoutError, OSError),
    )


def test_search_hits_shards_endpoint_with_default_params(cfg, fake_clock):
    payload = _payload(0, last_page=0, vacancies=[{"vacancyId": 1}])
    session = FakeSession([FakeResponse(200, payload)])
    client = _client(cfg, session, fake_clock)

    data = client.search(area=113)

    assert data == payload
    url, params = session.calls[0]
    assert url == "https://hh.ru/shards/vacancy/search"
    assert params == {"area": 113, "items_on_page": 50, "page": 0, "order_by": "publication_time"}


def test_extra_params_pass_through_and_override(cfg, fake_clock):
    session = FakeSession([FakeResponse(200, _payload(0))])
    client = _client(cfg, session, fake_clock)

    client.search(area=1, per_page=20, text="data analyst", order_by="relevance", professional_role=10)

    _, params = session.calls[0]
    assert params == {
        "area": 1, "items_on_page": 20, "page": 0,
        "order_by": "relevance", "text": "data analyst", "professional_role": 10,
    }


def test_429_with_retry_after_waits_then_succeeds(cfg, fake_clock):
    state, _, _ = fake_clock
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(200, _payload(0)),
    ])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert state["now"] >= 2.0
    assert len(session.calls) == 2


def test_429_without_retry_after_uses_exponential_backoff(cfg, fake_clock):
    state, _, _ = fake_clock
    session = FakeSession([
        FakeResponse(429),
        FakeResponse(429),
        FakeResponse(200, _payload(0)),
    ])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert state["now"] == pytest.approx(0.3, abs=0.01)


def test_5xx_retried_then_raises_after_max(cfg, fake_clock):
    session = FakeSession([FakeResponse(503) for _ in range(10)])
    client = _client(cfg, session, fake_clock)

    with pytest.raises(HHTransientError):
        client.search()

    assert len(session.calls) == cfg.max_retries + 1


def test_cloudflare_5xx_treated_as_transient(cfg, fake_clock):
    session = FakeSession([
        FakeResponse(520),
        FakeResponse(522),
        FakeResponse(200, _payload(0)),
    ])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert len(session.calls) == 3


def test_network_exception_treated_as_transient(cfg, fake_clock):
    session = FakeSession([
        ConnectionError("dns"),
        FakeResponse(200, _payload(0)),
    ])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert len(session.calls) == 2


def test_unexpected_exception_propagates(cfg, fake_clock):
    session = FakeSession([RuntimeError("boom")])
    client = _client(cfg, session, fake_clock)

    with pytest.raises(RuntimeError):
        client.search()


def test_iter_pages_terminates_on_last_page(cfg, fake_clock):
    session = FakeSession([
        FakeResponse(200, _payload(0, last_page=2, vacancies=[{"vacancyId": 1}])),
        FakeResponse(200, _payload(1, last_page=2, vacancies=[{"vacancyId": 2}])),
        FakeResponse(200, _payload(2, last_page=2, vacancies=[{"vacancyId": 3}])),
    ])
    client = _client(cfg, session, fake_clock)

    pages = list(client.iter_pages(area=113))

    assert len(pages) == 3
    page_params = [c[1]["page"] for c in session.calls]
    assert page_params == [0, 1, 2]


def test_iter_pages_respects_max_pages_cap(cfg, fake_clock):
    session = FakeSession([
        FakeResponse(200, _payload(0, last_page=99, vacancies=[{"vacancyId": 1}])),
        FakeResponse(200, _payload(1, last_page=99, vacancies=[{"vacancyId": 2}])),
    ])
    client = _client(cfg, session, fake_clock)

    pages = list(client.iter_pages(area=113, max_pages=2))

    assert len(pages) == 2


def test_iter_pages_starts_at_requested_page(cfg, fake_clock):
    session = FakeSession([
        FakeResponse(200, _payload(2, last_page=99, vacancies=[{"vacancyId": 3}])),
        FakeResponse(200, _payload(3, last_page=99, vacancies=[{"vacancyId": 4}])),
    ])
    client = _client(cfg, session, fake_clock)

    pages = list(client.iter_pages(area=113, start_page=2, max_pages=2))

    assert len(pages) == 2
    page_params = [c[1]["page"] for c in session.calls]
    assert page_params == [2, 3]


def test_extract_vacancies_handles_missing_keys():
    assert extract_vacancies({}) == []
    assert extract_vacancies({"vacancySearchResult": {}}) == []
    assert extract_vacancies({"vacancySearchResult": {"vacancies": None}}) == []
    assert extract_vacancies({"vacancySearchResult": {"vacancies": [{"vacancyId": 1}]}}) == [{"vacancyId": 1}]


def test_from_hh_shards_item_basic_mapping():
    item = {
        "vacancyId": 129605072,
        "name": "Водитель",
        "publicationTime": {"@timestamp": 1777109032, "$": "2026-04-25T12:23:52.395+03:00"},
        "creationTime": "2026-01-19T15:22:52.728+03:00",
        "company": {"id": 1780304, "name": "ACME"},
    }
    fetched = datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc)

    rec = RawRecord.from_hh_shards_item(item, fetched)

    assert rec.vacancy_id == "hh:129605072"
    assert rec.source == "hh"
    assert rec.fetched_at == fetched
    assert rec.posted_at is not None and rec.posted_at.year == 2026 and rec.posted_at.month == 4
    assert rec.employer_id == "1780304"
    assert "129605072" in rec.raw_json


def test_from_hh_shards_item_falls_back_to_creation_time_when_publication_missing():
    item = {
        "vacancyId": 1,
        "creationTime": {"@timestamp": 100, "$": "2026-01-19T15:22:52.728+03:00"},
        "company": {},
    }
    rec = RawRecord.from_hh_shards_item(item, datetime(2026, 4, 27, tzinfo=timezone.utc))
    assert rec.posted_at is not None and rec.posted_at.year == 2026 and rec.posted_at.month == 1
    assert rec.employer_id is None


def test_from_hh_shards_item_handles_missing_dates_and_company():
    item = {"vacancyId": 99}
    rec = RawRecord.from_hh_shards_item(item, datetime(2026, 4, 27, tzinfo=timezone.utc))
    assert rec.vacancy_id == "hh:99"
    assert rec.posted_at is None
    assert rec.employer_id is None


def test_from_hh_shards_item_volatile_fields_dont_affect_hash():
    """responsesCount/searchRid/clickUrl etc change between requests for an
    unchanged vacancy; they must not flip content_hash and trigger desc_changed."""
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    base = {
        "vacancyId": 1,
        "name": "Data Analyst",
        "company": {"id": 7, "name": "ACME"},
        "compensation": {"from": 100, "to": 200, "currencyCode": "RUR"},
        "publicationTime": "2026-04-25T12:00:00+03:00",
    }
    noisy = dict(base)
    noisy.update({
        "responsesCount": 42,
        "totalResponsesCount": 99,
        "online_users_count": 12,
        "searchRid": "abc123",
        "clickUrl": "https://hh.ru/click?rid=abc123",
        "userLabels": ["favorite"],
        "notify": True,
        "inboxPossibility": "ALLOWED",
        "chatWritePossibility": "ENABLED",
        "@isAdv": True,
    })

    rec_base = RawRecord.from_hh_shards_item(base, fetched)
    rec_noisy = RawRecord.from_hh_shards_item(noisy, fetched)

    assert rec_base.content_hash == rec_noisy.content_hash


def test_from_hh_shards_item_real_change_flips_hash():
    fetched = datetime(2026, 4, 27, tzinfo=timezone.utc)
    base = {
        "vacancyId": 1,
        "name": "Data Analyst",
        "compensation": {"from": 100, "to": 200, "currencyCode": "RUR"},
    }
    renamed = dict(base, name="Senior Data Analyst")
    repaid = dict(base, compensation={"from": 200, "to": 400, "currencyCode": "RUR"})

    h_base = RawRecord.from_hh_shards_item(base, fetched).content_hash
    assert RawRecord.from_hh_shards_item(renamed, fetched).content_hash != h_base
    assert RawRecord.from_hh_shards_item(repaid, fetched).content_hash != h_base


def test_from_hh_shards_item_raw_json_preserves_volatile_fields():
    """raw_json keeps the full payload for debug/replay; only hash is normalised."""
    item = {"vacancyId": 1, "name": "X", "responsesCount": 99, "searchRid": "rid"}
    rec = RawRecord.from_hh_shards_item(item, datetime(2026, 4, 27, tzinfo=timezone.utc))
    assert "responsesCount" in rec.raw_json
    assert "searchRid" in rec.raw_json


# ---------------------------------------------------------------------------
# Status-code coverage gaps (line 117 / 121 / 126-131).
# ---------------------------------------------------------------------------


def test_403_treated_as_cloudflare_transient(cfg, fake_clock):
    """403 от Cloudflare — transient (line 117). Не должен валить ingest сразу;
    retry до max_retries, потом raise."""
    session = FakeSession([FakeResponse(403) for _ in range(cfg.max_retries + 1)])
    client = _client(cfg, session, fake_clock)

    with pytest.raises(HHTransientError, match="Cloudflare anti-bot"):
        client.search()

    assert len(session.calls) == cfg.max_retries + 1


def test_403_retried_then_recovers(cfg, fake_clock):
    """403 → 200 в пределах retry budget = OK."""
    session = FakeSession([FakeResponse(403), FakeResponse(200, _payload(0))])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert len(session.calls) == 2


def test_500_non_curated_treated_as_transient(cfg, fake_clock):
    """500/501/505 не входят в curated 502-524 list → попадают в общий
    `500 <= status < 600` branch (line 120-121)."""
    session = FakeSession([
        FakeResponse(500),
        FakeResponse(501),
        FakeResponse(505),
        FakeResponse(200, _payload(0)),
    ])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert len(session.calls) == 4


def test_json_decode_failure_treated_as_transient(cfg, fake_clock):
    """200 OK с truncated body — json() raises (Cloudflare challenge / curl_cffi
    edge), caught как transient (lines 126-131)."""

    class _BadJsonResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__(200)
            self.content = b"<html>cloudflare</html>"  # noqa: S105 -- not a secret

        def json(self) -> Any:
            raise ValueError("orjson: invalid JSON")

    session = FakeSession([_BadJsonResponse(), FakeResponse(200, _payload(0))])
    client = _client(cfg, session, fake_clock)

    client.search()

    assert len(session.calls) == 2


def test_json_decode_failure_exhausts_retries(cfg, fake_clock):
    """json decode fails и не recover'ится — raises HHTransientError after
    max_retries с body length в сообщении (line 130-132)."""

    class _BadJsonResponse(FakeResponse):
        def __init__(self) -> None:
            super().__init__(200)
            self.content = b"x" * 50  # synthetic body length

        def json(self) -> Any:
            raise ValueError("orjson: invalid JSON")

    session = FakeSession([_BadJsonResponse() for _ in range(cfg.max_retries + 1)])
    client = _client(cfg, session, fake_clock)

    with pytest.raises(HHTransientError, match=r"json decode failed \(body=50b\)"):
        client.search()


# ---------------------------------------------------------------------------
# _default_transient_types — exception types fallback (lines 45-52).
# ---------------------------------------------------------------------------


def test_default_transient_types_baseline_includes_stdlib_exceptions():
    """Baseline всегда содержит stdlib transient exception types."""
    from src.ingest.hh_shards import _default_transient_types

    types = _default_transient_types()
    assert ConnectionError in types
    assert TimeoutError in types
    assert OSError in types


def test_default_transient_types_appends_curl_cffi_when_available(monkeypatch):
    """Если curl_cffi installed — `CurlRequestException` добавляется в tuple
    (lines 46-49)."""
    import sys
    import types as _types

    # Подменяем модуль curl_cffi.requests.exceptions с известным
    # RequestException классом.
    class _FakeRequestException(Exception):
        pass

    fake_module = _types.ModuleType("curl_cffi.requests.exceptions")
    fake_module.RequestException = _FakeRequestException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi.requests.exceptions", fake_module)

    from src.ingest.hh_shards import _default_transient_types

    types = _default_transient_types()
    assert _FakeRequestException in types


def test_default_transient_types_falls_back_when_curl_cffi_missing(monkeypatch):
    """ImportError на curl_cffi.requests.exceptions → silent fallback (lines 50-51)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "curl_cffi.requests.exceptions":
            raise ImportError("curl_cffi not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from src.ingest.hh_shards import _default_transient_types

    types = _default_transient_types()
    # Только stdlib types, без curl_cffi entry.
    assert ConnectionError in types
    assert all("curl_cffi" not in t.__module__ for t in types)


# ---------------------------------------------------------------------------
# _make_default_session — curl_cffi session construction (lines 86-91).
# ---------------------------------------------------------------------------


def test_make_default_session_constructs_curl_cffi_session(monkeypatch):
    """Default constructor: `HHShardsClient()` без session= → `_make_default_session`
    мокает curl_cffi.requests.Session с правильным `impersonate` (lines 86-91)."""
    import sys
    import types as _types

    captured: dict[str, Any] = {}

    class _FakeSession:
        def __init__(self, *, impersonate: str) -> None:
            captured["impersonate"] = impersonate

    fake_requests = _types.ModuleType("curl_cffi.requests")
    fake_requests.Session = _FakeSession  # type: ignore[attr-defined]
    fake_curl_cffi = _types.ModuleType("curl_cffi")
    fake_curl_cffi.requests = fake_requests  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_curl_cffi)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_requests)

    cfg = HHShardsConfig(impersonate="chrome120")
    client = HHShardsClient(cfg)  # session= не передаём → fallback в _make_default_session

    assert isinstance(client.session, _FakeSession)
    assert captured["impersonate"] == "chrome120"
