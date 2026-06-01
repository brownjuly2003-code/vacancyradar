"""TTL-pass для Vercel Blob партиций `slim/events_30d/`.

Фон: `publish events` чистит локальный `derived/slim_events_30d/` перед
write, но stale партиции, ранее загруженные на Vercel Blob, остаются.
DuckDB httpfs читает их через wildcard glob и нарушает invariant контракта
events-30d-v1: `ts ∈ [now()-30d, now()]`.

Этот модуль использует Vercel Blob HTTP API:
- list: `GET {api_base}/?prefix=...&limit=...&cursor=...`
- del:  `POST {api_base}/delete` body `{"urls": [...]}`

(Reverse-engineered из @vercel/blob npm SDK; не публичный s3.)

Поведение по умолчанию консервативное:
- pathname без распознанной партиции date НИКОГДА не удаляется
- prefix фильтр строго `slim/events_30d/`
- dry_run=True по умолчанию в `prune_events_30d`; CLI выставляет False явно
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable

import requests

from src.publish.blob_push import BlobConfig


_PARTITION_RE = re.compile(r"year=(\d{4})/month=(\d{2})/day=(\d{2})/")
EVENTS_PREFIX = "slim/events_30d/"


@dataclass(frozen=True)
class BlobMeta:
    pathname: str
    url: str
    size: int | None
    uploaded_at: str | None


@dataclass(frozen=True)
class PruneResult:
    kept: int
    pruned: int
    pruned_pathnames: list[str]
    skipped_unparseable: list[str]
    dry_run: bool
    cutoff: date


def parse_partition_date(pathname: str) -> date | None:
    """Извлечь дату из Hive layout `.../year=YYYY/month=MM/day=DD/...`."""
    m = _PARTITION_RE.search(pathname)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def list_blobs(
    prefix: str,
    cfg: BlobConfig,
    *,
    get: Callable[..., requests.Response] = requests.get,
) -> list[BlobMeta]:
    """List all blobs under prefix. Paginates by cursor."""
    out: list[BlobMeta] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"prefix": prefix, "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        response = get(
            cfg.api_base,
            params=params,
            headers={"Authorization": f"Bearer {cfg.token}"},
            timeout=cfg.timeout,
        )
        response.raise_for_status()
        data = response.json() or {}
        for b in data.get("blobs", []):
            out.append(
                BlobMeta(
                    pathname=b.get("pathname", ""),
                    url=b.get("url", ""),
                    size=b.get("size"),
                    uploaded_at=b.get("uploadedAt"),
                )
            )
        if data.get("hasMore") and data.get("cursor"):
            cursor = data["cursor"]
            continue
        break
    return out


def delete_blobs(
    urls: list[str],
    cfg: BlobConfig,
    *,
    post: Callable[..., requests.Response] = requests.post,
) -> None:
    """Bulk-delete blobs by URL. No-op для пустого списка."""
    if not urls:
        return
    response = post(
        f"{cfg.api_base}/delete",
        json={"urls": urls},
        headers={"Authorization": f"Bearer {cfg.token}"},
        timeout=cfg.timeout,
    )
    response.raise_for_status()


def prune_events_30d(
    cfg: BlobConfig,
    *,
    today: date,
    keep_days: int = 30,
    dry_run: bool = True,
    list_fn: Callable[[], list[BlobMeta]] | None = None,
    delete_fn: Callable[[list[str]], None] | None = None,
) -> PruneResult:
    """Удалить партиции с date < (today - keep_days). Партиции, чью дату нельзя
    распарсить из pathname, НЕ удаляются (safety).

    dry_run=True — только посчитать что должно быть удалено, без HTTP delete.
    """
    list_fn = list_fn or (lambda: list_blobs(EVENTS_PREFIX, cfg))
    delete_fn = delete_fn or (lambda urls: delete_blobs(urls, cfg))

    blobs = list_fn()
    cutoff = today - timedelta(days=keep_days)

    kept: list[BlobMeta] = []
    to_prune: list[BlobMeta] = []
    skipped_unparseable: list[str] = []

    for blob in blobs:
        partition_date = parse_partition_date(blob.pathname)
        if partition_date is None:
            skipped_unparseable.append(blob.pathname)
            continue
        if partition_date < cutoff:
            to_prune.append(blob)
        else:
            kept.append(blob)

    if not dry_run and to_prune:
        delete_fn([b.url for b in to_prune])

    return PruneResult(
        kept=len(kept),
        pruned=len(to_prune),
        pruned_pathnames=[b.pathname for b in to_prune],
        skipped_unparseable=skipped_unparseable,
        dry_run=dry_run,
        cutoff=cutoff,
    )
