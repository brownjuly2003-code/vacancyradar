"""Daily ЦБ РФ exchange rates → master/ref/cbr_rates.parquet.

Source: cbr.ru/scripts/XML_daily.asp?date_req=DD/MM/YYYY (public XML, no auth).
Schema на одну запись:
    date         Date    дата котировки (UTC взято как date)
    char_code    String  ISO 4217 (USD/EUR/CNY/...) или RUR placeholder
    nominal      Int     номинал (1, 10, 100 — для йены/тенге)
    value        Float64 рублей за nominal единиц этой валюты

Запись append-only по date+char_code (идемпотентный upsert).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

import polars as pl
import requests


CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"

# Stale-rate warning threshold. Salaries normalize against latest available
# CBR rate; >N days old = signal that ingest cbr has been failing silently.
# KM re-audit 2026-05-17 P1 (cbr_rates silent stale degradation).
STALE_RATES_WARNING_DAYS = 7

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CBRRate:
    date: date
    char_code: str
    nominal: int
    value: float


def fetch_rates(
    on: date | None = None,
    *,
    get: Callable[..., requests.Response] = requests.get,
    timeout: float = 30.0,
    retries: int = 3,
    backoff_base: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> list[CBRRate]:
    """Fetch CBR rates for a given date (default: today).

    Возвращает список CBRRate. Возвращает пустой список если ЦБ не отдал котировки
    для этой даты (выходной, праздник).

    Retries 5xx HTTPError up to `retries` times with exp backoff (0.5/1.0/2.0s).
    404 означает что котировки на дату ещё не опубликованы; остальные 4xx и
    connection errors не retry'ятся — это конфигурационная проблема.
    """
    on = on or date.today()
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = get(
                CBR_DAILY_URL,
                params={"date_req": on.strftime("%d/%m/%Y")},
                timeout=timeout,
            )
            if 500 <= response.status_code < 600:
                response.raise_for_status()
            response.raise_for_status()
            break
        except requests.HTTPError as exc:
            last_exc = exc
            status = exc.response.status_code if exc.response is not None else None
            if status == 404:
                return []
            if status is None or status < 500 or attempt == retries - 1:
                raise
            delay = backoff_base * (2**attempt)
            _logger.warning(
                "cbr fetch attempt %d/%d failed with HTTP %s; retrying in %.1fs",
                attempt + 1,
                retries,
                status,
                delay,
            )
            sleep(delay)
    else:  # pragma: no cover — defensive, loop always breaks or raises
        if last_exc is not None:
            raise last_exc

    root = ET.fromstring(response.content)

    rates: list[CBRRate] = []
    for valute in root.findall("Valute"):
        char_code = (valute.findtext("CharCode") or "").strip()
        nominal_str = (valute.findtext("Nominal") or "1").strip()
        value_str = (valute.findtext("Value") or "").strip().replace(",", ".")
        if not char_code or not value_str:
            continue
        try:
            rates.append(
                CBRRate(
                    date=on,
                    char_code=char_code,
                    nominal=int(nominal_str),
                    value=float(value_str),
                )
            )
        except ValueError:
            continue
    return rates


def write_rates(rates: list[CBRRate], parquet_path: Path) -> Path:
    """Idempotent upsert по (date, char_code). Создаёт parent dirs."""
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    new_df = pl.DataFrame(
        {
            "date": [r.date for r in rates],
            "char_code": [r.char_code for r in rates],
            "nominal": [r.nominal for r in rates],
            "value": [r.value for r in rates],
        },
        schema={
            "date": pl.Date,
            "char_code": pl.String,
            "nominal": pl.Int32,
            "value": pl.Float64,
        },
    )

    if parquet_path.exists():
        existing = pl.read_parquet(parquet_path)
        merged = (
            pl.concat([existing, new_df], how="vertical_relaxed")
            .unique(subset=["date", "char_code"], keep="last", maintain_order=True)
            .sort(["date", "char_code"])
        )
    else:
        merged = new_df.sort(["date", "char_code"])

    merged.write_parquet(parquet_path, compression="zstd", compression_level=3)
    return parquet_path


def load_rates_for(parquet_path: Path, on: date) -> dict[str, float]:
    """Load rates for date (или ближайшая прошлая дата если запрошенной нет).

    Returns: dict[char_code] → RUB per 1 unit (т.е. value/nominal).
    Если parquet не существует или нет ни одной даты ≤ on → {}.
    Включает синтетический RUR=1.0 / RUB=1.0 для удобства downstream-кода.

    Warns via stdlib logging если latest_date старше STALE_RATES_WARNING_DAYS
    (default 7): значит ingest cbr тихо не отдаёт fresh rates уже неделю.
    """
    rates: dict[str, float] = {"RUR": 1.0, "RUB": 1.0}
    if not parquet_path.exists():
        return rates

    df = pl.read_parquet(parquet_path).filter(pl.col("date") <= on)
    if df.is_empty():
        return rates

    latest_date = df["date"].max()
    if isinstance(latest_date, date):
        age_days = (on - latest_date).days
        if age_days > STALE_RATES_WARNING_DAYS:
            _logger.warning(
                "cbr rates stale: latest_date=%s, requested=%s, age=%d days "
                "(threshold=%d). Salary RUB normalization using outdated rates.",
                latest_date.isoformat(),
                on.isoformat(),
                age_days,
                STALE_RATES_WARNING_DAYS,
            )

    snapshot = df.filter(pl.col("date") == latest_date)
    for row in snapshot.iter_rows(named=True):
        if row["nominal"] > 0:
            rates[row["char_code"]] = row["value"] / row["nominal"]
    return rates


def utc_today() -> date:
    return datetime.now(timezone.utc).date()
