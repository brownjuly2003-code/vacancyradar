"""TDD для hh.ru detail HTML scrape (Phase 5)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl

from src.ingest.hh_detail import (
    FTS_LIMIT,
    TEASER_LIMIT,
    HHDetail,
    HHDetailTransientError,
    clean_html,
    fetch_detail,
    fetch_missing_details,
    parse_description_html,
    read_details_cache,
    write_details_cache,
)


SAMPLE_PAGE = """<!DOCTYPE html><html><head><title>Test</title></head><body>
<header>...</header>
<main>
<div class="vacancy-section vacancy-section_magritte">
<div class="g-user-content" data-qa="vacancy-description"><p><strong>Senior Python Developer</strong></p>
<p>Мы ищем сильного разработчика с опытом работы с FastAPI, Django, PostgreSQL.</p>
<ul><li>Полная занятость</li><li>Гибкий график</li></ul>
<p>Уровень дохода: до 350 000 руб. на руки</p></div>
</div>
</main>
</body></html>"""


def _mock_response(status: int = 200, text: str = "", headers: dict | None = None):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.headers = headers or {}
    response.raise_for_status = lambda: None
    return response


class TestCleanHtml:
    def test_strips_tags(self):
        assert clean_html("<p>hello <b>world</b></p>") == "hello world"

    def test_decodes_entities(self):
        assert clean_html("&quot;ОРМАТЕК&quot; &amp; Co") == '"ОРМАТЕК" & Co'

    def test_collapses_whitespace(self):
        assert clean_html("a\n\n\nb     c\td") == "a b c d"

    def test_empty_input(self):
        assert clean_html("") == ""
        assert clean_html(None) == ""  # type: ignore[arg-type]


class TestParseDescriptionHtml:
    def test_extracts_description_div(self):
        out = parse_description_html(SAMPLE_PAGE)
        assert out is not None
        assert "Senior Python Developer" in out
        assert "FastAPI" in out

    def test_no_description_returns_none(self):
        assert parse_description_html("<html><body>nothing</body></html>") is None
        assert parse_description_html("") is None

    def test_handles_nested_divs_inside_description(self):
        """Старый regex ломался на закрывающем </div> вложенного блока —
        bs4 идёт по DOM-дереву и берёт правильное закрытие."""
        html = (
            '<div data-qa="vacancy-description">'
            "<div><strong>Заголовок</strong></div>"
            "<p>Описание <em>с акцентами</em></p>"
            "<ul><li>пункт 1</li><li>пункт 2</li></ul>"
            "</div>"
            "<div>another section</div>"
        )
        out = parse_description_html(html)
        assert out is not None
        assert "Заголовок" in out
        assert "Описание" in out
        assert "пункт 1" in out
        # Не должны прихватить соседний `<div>another section</div>`.
        assert "another section" not in out


class TestFetchDetail:
    def test_redirect_returns_empty_detail(self):
        get = MagicMock(return_value=_mock_response(status=301, headers={"Location": "/article/32027"}))
        d = fetch_detail("hh:132347798", get=get)
        assert d.vacancy_id == "hh:132347798"
        assert d.description_html is None
        assert d.description_teaser is None
        assert d.description_fts is None

    def test_200_with_description_returns_cleaned_text(self):
        get = MagicMock(return_value=_mock_response(status=200, text=SAMPLE_PAGE))
        d = fetch_detail("hh:127619695", get=get)
        assert d.vacancy_id == "hh:127619695"
        assert d.description_html is not None
        assert "FastAPI" in d.description_text
        assert d.description_teaser is not None
        assert d.description_fts is not None
        # plain text не содержит тегов
        assert "<p>" not in d.description_text
        assert "<strong>" not in d.description_teaser

    def test_200_without_description_div_returns_none_fields(self):
        get = MagicMock(return_value=_mock_response(status=200, text="<html>nope</html>"))
        d = fetch_detail("hh:1", get=get)
        assert d.description_html is None
        assert d.description_teaser is None

    def test_teaser_truncation_word_boundary(self):
        long = "<div data-qa=\"vacancy-description\"><p>" + ("слово " * 200) + "</p></div></div>"
        get = MagicMock(return_value=_mock_response(status=200, text=long))
        d = fetch_detail("hh:1", get=get)
        assert d.description_teaser is not None
        assert len(d.description_teaser) <= TEASER_LIMIT
        # обрезано на word boundary — не должно заканчиваться на половине слова
        assert d.description_teaser.endswith("слово") or " " not in d.description_teaser[-20:]

    def test_fts_uses_higher_limit(self):
        long = "<div data-qa=\"vacancy-description\"><p>" + ("test " * 500) + "</p></div></div>"
        get = MagicMock(return_value=_mock_response(status=200, text=long))
        d = fetch_detail("hh:1", get=get)
        assert d.description_fts is not None
        assert len(d.description_fts) > TEASER_LIMIT
        assert len(d.description_fts) <= FTS_LIMIT

    def test_uses_bare_id_in_url(self):
        get = MagicMock(return_value=_mock_response(status=200, text=SAMPLE_PAGE))
        fetch_detail("hh:127619695", get=get)
        url = get.call_args[0][0]
        assert url == "https://hh.ru/vacancy/127619695"

    def test_403_does_not_retry(self):
        def raise_403():
            raise AssertionError("403 forbidden")

        response = _mock_response(status=403)
        response.raise_for_status = raise_403  # type: ignore[method-assign]
        get = MagicMock(return_value=response)
        try:
            fetch_detail("hh:1", get=get, sleeper=lambda _: None)
        except AssertionError:
            pass
        assert get.call_count == 1

    def test_retries_on_429_uses_retry_after_header(self):
        get = MagicMock(
            side_effect=[
                _mock_response(status=429, headers={"Retry-After": "5"}),
                _mock_response(status=200, text=SAMPLE_PAGE),
            ]
        )
        sleeps: list[float] = []
        fetch_detail("hh:1", get=get, sleeper=sleeps.append)
        assert sleeps == [5.0]

    def test_retries_on_5xx(self):
        get = MagicMock(
            side_effect=[
                _mock_response(status=502),
                _mock_response(status=200, text=SAMPLE_PAGE),
            ]
        )
        d = fetch_detail("hh:1", get=get, sleeper=lambda _: None)
        assert get.call_count == 2
        assert d.description_teaser is not None

    def test_gives_up_after_max_retries(self):
        get = MagicMock(return_value=_mock_response(status=502))
        try:
            fetch_detail("hh:1", get=get, max_retries=2, sleeper=lambda _: None)
            assert False, "expected HHDetailTransientError"
        except HHDetailTransientError as exc:
            assert "502" in str(exc)
        assert get.call_count == 3  # initial + 2 retries

    def test_non_retryable_4xx_does_not_retry(self):
        def raise_404():
            raise AssertionError("404 vacancy not found")

        response = _mock_response(status=404)
        response.raise_for_status = raise_404  # type: ignore[method-assign]
        get = MagicMock(return_value=response)
        try:
            fetch_detail("hh:1", get=get, sleeper=lambda _: None)
        except AssertionError:
            pass
        assert get.call_count == 1  # no retries on non-transient status


class TestCacheRoundtrip:
    def _detail(self, vid: str, text: str = "test description"):
        return HHDetail(
            vacancy_id=vid,
            fetched_at=datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc),
            description_html=f"<p>{text}</p>",
            description_text=text,
            description_teaser=text[:500] if text else None,
            description_fts=text[:1500] if text else None,
        )

    def test_write_and_read_empty(self, tmp_path: Path):
        path = tmp_path / "details.parquet"
        df = read_details_cache(path)
        assert df.is_empty()

    def test_write_and_read_one(self, tmp_path: Path):
        path = tmp_path / "details.parquet"
        write_details_cache([self._detail("hh:1")], path)
        df = read_details_cache(path)
        assert df.height == 1
        assert df["vacancy_id"][0] == "hh:1"
        assert df["description_text"][0] == "test description"

    def test_idempotent_upsert_keeps_latest_fetched(self, tmp_path: Path):
        path = tmp_path / "details.parquet"
        old = HHDetail(
            vacancy_id="hh:1",
            fetched_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
            description_html="<p>old</p>",
            description_text="old",
            description_teaser="old",
            description_fts="old",
        )
        new = HHDetail(
            vacancy_id="hh:1",
            fetched_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            description_html="<p>new</p>",
            description_text="new",
            description_teaser="new",
            description_fts="new",
        )
        write_details_cache([old], path)
        write_details_cache([new], path)
        df = read_details_cache(path)
        assert df.height == 1
        assert df["description_text"][0] == "new"


class TestFetchMissingDetails:
    def test_skips_already_cached(self, tmp_path: Path):
        path = tmp_path / "cache.parquet"
        existing = HHDetail(
            vacancy_id="hh:1",
            fetched_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
            description_html=None,
            description_text="cached",
            description_teaser="cached",
            description_fts="cached",
        )
        write_details_cache([existing], path)

        fetch = MagicMock(
            return_value=HHDetail(
                vacancy_id="hh:2",
                fetched_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
                description_html=None,
                description_text="new",
                description_teaser="new",
                description_fts="new",
            )
        )
        n = fetch_missing_details(["hh:1", "hh:2"], path, rate_limit_sec=0, fetch=fetch)
        assert n == 1
        fetch.assert_called_once_with("hh:2")
        df = pl.read_parquet(path).sort("vacancy_id")
        assert df["vacancy_id"].to_list() == ["hh:1", "hh:2"]

    def test_all_cached_no_calls(self, tmp_path: Path):
        path = tmp_path / "cache.parquet"
        write_details_cache(
            [
                HHDetail("hh:1", datetime(2026, 4, 27, tzinfo=timezone.utc), None, "x", "x", "x"),
            ],
            path,
        )
        fetch = MagicMock()
        n = fetch_missing_details(["hh:1"], path, rate_limit_sec=0, fetch=fetch)
        assert n == 0
        fetch.assert_not_called()

    def test_continues_on_individual_fetch_error(self, tmp_path: Path):
        path = tmp_path / "cache.parquet"

        def flaky(vid: str):
            if vid == "hh:bad":
                raise RuntimeError("fail")
            return HHDetail(vid, datetime(2026, 4, 27, tzinfo=timezone.utc), None, vid, vid, vid)

        n = fetch_missing_details(["hh:1", "hh:bad", "hh:2"], path, rate_limit_sec=0, fetch=flaky)
        assert n == 2
        df = pl.read_parquet(path).sort("vacancy_id")
        assert "hh:bad" not in df["vacancy_id"].to_list()

    def test_throttles_between_requests(self, tmp_path: Path, monkeypatch):
        """rate_limit_sec > 0 + i > 0 → time.sleep вызывается ровно (N-1) раз
        (line 311-312)."""
        path = tmp_path / "cache.parquet"
        sleeps: list[float] = []
        monkeypatch.setattr("src.ingest.hh_detail.time.sleep", lambda s: sleeps.append(s))

        def ok(vid: str) -> HHDetail:
            return HHDetail(vid, datetime(2026, 4, 27, tzinfo=timezone.utc), None, vid, vid, vid)

        n = fetch_missing_details(["hh:1", "hh:2", "hh:3"], path, rate_limit_sec=0.25, fetch=ok)
        assert n == 3
        assert sleeps == [0.25, 0.25]  # перед 2-м и 3-м запросами

    def test_logs_error_on_majority_failures(self, tmp_path: Path, caplog):
        """Если ≥50% fetch вызовов упали → error log (lines 321-326)."""
        path = tmp_path / "cache.parquet"

        def all_fail(vid: str):
            raise RuntimeError(f"fail {vid}")

        with caplog.at_level("ERROR", logger="src.ingest.hh_detail"):
            n = fetch_missing_details(
                ["hh:1", "hh:2", "hh:3", "hh:4"], path, rate_limit_sec=0, fetch=all_fail
            )
        assert n == 0
        assert any("4/4 failures" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Coverage gaps в fetch_detail (line 153-168) и write_details_cache (264).
# ---------------------------------------------------------------------------


def test_fetch_detail_connection_error_retried_via_transient_types():
    """`get` raises ConnectionError → transient_exc_types catch (line 167-168)
    → retry → success."""
    sleeps: list[float] = []
    calls = {"n": 0}

    def get(url, *, headers, timeout, allow_redirects):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("dns lookup failed")
        return _mock_response(status=200, text=SAMPLE_PAGE)

    detail = fetch_detail("hh:1", get=get, sleeper=sleeps.append)
    assert calls["n"] == 2
    assert detail.description_teaser is not None
    assert sleeps == [1.0]  # exponential backoff base


def test_fetch_detail_429_invalid_retry_after_falls_back_to_exponential(monkeypatch):
    """Retry-After header не парсится в float → fallback на exponential backoff
    (lines 178-179)."""
    sleeps: list[float] = []
    responses = [
        _mock_response(status=429, headers={"Retry-After": "garbage"}),
        _mock_response(status=200, text=SAMPLE_PAGE),
    ]
    get = MagicMock(side_effect=responses)

    fetch_detail("hh:1", get=get, sleeper=sleeps.append)
    # garbage Retry-After → fallback на 1.0 * 2**0 = 1.0
    assert sleeps == [1.0]


def test_fetch_detail_uses_default_session_when_no_get(monkeypatch):
    """Без `get=` argument → fetch_detail вызывает `_curl_cffi.get` напрямую
    (lines 153-159, _IMPERSONATE branch)."""
    captured: dict = {}

    def fake_curl_get(url, *, impersonate=None, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["impersonate"] = impersonate
        captured["allow_redirects"] = allow_redirects
        return _mock_response(status=200, text=SAMPLE_PAGE)

    monkeypatch.setattr("src.ingest.hh_detail._curl_cffi.get", fake_curl_get)
    monkeypatch.setattr("src.ingest.hh_detail._IMPERSONATE", "chrome")

    detail = fetch_detail("hh:777")
    assert captured["url"] == "https://hh.ru/vacancy/777"
    assert captured["impersonate"] == "chrome"
    assert captured["allow_redirects"] is False
    assert detail.description_teaser is not None


def test_fetch_detail_no_impersonate_uses_user_agent(monkeypatch):
    """`_IMPERSONATE` пустой (curl_cffi отсутствовал на import) → fallback на
    `requests`-style call с User-Agent (lines 160-166)."""
    captured: dict = {}

    def fake_curl_get(url, *, headers=None, timeout=None, allow_redirects=None):
        captured["url"] = url
        captured["headers"] = headers
        return _mock_response(status=200, text=SAMPLE_PAGE)

    monkeypatch.setattr("src.ingest.hh_detail._curl_cffi.get", fake_curl_get)
    monkeypatch.setattr("src.ingest.hh_detail._IMPERSONATE", None)

    fetch_detail("hh:888", user_agent="Test-UA")
    assert captured["url"] == "https://hh.ru/vacancy/888"
    assert captured["headers"] == {"User-Agent": "Test-UA"}


def test_write_details_cache_empty_list_returns_path_unchanged(tmp_path: Path):
    """Empty details — write_details_cache не создаёт файл и не падает (line 263-264)."""
    path = tmp_path / "cache.parquet"
    result = write_details_cache([], path)
    assert result == path
    assert not path.exists()


# ---------------------------------------------------------------------------
# _detail_transient_types — curl_cffi optional dep (lines 66-67).
# ---------------------------------------------------------------------------


def test_detail_transient_types_fallback_when_curl_cffi_missing(monkeypatch):
    """ImportError на curl_cffi.requests.exceptions → fallback на stdlib only
    (lines 66-67)."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "curl_cffi.requests.exceptions":
            raise ImportError("curl_cffi not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from src.ingest.hh_detail import _detail_transient_types

    types = _detail_transient_types()
    assert ConnectionError in types
    assert TimeoutError in types
    assert OSError in types
