from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import requests

logger = logging.getLogger(__name__)


class RateLimited(Exception):
    def __init__(self, retry_after_sec: float | None = None):
        self.retry_after_sec = retry_after_sec
        super().__init__(f"hh.ru 429 (retry after {retry_after_sec}s)")


class HHTransientError(Exception):
    pass


@dataclass(frozen=True)
class HHConfig:
    base: str = "https://api.hh.ru"
    user_agent: str = "VacancyRadar/0.1 (research; contact: gemini.ge2026@gmail.com)"
    access_token: str | None = None
    requests_per_second: float = 10.0
    backoff_min: float = 1.0
    backoff_max: float = 60.0
    max_retries: int = 5
    timeout: float = 30.0


class HHClient:
    def __init__(
        self,
        cfg: HHConfig | None = None,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg or HHConfig()
        self.session = session or requests.Session()
        self.session.headers["HH-User-Agent"] = self.cfg.user_agent
        if self.cfg.access_token:
            self.session.headers["Authorization"] = f"Bearer {self.cfg.access_token}"
        self._sleep = sleeper
        self._clock = clock
        self._next_request_at: float = 0.0

    def _rate_limit(self) -> None:
        now = self._clock()
        if now < self._next_request_at:
            self._sleep(self._next_request_at - now)
            now = self._next_request_at
        gap = 1.0 / self.cfg.requests_per_second
        self._next_request_at = now + gap

    def _request(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.cfg.base}{path}"
        attempt = 0
        while True:
            self._rate_limit()
            last_error: Exception
            try:
                response = self.session.get(url, params=params, timeout=self.cfg.timeout)
            except requests.RequestException as exc:
                last_error = HHTransientError(f"{type(exc).__name__}: {exc}")
            else:
                status = response.status_code
                if status == 429:
                    raw = response.headers.get("Retry-After")
                    last_error = RateLimited(float(raw) if raw else None)
                elif 500 <= status < 600:
                    last_error = HHTransientError(f"hh.ru {status}")
                else:
                    response.raise_for_status()
                    return response.json()

            if attempt >= self.cfg.max_retries:
                raise last_error
            wait = self._compute_backoff(attempt, last_error)
            logger.warning("hh.ru retry %d after %.1fs: %s", attempt, wait, last_error)
            self._sleep(wait)
            attempt += 1

    def _compute_backoff(self, attempt: int, error: Exception) -> float:
        if isinstance(error, RateLimited) and error.retry_after_sec is not None:
            return error.retry_after_sec
        return min(self.cfg.backoff_min * (2**attempt), self.cfg.backoff_max)

    def search(
        self,
        *,
        area: int = 113,
        per_page: int = 100,
        page: int = 0,
        **extra: Any,
    ) -> dict:
        params: dict[str, Any] = {"area": area, "per_page": per_page, "page": page}
        params.update(extra)
        return self._request("/vacancies", params=params)

    def detail(self, vacancy_id: str) -> dict:
        return self._request(f"/vacancies/{vacancy_id}")

    def iter_pages(
        self,
        *,
        max_pages: int | None = None,
        start_page: int = 0,
        **search_kwargs: Any,
    ) -> Iterator[dict]:
        page = start_page
        pages_yielded = 0
        while True:
            data = self.search(page=page, **search_kwargs)
            yield data
            pages_yielded += 1
            page += 1
            if max_pages is not None and pages_yielded >= max_pages:
                return
            if page >= data.get("pages", 0):
                return
