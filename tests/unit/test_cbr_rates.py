"""TDD для CBR rates fetcher (Phase 5)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import polars as pl
import pytest

from src.ingest.cbr_rates import (
    CBRRate,
    fetch_rates,
    load_rates_for,
    utc_today,
    write_rates,
)


SAMPLE_XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="27.04.2026" name="Foreign Currency Market">
  <Valute ID="R01010">
    <NumCode>036</NumCode>
    <CharCode>AUD</CharCode>
    <Nominal>1</Nominal>
    <Name>Австралийский доллар</Name>
    <Value>50,1234</Value>
    <VunitRate>50,1234</VunitRate>
  </Valute>
  <Valute ID="R01235">
    <NumCode>840</NumCode>
    <CharCode>USD</CharCode>
    <Nominal>1</Nominal>
    <Name>Доллар США</Name>
    <Value>92,5000</Value>
    <VunitRate>92,5</VunitRate>
  </Valute>
  <Valute ID="R01239">
    <NumCode>978</NumCode>
    <CharCode>EUR</CharCode>
    <Nominal>1</Nominal>
    <Name>Евро</Name>
    <Value>100,2500</Value>
    <VunitRate>100,25</VunitRate>
  </Valute>
  <Valute ID="R01375">
    <NumCode>156</NumCode>
    <CharCode>CNY</CharCode>
    <Nominal>10</Nominal>
    <Name>Юаней</Name>
    <Value>127,5000</Value>
    <VunitRate>12,75</VunitRate>
  </Valute>
</ValCurs>
""".encode("utf-8")


def _mock_response(content: bytes):
    response = MagicMock()
    response.content = content
    response.status_code = 200
    response.raise_for_status = lambda: None
    return response


class TestFetchRates:
    def test_parses_sample_xml(self):
        get = MagicMock(return_value=_mock_response(SAMPLE_XML))
        rates = fetch_rates(date(2026, 4, 27), get=get)
        codes = {r.char_code for r in rates}
        assert codes == {"USD", "EUR", "CNY", "AUD"}
        usd = next(r for r in rates if r.char_code == "USD")
        assert usd.nominal == 1
        assert usd.value == 92.5
        cny = next(r for r in rates if r.char_code == "CNY")
        assert cny.nominal == 10
        assert cny.value == 127.5

    def test_uses_correct_date_param(self):
        get = MagicMock(return_value=_mock_response(SAMPLE_XML))
        fetch_rates(date(2026, 4, 27), get=get)
        _, kwargs = get.call_args
        assert kwargs["params"] == {"date_req": "27/04/2026"}

    def test_empty_xml_returns_empty(self):
        get = MagicMock(return_value=_mock_response(b'<?xml version="1.0"?><ValCurs></ValCurs>'))
        assert fetch_rates(date(2026, 4, 27), get=get) == []

    def test_404_for_unpublished_date_returns_empty(self):
        import requests as _requests

        missing = MagicMock()
        missing.status_code = 404
        missing.raise_for_status.side_effect = _requests.HTTPError(response=missing)
        get = MagicMock(return_value=missing)
        assert fetch_rates(date(2026, 4, 27), get=get, retries=3) == []
        assert get.call_count == 1

    def test_skips_invalid_value(self):
        bad = b"""<?xml version="1.0"?><ValCurs>
        <Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>not-a-number</Value></Valute>
        <Valute><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>100,5</Value></Valute>
        </ValCurs>"""
        get = MagicMock(return_value=_mock_response(bad))
        rates = fetch_rates(date(2026, 4, 27), get=get)
        assert [r.char_code for r in rates] == ["EUR"]

    def test_skips_entries_without_code_or_value(self):
        bad = b"""<?xml version="1.0"?><ValCurs>
        <Valute><Nominal>1</Nominal><Value>92,5</Value></Valute>
        <Valute><CharCode>USD</CharCode><Nominal>1</Nominal></Valute>
        <Valute><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>100,5</Value></Valute>
        </ValCurs>"""
        get = MagicMock(return_value=_mock_response(bad))

        rates = fetch_rates(date(2026, 4, 27), get=get)

        assert [r.char_code for r in rates] == ["EUR"]

    def test_retries_on_5xx_and_eventually_succeeds(self):
        """KM re-audit 2026-05-17 P1: silent CBR 5xx degradation prevented."""
        import requests as _requests

        bad_response = MagicMock()
        bad_response.status_code = 503
        bad_response.raise_for_status.side_effect = _requests.HTTPError(
            response=bad_response,
        )
        ok = _mock_response(SAMPLE_XML)
        get = MagicMock(side_effect=[bad_response, bad_response, ok])
        sleeper = MagicMock()
        rates = fetch_rates(
            date(2026, 4, 27),
            get=get,
            retries=3,
            backoff_base=0.01,
            sleep=sleeper,
        )
        assert {r.char_code for r in rates} == {"USD", "EUR", "CNY", "AUD"}
        assert get.call_count == 3
        assert sleeper.call_count == 2

    def test_raises_after_max_retries_on_5xx(self):
        import requests as _requests

        bad = MagicMock()
        bad.status_code = 502
        bad.raise_for_status.side_effect = _requests.HTTPError(response=bad)
        get = MagicMock(return_value=bad)
        with pytest.raises(_requests.HTTPError):
            fetch_rates(
                date(2026, 4, 27),
                get=get,
                retries=2,
                backoff_base=0.01,
                sleep=lambda _s: None,
            )
        assert get.call_count == 2

    def test_does_not_retry_other_4xx(self):
        import requests as _requests

        bad = MagicMock()
        bad.status_code = 403
        bad.raise_for_status.side_effect = _requests.HTTPError(response=bad)
        get = MagicMock(return_value=bad)
        with pytest.raises(_requests.HTTPError):
            fetch_rates(
                date(2026, 4, 27),
                get=get,
                retries=3,
                backoff_base=0.01,
                sleep=lambda _s: None,
            )
        assert get.call_count == 1


