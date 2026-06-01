from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import polars as pl

if TYPE_CHECKING:
    from src.ingest.tg_client import TGMessage


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


_VOLATILE_SHARDS_FIELDS = frozenset({
    "responsesCount",
    "totalResponsesCount",
    "online_users_count",
    "searchRid",
    "clickUrl",
    "userLabels",
    "notify",
    "inboxPossibility",
    "chatWritePossibility",
    "@isAdv",
})


def _strip_volatile_shards(item: dict) -> dict:
    return {k: v for k, v in item.items() if k not in _VOLATILE_SHARDS_FIELDS}


@dataclass(frozen=True)
class RawRecord:
    vacancy_id: str
    source: str
    fetched_at: datetime
    posted_at: datetime | None
    employer_id: str | None
    content_hash: str
    raw_json: str
    market_scope: str | None = None
    professional_role_id: int | None = None

    @classmethod
    def from_hh_item(
        cls,
        item: dict,
        fetched_at: datetime,
        *,
        market_scope: str | None = None,
    ) -> "RawRecord":
        posted_raw = item.get("published_at") or item.get("created_at")
        posted_at = _parse_iso(posted_raw)
        employer = item.get("employer") or {}
        return cls(
            vacancy_id=f"hh:{item['id']}",
            source="hh",
            fetched_at=fetched_at,
            posted_at=posted_at,
            employer_id=str(employer.get("id")) if employer.get("id") else None,
            content_hash=content_hash(item),
            raw_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
            market_scope=market_scope,
            professional_role_id=_extract_professional_role_id(item),
        )

    @classmethod
    def from_hh_shards_item(
        cls,
        item: dict,
        fetched_at: datetime,
        *,
        market_scope: str | None = None,
    ) -> "RawRecord":
        """Build a RawRecord from hh.ru/shards/vacancy/search JSON shape.

        Shape diverges from api.hh.ru: vacancyId/company.id, publicationTime
        and creationTime are nested objects ({"@timestamp": int, "$": iso}) or
        plain ISO strings.
        """
        posted_raw = _shards_iso(item.get("publicationTime")) or _shards_iso(
            item.get("creationTime")
        )
        company = item.get("company") or {}
        company_id = company.get("id")
        return cls(
            vacancy_id=f"hh:{item['vacancyId']}",
            source="hh",
            fetched_at=fetched_at,
            posted_at=_parse_iso(posted_raw),
            employer_id=str(company_id) if company_id else None,
            content_hash=content_hash(_strip_volatile_shards(item)),
            raw_json=json.dumps(item, ensure_ascii=False, sort_keys=True),
            market_scope=market_scope,
            professional_role_id=_extract_professional_role_id(item),
        )

    @classmethod
    def from_telegram_message(
        cls,
        msg: "TGMessage",
        fetched_at: datetime,
        *,
        market_scope: str | None = None,
    ) -> "RawRecord":
        payload = {
            "channel": msg.channel,
            "message_id": msg.message_id,
            "date": msg.date.isoformat(),
            "text": msg.text,
            "views": msg.views,
        }
        return cls(
            vacancy_id=f"tg:{msg.channel}:{msg.message_id}",
            source="telegram",
            fetched_at=fetched_at,
            posted_at=msg.date,
            employer_id=None,
            content_hash=content_hash(payload),
            raw_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            market_scope=market_scope,
            professional_role_id=None,
        )


def _extract_professional_role_id(item: dict) -> int | None:
    for key in (
        "professional_roles",
        "professionalRoles",
        "professionalRoleIds",
        "professional_role",
        "professionalRole",
        "professional_role_id",
        "professionalRoleId",
    ):
        role_id = _role_id_from_value(item.get(key))
        if role_id is not None:
            return role_id
    return None


