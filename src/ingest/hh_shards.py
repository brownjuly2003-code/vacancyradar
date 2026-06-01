"""Public hh.ru search via /shards/vacancy/search using curl_cffi (Chrome JA3).

api.hh.ru/vacancies is closed at the Cloudflare edge unless requests come from a
registered application (UA whitelist + JA3 fingerprint). The hh.ru frontend hits
hh.ru/shards/vacancy/search to render the public search page; that endpoint is
reachable as long as the TLS fingerprint matches a real browser. curl_cffi
impersonates Chrome's JA3/JA4/H2 so this client receives 200 OK with full JSON.

This is intentionally separate from src.ingest.hh_api (api.hh.ru via OAuth Bearer)
so we can switch transports without losing the OAuth path for the future.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

logger = logging.getLogger(__name__)


class RateLimited(Exception):
    def __init__(self, retry_after_sec: float | None = None):
        self.retry_after_sec = retry_after_sec
        super().__init__(f"hh.ru shards 429 (retry after {retry_after_sec}s)")


class HHTransientError(Exception):
    pass


class _Response(Protocol):
    status_code: int
    headers: Any

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


class _Session(Protocol):
    def get(self, url: str, params: dict | None = None, timeout: float | None = None) -> _Response: ...


def _default_transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [ConnectionError, TimeoutError, OSError]
    try:
        from curl_cffi.requests.exceptions import RequestException as CurlRequestException

        types.append(CurlRequestException)
    except ImportError:
        pass
    return tuple(types)


@dataclass(frozen=True)
class HHShardsConfig:
    base: str = "https://hh.ru"
    impersonate: str = "chrome"
    requests_per_second: float = 2.0
    backoff_min: float = 1.0
    backoff_max: float = 120.0
    max_retries: int = 10
    timeout: float = 30.0


class HHShardsClient:
    """hh.ru/shards/vacancy/search wrapper with rate limit, retry, pagination."""

    def __init__(
        self,
        cfg: HHShardsConfig | None = None,
        session: _Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transient_exception_types: tuple[type[BaseException], ...] | None = None,
    ):
        self.cfg = cfg or HHShardsConfig()
        self.session = session if session is not None else self._make_default_session(self.cfg)
        self._sleep = sleeper
        self._clock = clock
        self._transient = transient_exception_types or _default_transient_types()
        self._next_request_at: float = 0.0

    @staticmethod
    def _make_default_session(cfg: HHShardsConfig) -> _Session:
        from curl_cffi import requests as curl_requests

        # curl_cffi Session принимает Literal[...] для impersonate, но мы
        # держим cfg.impersonate как str чтобы конфиг был сериализуем —
        # значение проверяется в runtime curl_cffi'ем сами.
        return curl_requests.Session(impersonate=cfg.impersonate)  # type: ignore[arg-type,return-value]

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
            last_error: BaseException
            try:
                response = self.session.get(url, params=params, timeout=self.cfg.timeout)
            except self._transient as exc:
                last_error = HHTransientError(f"{type(exc).__name__}: {exc}")
            else:
                status = response.status_code
                if status == 429:
                    raw = response.headers.get("Retry-After") if response.headers else None
                    last_error = RateLimited(float(raw) if raw else None)
                elif status == 403:
                    last_error = HHTransientError("hh.ru shards 403 (Cloudflare anti-bot)")
                elif status in (502, 503, 504, 520, 521, 522, 524):
                    last_error = HHTransientError(f"hh.ru shards {status}")
                elif 500 <= status < 600:
                    last_error = HHTransientError(f"hh.ru shards {status}")
                else:
                    response.raise_for_status()
                    try:
                        return response.json()
                    except Exception as exc:
                        # curl_cffi/orjson сюда падает при пустом или
                        # повреждённом 200-ответе (Cloudflare challenge,
                        # truncated body). Treat как transient — retry.
                        body_len = len(getattr(response, "content", b"") or b"")
                        last_error = HHTransientError(
                            f"json decode failed (body={body_len}b): {type(exc).__name__}"
                        )

            if attempt >= self.cfg.max_retries:
                raise last_error
            wait = self._compute_backoff(attempt, last_error)
            logger.warning("hh.ru shards retry %d after %.1fs: %s", attempt, wait, last_error)
            self._sleep(wait)
            attempt += 1

    def _compute_backoff(self, attempt: int, error: BaseException) -> float:
        if isinstance(error, RateLimited) and error.retry_after_sec is not None:
            return error.retry_after_sec
        return min(self.cfg.backoff_min * (2**attempt), self.cfg.backoff_max)

    def search(
        self,
        *,
        area: int = 113,
        per_page: int = 50,
        page: int = 0,
        order_by: str = "publication_time",
        **extra: Any,
    ) -> dict:
        """Return the full /shards/vacancy/search JSON payload (top-level)."""
        params: dict[str, Any] = {
            "area": area,
            "items_on_page": per_page,
            "page": page,
            "order_by": order_by,
        }
        params.update(extra)
        return self._request("/shards/vacancy/search", params=params)

    def iter_pages(
        self,
        *,
        max_pages: int | None = None,
        start_page: int = 0,
        **search_kwargs: Any,
    ) -> Iterator[dict]:
        """Yield search payloads page by page until paging.lastPage is reached.

        hh.ru caps deep pagination at page=99 (2000 results). max_pages overrides
        that cap from the caller's side.
        """
        page = start_page
        pages_yielded = 0
        while True:
            data = self.search(page=page, **search_kwargs)
            yield data
            pages_yielded += 1
            vsr = data.get("vacancySearchResult") or {}
            paging = vsr.get("paging") or {}
            last_page = (paging.get("lastPage") or {}).get("page")
            page += 1
            if max_pages is not None and pages_yielded >= max_pages:
                return
            if last_page is None or page > last_page:
                return


def extract_vacancies(payload: dict) -> list[dict]:
    """Pull the vacancies array out of a /shards/vacancy/search payload."""
    return list((payload.get("vacancySearchResult") or {}).get("vacancies") or [])
