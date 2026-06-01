"""TDD для salary normalization (Phase 5)."""
from __future__ import annotations

from src.enrich.salary_norm import extract_salary_rub, normalize_to_rub


RATES = {"RUR": 1.0, "RUB": 1.0, "USD": 92.5, "EUR": 100.25, "GBP": 115.0}


class TestNormalizeToRub:
    def test_none_amount_returns_none(self):
        assert normalize_to_rub(None, "RUR", RATES) is None

    def test_none_currency_returns_none(self):
        assert normalize_to_rub(100000, None, RATES) is None

    def test_unknown_currency_returns_none(self):
        assert normalize_to_rub(100, "ZWL", RATES) is None

    def test_rur_passthrough(self):
        assert normalize_to_rub(100000, "RUR", RATES) == 100000
        assert normalize_to_rub(100000, "RUB", RATES) == 100000

    def test_usd_conversion_rounded(self):
        assert normalize_to_rub(1000, "USD", RATES) == 92500

    def test_eur_conversion_rounded(self):
        assert normalize_to_rub(2500, "EUR", RATES) == int(round(2500 * 100.25))

    def test_lowercase_currency(self):
        assert normalize_to_rub(1000, "usd", RATES) == 92500

    def test_invalid_amount_string_returns_none(self):
        """Currency валидна + rate известна, но amount не парсится в float →
        ValueError → catch → None (lines 34-35)."""
        assert normalize_to_rub("not-a-number", "RUB", RATES) is None

    def test_invalid_amount_object_returns_none(self):
        """Object, который не приводится к float → TypeError → catch (lines 34-35)."""
        assert normalize_to_rub(object(), "RUB", RATES) is None


class TestExtractSalaryRub:
    def test_shards_compensation_rur_uses_from_to(self):
        item = {
            "compensation": {
                "currencyCode": "RUR",
                "from": 100_000,
                "to": 200_000,
                "perModeFrom": 1_000,
                "perModeTo": 2_000,
                "mode": "MONTH",
            }
        }
        assert extract_salary_rub(item, RATES) == (100_000, 200_000)

    def test_shards_compensation_falls_back_to_from_to(self):
        item = {"compensation": {"currencyCode": "RUR", "from": 90_000, "to": 150_000}}
        assert extract_salary_rub(item, RATES) == (90_000, 150_000)

    def test_shards_compensation_usd_converted(self):
        item = {"compensation": {"currencyCode": "USD", "from": 3000, "to": 5000}}
        assert extract_salary_rub(item, RATES) == (3000 * 92, 5000 * 92) or extract_salary_rub(
            item, RATES
        ) == (int(3000 * 92.5), int(5000 * 92.5))

    def test_shards_hour_mode_uses_monthly_from_to_not_per_mode(self):
        item = {
            "compensation": {
                "currencyCode": "RUR",
                "from": 102_859,
                "to": 359_640,
                "perModeFrom": 600,
                "perModeTo": 1_500,
                "mode": "HOUR",
                "frequency": "MONTHLY",
            }
        }
        assert extract_salary_rub(item, RATES) == (102_859, 359_640)

    def test_shards_only_from_no_to(self):
        item = {"compensation": {"currencyCode": "RUR", "from": 100_000}}
        result = extract_salary_rub(item, RATES)
        assert result[0] == 100_000
        assert result[1] is None

    def test_shards_only_to_no_from(self):
        item = {"compensation": {"currencyCode": "RUR", "to": 250_000}}
        result = extract_salary_rub(item, RATES)
        assert result[0] is None
        assert result[1] == 250_000

    def test_api_shape_rur(self):
        item = {"salary": {"currency": "RUR", "from": 150_000, "to": 250_000}}
        assert extract_salary_rub(item, RATES) == (150_000, 250_000)

    def test_api_shape_eur(self):
        item = {"salary": {"currency": "EUR", "from": 2000, "to": 3000}}
        result = extract_salary_rub(item, RATES)
        assert result[0] == int(round(2000 * 100.25))
        assert result[1] == int(round(3000 * 100.25))

    def test_api_shape_no_salary_returns_none(self):
        assert extract_salary_rub({"id": "123"}, RATES) == (None, None)

    def test_api_shape_empty_salary_returns_none(self):
        assert extract_salary_rub({"salary": None}, RATES) == (None, None)

    def test_unknown_currency_returns_none(self):
        item = {"compensation": {"currencyCode": "ZWL", "from": 100_000}}
        assert extract_salary_rub(item, RATES) == (None, None)
