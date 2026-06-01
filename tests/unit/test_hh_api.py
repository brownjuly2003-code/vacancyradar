from __future__ import annotations

from typing import Any

import pytest
import requests

from src.ingest.hh_api import HHClient, HHConfig, HHTransientError


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: Any = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {"items": [], "pages": 1}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> Any:
        return self._json


class FakeSession:
    def __init__(self, responses: list[FakeResponse | Exception]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict | None]] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append((url, dict(params) if params else None))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_clock():
    state = {"now": 0.0}

    def clock() -> float:
        return state["now"]

    def sleep(secs: float) -> None:
        state["now"] += secs

    return state, clock, sleep


@pytest.fixture
def cfg() -> HHConfig:
    return HHConfig(
        base="https://api.hh.ru",
        user_agent="ua/test",
        requests_per_second=10.0,
        backoff_min=0.1,
        backoff_max=1.0,
        max_retries=3,
        timeout=5.0,
    )


def test_search_returns_json(cfg, fake_clock):
    _, clock, sleep = fake_clock
    payload = {"items": [{"id": "1", "name": "Data Analyst"}], "pages": 1}
    session = FakeSession([FakeResponse(200, payload)])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    data = client.search(area=113)

    assert data == payload
    assert session.calls == [("https://api.hh.ru/vacancies", {"area": 113, "per_page": 100, "page": 0})]
    assert session.headers["HH-User-Agent"] == "ua/test"
    assert "Authorization" not in session.headers


def test_access_token_sets_authorization_header(fake_clock):
    _, clock, sleep = fake_clock
    cfg_with_token = HHConfig(
        base="https://api.hh.ru",
        user_agent="ua/test",
        access_token="hh_demo_token",
        backoff_min=0.1,
        backoff_max=1.0,
        max_retries=3,
    )
    session = FakeSession([FakeResponse(200, {"items": [], "pages": 1})])
    client = HHClient(cfg_with_token, session=session, clock=clock, sleeper=sleep)

    client.search()

    assert session.headers["Authorization"] == "Bearer hh_demo_token"


def test_search_extra_params_pass_through(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([FakeResponse(200, {"items": [], "pages": 1})])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    client.search(area=1, text="data analyst", per_page=50)

    _, params = session.calls[0]
    assert params == {"area": 1, "per_page": 50, "page": 0, "text": "data analyst"}


def test_429_with_retry_after_waits_then_succeeds(cfg, fake_clock):
    state, clock, sleep = fake_clock
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "2"}),
        FakeResponse(200, {"items": [], "pages": 1}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    data = client.search()

    assert data == {"items": [], "pages": 1}
    # Retry-After=2 driven sleep observed (plus rate-limit gaps).
    assert state["now"] >= 2.0


def test_429_without_retry_after_uses_exponential_backoff(cfg, fake_clock):
    state, clock, sleep = fake_clock
    session = FakeSession([
        FakeResponse(429),
        FakeResponse(429),
        FakeResponse(200, {"items": [], "pages": 1}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    client.search()

    # First call goes immediately (rate gap is "next allowed time", not "wait now").
    # Backoff: 0.1 (attempt 0) + 0.2 (attempt 1). Subsequent rate-limit checks fall
    # within the backoff sleep, so no extra gap accrues.
    assert state["now"] == pytest.approx(0.3, abs=0.01)


def test_5xx_retried_then_raises_after_max(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([FakeResponse(503) for _ in range(10)])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    with pytest.raises(HHTransientError):
        client.search()

    assert len(session.calls) == cfg.max_retries + 1


def test_network_exception_treated_as_transient(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([
        requests.ConnectionError("boom"),
        FakeResponse(200, {"items": [], "pages": 1}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    data = client.search()

    assert data == {"items": [], "pages": 1}


def test_404_not_retried(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([FakeResponse(404)])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    with pytest.raises(requests.HTTPError):
        client.detail("doesnotexist")

    assert len(session.calls) == 1


def test_rate_limit_paces_consecutive_calls(cfg, fake_clock):
    state, clock, sleep = fake_clock
    session = FakeSession([
        FakeResponse(200, {"items": [], "pages": 1}),
        FakeResponse(200, {"items": [], "pages": 1}),
        FakeResponse(200, {"items": [], "pages": 1}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    for _ in range(3):
        client.search()

    # gap = 1/10 = 0.1s. Call 1 immediate; calls 2 and 3 each wait one gap → 2*0.1 = 0.2s.
    assert state["now"] == pytest.approx(0.2, abs=0.01)


def test_iter_pages_terminates_on_last_page(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([
        FakeResponse(200, {"items": [{"id": "a"}], "pages": 3}),
        FakeResponse(200, {"items": [{"id": "b"}], "pages": 3}),
        FakeResponse(200, {"items": [{"id": "c"}], "pages": 3}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    pages = list(client.iter_pages(area=113))

    assert len(pages) == 3
    assert [p["items"][0]["id"] for p in pages] == ["a", "b", "c"]
    pages_param = [c[1]["page"] for c in session.calls]
    assert pages_param == [0, 1, 2]


def test_iter_pages_handles_empty_result(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([FakeResponse(200, {"items": [], "pages": 0})])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    pages = list(client.iter_pages(area=113))

    assert pages == [{"items": [], "pages": 0}]
    assert len(session.calls) == 1


def test_iter_pages_starts_at_requested_page(cfg, fake_clock):
    _, clock, sleep = fake_clock
    session = FakeSession([
        FakeResponse(200, {"items": [{"id": "c"}], "pages": 5}),
        FakeResponse(200, {"items": [{"id": "d"}], "pages": 5}),
    ])
    client = HHClient(cfg, session=session, clock=clock, sleeper=sleep)

    pages = list(client.iter_pages(area=113, start_page=2, max_pages=2))

    assert len(pages) == 2
    pages_param = [c[1]["page"] for c in session.calls]
    assert pages_param == [2, 3]