class TestWriteRates:
    def test_creates_new_file(self, tmp_path: Path):
        rates = [
            CBRRate(date=date(2026, 4, 27), char_code="USD", nominal=1, value=92.5),
            CBRRate(date=date(2026, 4, 27), char_code="EUR", nominal=1, value=100.25),
        ]
        path = tmp_path / "ref" / "cbr_rates.parquet"
        write_rates(rates, path)
        assert path.exists()
        df = pl.read_parquet(path)
        assert df.height == 2
        assert set(df["char_code"]) == {"USD", "EUR"}

    def test_idempotent_upsert(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 4, 27), "USD", 1, 92.5)], path)
        # повторно записать другую цену для того же (date, char_code) — overwrite
        write_rates([CBRRate(date(2026, 4, 27), "USD", 1, 95.0)], path)
        df = pl.read_parquet(path)
        assert df.height == 1
        assert df["value"][0] == 95.0

    def test_appends_new_dates(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 4, 27), "USD", 1, 92.5)], path)
        write_rates([CBRRate(date(2026, 4, 28), "USD", 1, 93.0)], path)
        df = pl.read_parquet(path).sort("date")
        assert df.height == 2
        assert df["value"].to_list() == [92.5, 93.0]


class TestLoadRatesFor:
    def test_returns_empty_with_synthetic_rur_when_file_missing(self, tmp_path: Path):
        result = load_rates_for(tmp_path / "nope.parquet", date(2026, 4, 27))
        assert result == {"RUR": 1.0, "RUB": 1.0}

    def test_returns_rates_with_unit_value(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates(
            [
                CBRRate(date(2026, 4, 27), "USD", 1, 92.5),
                CBRRate(date(2026, 4, 27), "CNY", 10, 127.5),
            ],
            path,
        )
        rates = load_rates_for(path, date(2026, 4, 27))
        assert rates["USD"] == 92.5
        assert rates["CNY"] == 12.75  # value/nominal
        assert rates["RUR"] == 1.0
        assert rates["RUB"] == 1.0

    def test_falls_back_to_latest_prior_date(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 4, 25), "USD", 1, 90.0)], path)
        rates = load_rates_for(path, date(2026, 4, 27))
        assert rates["USD"] == 90.0

    def test_no_dates_le_on_returns_only_synthetic(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 5, 1), "USD", 1, 92.5)], path)
        rates = load_rates_for(path, date(2026, 4, 27))
        assert rates == {"RUR": 1.0, "RUB": 1.0}

    def test_ignores_zero_nominal_rates(self, tmp_path: Path):
        path = tmp_path / "rates.parquet"
        write_rates(
            [
                CBRRate(date(2026, 4, 27), "USD", 0, 92.5),
                CBRRate(date(2026, 4, 27), "EUR", 1, 100.25),
            ],
            path,
        )

        rates = load_rates_for(path, date(2026, 4, 27))

        assert "USD" not in rates
        assert rates["EUR"] == 100.25

    def test_warns_when_rates_older_than_threshold(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """KM re-audit 2026-05-17 P1: stale-rate silent degradation flagged."""
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 4, 1), "USD", 1, 92.5)], path)
        caplog.set_level("WARNING", logger="src.ingest.cbr_rates")
        load_rates_for(path, date(2026, 4, 27))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "expected stale-rates warning"
        assert "stale" in warnings[0].getMessage()
        assert "26 days" in warnings[0].getMessage()

    def test_no_warning_for_fresh_rates(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = tmp_path / "rates.parquet"
        write_rates([CBRRate(date(2026, 4, 25), "USD", 1, 92.5)], path)
        caplog.set_level("WARNING", logger="src.ingest.cbr_rates")
        load_rates_for(path, date(2026, 4, 27))
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []


def test_utc_today_returns_date():
    assert isinstance(utc_today(), date)