def _role_id_from_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, list):
        for item in value:
            role_id = _role_id_from_value(item)
            if role_id is not None:
                return role_id
        return None
    if isinstance(value, dict):
        return _role_id_from_value(
            value.get("id")
            or value.get("@id")
            or value.get("professionalRoleId")
            or value.get("professional_role_id")
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _shards_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        raw = value.get("$")
        return raw if isinstance(raw, str) else None
    return None


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def write_batch(records: Iterable[RawRecord], lake_root: Path) -> Path:
    records_list = list(records)
    if not records_list:
        raise ValueError("write_batch called with empty records")
    fetched_at = records_list[0].fetched_at
    for r in records_list[1:]:
        if r.fetched_at != fetched_at:
            raise ValueError("all records in a batch must share the same fetched_at")
    source = records_list[0].source
    partition = (
        lake_root
        / f"year={fetched_at.year}"
        / f"month={fetched_at.month:02d}"
        / f"source={source}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    filename = f"fetched_{int(fetched_at.timestamp())}_{uuid.uuid4().hex[:8]}.parquet"
    path = partition / filename
    df = pl.DataFrame(
        {
            "vacancy_id": [r.vacancy_id for r in records_list],
            "source": [r.source for r in records_list],
            "fetched_at": [r.fetched_at for r in records_list],
            "posted_at": [r.posted_at for r in records_list],
            "employer_id": [r.employer_id for r in records_list],
            "content_hash": [r.content_hash for r in records_list],
            "raw_json": [r.raw_json for r in records_list],
            "market_scope": [r.market_scope for r in records_list],
            "professional_role_id": [r.professional_role_id for r in records_list],
        }
    )
    df.write_parquet(path, compression="zstd")
    return path


_LAKE_SCHEMA: dict[str, Any] = {
    "vacancy_id": pl.String,
    "source": pl.String,
    "fetched_at": pl.Datetime("us", "UTC"),
    "posted_at": pl.Datetime("us", "UTC"),
    "employer_id": pl.String,
    "content_hash": pl.String,
    "raw_json": pl.String,
    "market_scope": pl.String,
    "professional_role_id": pl.Int64,
}


def scan_lake(
    lake_root: Path,
    source: str | None = None,
    *,
    columns: list[str] | None = None,
) -> pl.LazyFrame:
    """Lazy scan of entire raw lake — projection pushdown happens во время
    `.collect()`, поэтому колонки которые caller не select'ит (часто
    `raw_json`, ~95% размера на disk) физически не читаются.

    Returns empty LazyFrame с фиксированной schema если файлов нет.
    """
    if source is not None:
        glob_pattern = f"**/source={source}/*.parquet"
        glob_str = str(lake_root / "**" / f"source={source}" / "*.parquet")
    else:
        glob_pattern = "**/*.parquet"
        glob_str = str(lake_root / "**" / "*.parquet")

    if not lake_root.exists() or next(lake_root.glob(glob_pattern), None) is None:
        empty_schema = (
            _LAKE_SCHEMA if columns is None else {k: v for k, v in _LAKE_SCHEMA.items() if k in columns}
        )
        return pl.LazyFrame(schema=empty_schema)

    lf = pl.scan_parquet(
        glob_str,
        schema=_LAKE_SCHEMA,
        missing_columns="insert",
        extra_columns="ignore",
    )
    if columns is not None:
        lf = lf.select(columns)
    return lf


def read_lake(
    lake_root: Path,
    source: str | None = None,
    *,
    columns: list[str] | None = None,
) -> pl.DataFrame:
    """Eager read of entire raw lake — thin wrapper над `scan_lake().collect()`.

    `columns` enables projection pushdown (huge win when caller doesn't
    need raw_json).
    """
    try:
        df = scan_lake(lake_root, source=source, columns=columns).collect()
    except (FileNotFoundError, pl.exceptions.ComputeError):
        df = pl.DataFrame()
    if df.is_empty():
        empty_schema = {k: v for k, v in _LAKE_SCHEMA.items() if columns is None or k in columns}
        return pl.DataFrame(schema=empty_schema)
    return df


def latest_snapshot(lake_root: Path, source: str = "hh") -> pl.DataFrame:
    lf = scan_lake(lake_root, source=source)
    return (
        lf.with_row_index("_row_order")
        .sort(["fetched_at", "_row_order"])
        .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
        .drop("_row_order")
        .sort("vacancy_id")
        .collect()
    )


_META_COLUMNS = ["vacancy_id", "employer_id", "content_hash", "fetched_at"]


def latest_snapshot_meta(
    lake_root: Path,
    source: str = "hh",
    *,
    market_scope: str | None = None,
) -> pl.DataFrame:
    """Snapshot БЕЗ `raw_json` — для diff identification.

    raw_json — самая тяжёлая колонка (~95% размера на disk + GB+ RSS на
    421k уникальных вакансий). Когда нам нужен только diff по
    content_hash, он не должен загружаться в память. Сначала делаем meta
    snapshot, потом вызываем `load_raw_json_for(changed_ids)` точечно.
    """
    columns = list(_META_COLUMNS)
    if market_scope is not None:
        columns.append("market_scope")
    lf = scan_lake(lake_root, source=source, columns=columns)
    if market_scope is not None:
        lf = lf.filter(pl.col("market_scope") == market_scope)
    drop_columns = ["_row_order", "fetched_at"]
    if market_scope is not None:
        drop_columns.append("market_scope")
    return (
        lf.with_row_index("_row_order")
        .sort(["fetched_at", "_row_order"])
        .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
        .drop(*drop_columns)
        .sort("vacancy_id")
        .collect()
    )


def load_raw_json_for(
    lake_root: Path,
    vacancy_ids: Iterable[str],
    source: str = "hh",
) -> pl.DataFrame:
    """Подгрузить raw_json для конкретного множества vacancy_id (latest fetched).

    Pair с `latest_snapshot_meta` — после diff identification
    подгружаем raw_json только для тех vacancies, которые реально нужны
    `_classify_change` (изменившиеся content_hash). Для appeared/closed
    raw_json не нужен в `derive_events`.
    """
    ids_list = list(vacancy_ids)
    if not ids_list:
        return pl.DataFrame(schema={"vacancy_id": pl.String, "raw_json": pl.String})

    lf = scan_lake(
        lake_root,
        source=source,
        columns=["vacancy_id", "raw_json", "fetched_at"],
    )
    return (
        lf.filter(pl.col("vacancy_id").is_in(ids_list))
        .with_row_index("_row_order")
        .sort(["fetched_at", "_row_order"])
        .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
        .drop("_row_order", "fetched_at")
        .sort("vacancy_id")
        .collect()
    )


def snapshot_at(lake_root: Path, fetched_at: datetime, source: str = "hh") -> pl.DataFrame:
    """Snapshot — все записи с fetched_at <= given timestamp, latest per vacancy_id."""
    lf = scan_lake(lake_root, source=source)
    return (
        lf.filter(pl.col("fetched_at") <= fetched_at)
        .with_row_index("_row_order")
        .sort(["fetched_at", "_row_order"])
        .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
        .drop("_row_order")
        .sort("vacancy_id")
        .collect()
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
