"""Convert vacancy compensation → salary_rub_min/max через ЦБ rates.

Источники salary в lake:
- shards-shape: `compensation.{from,to,currencyCode,perModeFrom,perModeTo,frequency,mode,gross}`
- api.hh.ru-shape: `salary.{from,to,currency,gross}`

Выбор field:
- from/to — comparable payout range from hh search payload
- perModeFrom/perModeTo (shards) — amount per selected mode (hour/shift/etc);
  do not use for monthly salary aggregates

Currency:
- RUR/RUB → 1:1
- USD/EUR/etc — RUB через CBR rate (RUB per 1 unit)
- неизвестная валюта или нет rate → NULL

Возвращаем (min_rub, max_rub) — int RUB, или (None, None) если данных нет.
"""
from __future__ import annotations


def normalize_to_rub(amount: float | int | None, currency: str | None, rates: dict[str, float]) -> int | None:
    """Convert amount in <currency> → RUB int. None если currency не известна."""
    if amount is None:
        return None
    if not currency:
        return None
    rate = rates.get(currency.upper())
    if rate is None:
        return None
    try:
        return int(round(float(amount) * rate))
    except (TypeError, ValueError):
        return None


def extract_salary_rub(item: dict, rates: dict[str, float]) -> tuple[int | None, int | None]:
    """Извлечь salary_rub_min / salary_rub_max из item (api или shards shape)."""
    if "compensation" in item:
        comp = item.get("compensation") or {}
        currency = comp.get("currencyCode")
        from_val = comp.get("from")
        to_val = comp.get("to")
    else:
        sal = item.get("salary") or {}
        if not sal:
            return None, None
        currency = sal.get("currency")
        from_val = sal.get("from")
        to_val = sal.get("to")

    return (
        normalize_to_rub(from_val, currency, rates),
        normalize_to_rub(to_val, currency, rates),
    )
