"""Fetch hh.ru vacancy detail HTML → cleaned description text.

Источник: `https://hh.ru/vacancy/{id}` (frontend, через curl_cffi с Chrome
JA3, как в hh_shards). api.hh.ru/vacancies/{id} даёт 403 даже с Chrome JA3 —
Cloudflare-фильтр на API endpoint жёстче чем на фронт.

Описание лежит в `<div data-qa="vacancy-description">...</div>`. Для
sponsored/marketing-вакансий (например, hh:132347798 → article/32027) hh.ru
делает 301 redirect — `fetch_detail` возвращает None.

Cache: `master/hh_details.parquet` — append-only по vacancy_id (idempotent
upsert). Re-fetch только тех id, которых нет в кэше.

Поля slim-active-v1:
- description_teaser — первые 500 символов cleaned plain text
- description_fts — до 1500 символов cleaned plain text для full-text search
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import polars as pl
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

_IMPERSONATE: str | None
try:
    from curl_cffi import requests as _curl_cffi
    _IMPERSONATE = "chrome"
except ImportError:  # pragma: no cover
    import requests as _curl_cffi  # type: ignore
    _IMPERSONATE = None


_WHITESPACE_RE = re.compile(r"\s+")

TEASER_LIMIT = 500
FTS_LIMIT = 1500

DETAIL_MAX_RETRIES = 3
DETAIL_BACKOFF_MIN = 1.0
DETAIL_BACKOFF_MAX = 60.0
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 524})


class HHDetailTransientError(Exception):
    """Retryable hh.ru/vacancy/{id} response (429, 5xx, or connection-level
    failure). fetch_detail retries with exponential backoff before surfacing it.
    """


def _detail_transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [ConnectionError, TimeoutError, OSError]
    try:
        from curl_cffi.requests.exceptions import RequestException as CurlRequestException

        types.append(CurlRequestException)
    except ImportError:
        pass
    return tuple(types)


@dataclass(frozen=True)
class HHDetail:
    vacancy_id: str  # namespaced 'hh:<id>'
    fetched_at: datetime
    description_html: str | None  # raw HTML from div, or None если redirect
    description_text: str  # cleaned plain text (full)
    description_teaser: str | None  # first TEASER_LIMIT chars
    description_fts: str | None  # first FTS_LIMIT chars


def clean_html(html: str) -> str:
    """Strip HTML tags + decode entities + collapse whitespace.

    BeautifulSoup корректно обрабатывает nested tags, broken HTML и
    namedchar entities (`&nbsp;` → U+00A0 etc), которые regex путал.
    """
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _truncate_word_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    return cut[:last_space] if last_space > 0 else cut


def parse_description_html(html: str) -> str | None:
    """Extract description block from full hh.ru vacancy page HTML.

    bs4 находит div корректно даже при nested div'ах внутри описания —
    старый regex ломался на закрывающих тегах вложенных блоков.
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.find("div", attrs={"data-qa": "vacancy-description"})
    if not isinstance(desc, Tag):
        return None
    return desc.decode_contents()


def fetch_detail(
    vacancy_id: str,
    *,
    get: Callable | None = None,
    timeout: float = 30.0,
    user_agent: str = "Mozilla/5.0",
    now: Callable[[], datetime] | None = None,
    max_retries: int = DETAIL_MAX_RETRIES,
    sleeper: Callable[[float], None] = time.sleep,
) -> HHDetail:
    """Fetch detail для одного vacancy_id. Возвращает HHDetail; description_*
    будут None если страница редиректит (sponsored / снятая вакансия).

    Транзитные ответы (429, 5xx, connection errors) ретраятся до `max_retries`
    раз с exponential backoff (1s → 2s → 4s, capped at 60s). После исчерпания
    ретраев выкидывает HHDetailTransientError.

    `get` — injectable HTTP getter (для тестов).
    """
    now_fn = now or (lambda: datetime.now(timezone.utc))
    bare_id = vacancy_id.split(":", 1)[1] if ":" in vacancy_id else vacancy_id
    url = f"https://hh.ru/vacancy/{bare_id}"
    transient_exc_types = _detail_transient_types()

    attempt = 0
    while True:
        last_error: BaseException | None = None
        response = None
        try:
            if get is not None:
                response = get(
                    url,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                    allow_redirects=False,
                )
            elif _IMPERSONATE:
                response = _curl_cffi.get(
                    url,
                    impersonate=_IMPERSONATE,  # type: ignore[arg-type]
                    timeout=timeout,
                    allow_redirects=False,
                )
            else:
                response = _curl_cffi.get(
                    url,
                    headers={"User-Agent": user_agent},
                    timeout=timeout,
                    allow_redirects=False,
                )
        except transient_exc_types as exc:
            last_error = HHDetailTransientError(f"{type(exc).__name__}: {exc}")

        if response is not None:
            status = response.status_code
            if status in _TRANSIENT_STATUS:
                retry_after = None
                if status == 429 and getattr(response, "headers", None):
                    raw = response.headers.get("Retry-After")
                    try:
                        retry_after = float(raw) if raw else None
                    except (TypeError, ValueError):
                        retry_after = None
                last_error = HHDetailTransientError(
                    f"hh.ru vacancy/{bare_id} {status}"
                    + (f" retry_after={retry_after}" if retry_after else "")
                )
                last_error.retry_after_sec = retry_after  # type: ignore[attr-defined]
            else:
                # Terminal: either redirect, 200 OK, or non-retryable 4xx
                break

        if attempt >= max_retries:
            assert last_error is not None
            raise last_error
        retry_after = getattr(last_error, "retry_after_sec", None)
        wait = (
            retry_after
            if retry_after is not None
            else min(DETAIL_BACKOFF_MIN * (2**attempt), DETAIL_BACKOFF_MAX)
        )
        logger.warning(
            "hh.ru detail retry %d after %.1fs: %s", attempt, wait, last_error
        )
        sleeper(wait)
        attempt += 1

    fetched_at = now_fn()
    assert response is not None  # break only with response set

    if response.status_code in (301, 302, 308):
        return HHDetail(
            vacancy_id=vacancy_id,
            fetched_at=fetched_at,
            description_html=None,
            description_text="",
            description_teaser=None,
            description_fts=None,
        )
    response.raise_for_status()

    raw_html = parse_description_html(response.text)
    if raw_html is None:
        return HHDetail(
            vacancy_id=vacancy_id,
            fetched_at=fetched_at,
            description_html=None,
            description_text="",
            description_teaser=None,
            description_fts=None,
        )

    cleaned = clean_html(raw_html)
    teaser = _truncate_word_boundary(cleaned, TEASER_LIMIT) if cleaned else None
    fts = _truncate_word_boundary(cleaned, FTS_LIMIT) if cleaned else None
    return HHDetail(
        vacancy_id=vacancy_id,
        fetched_at=fetched_at,
        description_html=raw_html,
        description_text=cleaned,
        description_teaser=teaser,
        description_fts=fts,
    )


HH_DETAILS_PATH_DEFAULT = Path("master/hh_details.parquet")


_CACHE_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "vacancy_id": pl.String,
    "fetched_at": pl.Datetime("us", "UTC"),
    "description_text": pl.String,
    "description_teaser": pl.String,
    "description_fts": pl.String,
}


def read_details_cache(path: Path) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema=_CACHE_SCHEMA)
    return pl.read_parquet(path)


def write_details_cache(details: Iterable[HHDetail], path: Path) -> Path:
    """Idempotent upsert (latest fetched_at wins для каждого vacancy_id)."""
    details_list = list(details)
    if not details_list:
        return path
    new_df = pl.DataFrame(
        {
            "vacancy_id": [d.vacancy_id for d in details_list],
            "fetched_at": [d.fetched_at for d in details_list],
            "description_text": [d.description_text for d in details_list],
            "description_teaser": [d.description_teaser for d in details_list],
            "description_fts": [d.description_fts for d in details_list],
        },
        schema=_CACHE_SCHEMA,
    )
    if path.exists():
        existing = pl.read_parquet(path)
        merged = (
            pl.concat([existing, new_df], how="vertical_relaxed")
            .sort("fetched_at", descending=True)
            .unique(subset=["vacancy_id"], keep="first", maintain_order=True)
            .sort("vacancy_id")
        )
    else:
        merged = new_df.sort("vacancy_id")
        path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(path, compression="zstd", compression_level=3)
    return path


def fetch_missing_details(
    vacancy_ids: Iterable[str],
    cache_path: Path,
    *,
    rate_limit_sec: float = 1.0,
    fetch: Callable[[str], HHDetail] = fetch_detail,
) -> int:
    """Fetch detail для всех vacancy_id, которых нет в кэше. Throttle 1 req/sec
    по умолчанию (вежливо к hh.ru). Returns count of newly cached entries.
    """
    cached_ids = set()
    if cache_path.exists():
        cached_ids = set(pl.read_parquet(cache_path)["vacancy_id"].to_list())

    todo = [vid for vid in vacancy_ids if vid not in cached_ids]
    if not todo:
        return 0

    new_details: list[HHDetail] = []
    failed = 0
    for i, vid in enumerate(todo):
        if i > 0 and rate_limit_sec > 0:
            time.sleep(rate_limit_sec)
        try:
            new_details.append(fetch(vid))
        except Exception as exc:  # noqa: BLE001 — продолжаем batch
            failed += 1
            logger.warning("hh detail fetch failed for %s: %s", vid, exc)

    if new_details:
        write_details_cache(new_details, cache_path)
    if failed and failed >= max(1, len(todo) // 2):
        logger.error(
            "hh detail batch: %d/%d failures (>=50%%) — Cloudflare/JA3 may be throttling",
            failed,
            len(todo),
        )
    return len(new_details)
