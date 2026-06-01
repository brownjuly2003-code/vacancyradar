from __future__ import annotations

import json
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ingest.hh_shards import (
    HHShardsClient,
    HHShardsConfig,
    HHTransientError,
    RateLimited,
    extract_vacancies,
)
from src.ingest.raw_lake import RawRecord, write_batch
from src.ingest.refdata import (
    fetch_areas,
    fetch_professional_roles,
    load_areas_yaml,
    load_roles_yaml,
    russia_subjects,
    save_areas_yaml,
    save_roles_yaml,
)

ROLES_PATH = Path("data/professional_roles.yaml")
AREAS_PATH = Path("data/areas.yaml")
PERIODS = [1, 7, 30, 180]
SCHEDULES = ["fullDay", "shift", "flexible", "remote", "flyInFlyOut"]
HH_SHARDS_CAP = 10_000


@dataclass(frozen=True)
class Segment:
    area: int
    professional_role: int | None
    period: int | None
    schedule: str | None
    depth: int

    def to_dict(self, *, include_depth: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"area": self.area}
        if self.professional_role is not None:
            data["professional_role"] = self.professional_role
        if self.period is not None:
            data["period"] = self.period
        if self.schedule is not None:
            data["schedule"] = self.schedule
        if include_depth:
            data["depth"] = self.depth
        return data

    def search_kwargs(self) -> dict[str, Any]:
        return self.to_dict(include_depth=False)


def crawl(
    root: Segment,
    *,
    max_depth: int = 4,
    max_vacancies: int = 2_000_000,
    rate_limit_sec: float = 1.0,
    progress_path: Path,
    lake_root: Path,
    client: HHShardsClient | None = None,
) -> dict[str, Any]:
    client = client or HHShardsClient(
        HHShardsConfig(requests_per_second=1.0 / max(rate_limit_sec, 0.001))
    )
    progress = _load_progress(progress_path) or _new_progress(
        root, max_depth, max_vacancies, rate_limit_sec
    )
    areas, roles = _load_split_dimensions()
    last_log = {"at": time.monotonic()}

    def handle_sigint(signum, frame):  # noqa: ARG001
        progress["stats"]["last_update"] = _utcnow_iso()
        _save_progress_atomic(progress_path, progress)
        raise KeyboardInterrupt

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    try:
        _crawl_segment(
            root,
            client,
            progress,
            progress_path,
            lake_root,
            max_depth,
            max_vacancies,
            areas,
            roles,
            last_log,
        )
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    _save_progress_atomic(progress_path, progress)
    return progress


def _crawl_segment(
    segment: Segment,
    client: HHShardsClient,
    progress: dict[str, Any],
    progress_path: Path,
    lake_root: Path,
    max_depth: int,
    max_vacancies: int,
    areas: list[int],
    roles: list[int],
    last_log: dict[str, float],
) -> None:
    if progress["stats"]["vacancies_fetched"] >= max_vacancies:
        return
    if _is_closed(segment, progress.get("closed_segments", [])):
        return

    progress["current"] = _segment_label(segment)
    _maybe_log(progress, last_log)
    try:
        payload = client.search(**segment.search_kwargs(), page=0, per_page=100)
    except (HHTransientError, RateLimited) as exc:
        _record_failure(progress, segment, f"search: {exc}")
        progress["stats"]["last_update"] = _utcnow_iso()
        _save_progress_atomic(progress_path, progress)
        return
    except Exception as exc:
        _record_failure(progress, segment, f"search: {type(exc).__name__}: {exc}")
        progress["stats"]["last_update"] = _utcnow_iso()
        _save_progress_atomic(progress_path, progress)
        return
    progress["stats"]["requests"] += 1
    total = _total_results(payload)

    if total > HH_SHARDS_CAP:
        if segment.depth >= max_depth:
            _append_unique(progress["uncovered"], {**segment.to_dict(), "total": total})
            progress["stats"]["last_update"] = _utcnow_iso()
            _save_progress_atomic(progress_path, progress)
            return
        children = _next_dimension(segment, segment.depth, areas, roles, PERIODS, SCHEDULES)
        if not children:
            _append_unique(progress["uncovered"], {**segment.to_dict(), "total": total})
            progress["stats"]["last_update"] = _utcnow_iso()
            _save_progress_atomic(progress_path, progress)
            return
        for child in children:
            _crawl_segment(
                child,
                client,
                progress,
                progress_path,
                lake_root,
                max_depth,
                max_vacancies,
                areas,
                roles,
                last_log,
            )
        return

    try:
        fetched, drain_requests = _drain_segment(
            segment, client, lake_root, first_payload=payload
        )
    except (HHTransientError, RateLimited) as exc:
        _record_failure(progress, segment, f"drain: {exc}")
        progress["stats"]["last_update"] = _utcnow_iso()
        _save_progress_atomic(progress_path, progress)
        return
    except Exception as exc:
        _record_failure(progress, segment, f"drain: {type(exc).__name__}: {exc}")
        progress["stats"]["last_update"] = _utcnow_iso()
        _save_progress_atomic(progress_path, progress)
        return
    progress["stats"]["requests"] += drain_requests
    progress["stats"]["vacancies_fetched"] += fetched
    progress["stats"]["segments_done"] += 1
    _append_unique(progress["closed_segments"], segment.to_dict())
    progress["stats"]["last_update"] = _utcnow_iso()
    _save_progress_atomic(progress_path, progress)
    _maybe_log(progress, last_log)


def _next_dimension(
    segment: Segment,
    depth: int,
    areas: list[int],
    roles: list[int],
    periods: list[int],
    schedules: list[str],
) -> list[Segment]:
    """Order: area → period → schedule → role.

    Role split is LAST resort (depth=3) — иначе vacancies без
    professional_role в payload отсекаются на depth=1 и НЕ попадают
    ни в какой `professional_role=N` filter (≈ половина hh.ru
    vacancies на текущий момент). period/schedule filters не
    отсекают по role, так что full coverage сохраняется.
    """
    if depth == 0 and segment.area == 113:
        return [
            Segment(area=area, professional_role=None, period=None, schedule=None, depth=1)
            for area in areas
        ]
    if segment.period is None:
        return [
            Segment(
                area=segment.area,
                professional_role=segment.professional_role,
                period=period,
                schedule=None,
                depth=2,
            )
            for period in periods
        ]
    if segment.schedule is None:
        return [
            Segment(
                area=segment.area,
                professional_role=segment.professional_role,
                period=segment.period,
                schedule=schedule,
                depth=3,
            )
            for schedule in schedules
        ]
    if segment.professional_role is None:
        return [
            Segment(
                area=segment.area,
                professional_role=role,
                period=segment.period,
                schedule=segment.schedule,
                depth=4,
            )
            for role in roles
        ]
    return []


def _load_progress(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_progress_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _is_closed(segment: Segment, closed: list[dict[str, Any]]) -> bool:
    expected = segment.to_dict()
    for item in closed:
        normalized = {k: v for k, v in item.items() if k != "depth" and v is not None}
        if normalized == expected:
            return True
    return False


def _drain_segment(
    segment: Segment,
    client: HHShardsClient,
    lake_root: Path,
    *,
    first_payload: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Return (fetched_vacancies, http_requests). Page 0 уже посчитан caller'ом.

    Atomic: записывает batch в lake только после успешного drain всех страниц
    сегмента. Если 429/TLS/timeout посередине — exception bubble'ится наверх,
    batch отбрасывается, caller записывает failure. Это предотвращает дубли
    в raw lake когда следующий sweep пере-фетчит уже частично записанные
    pages 0-N (KM re-audit 2026-05-17 P1: hh_crawler mid-drain duplicate).

    Memory: max 100 pages × 100 vacancies = ~10k RawRecord per segment.
    Practical worst case ~25 MB peak.
    """
    requests = 0
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0)
    page = 0
    total = 0
    batch: list[RawRecord] = []

    while True:
        if page == 0 and first_payload is not None:
            payload = first_payload
        else:
            payload = client.search(**segment.search_kwargs(), page=page, per_page=100)
            requests += 1

        vacancies = extract_vacancies(payload)
        batch.extend(RawRecord.from_hh_shards_item(item, fetched_at) for item in vacancies)
        total += len(vacancies)

        last_page = _last_page(payload)
        if last_page is None or page >= min(last_page, 99):
            break
        page += 1

    if batch:
        write_batch(batch, lake_root)
    return total, requests


def _load_split_dimensions() -> tuple[list[int], list[int]]:
    if not AREAS_PATH.exists():
        save_areas_yaml(fetch_areas(), AREAS_PATH)
    if not ROLES_PATH.exists():
        save_roles_yaml(fetch_professional_roles(), ROLES_PATH)
    areas = [int(item["id"]) for item in russia_subjects(load_areas_yaml(AREAS_PATH))]
    roles = [
        int(role["id"])
        for category in load_roles_yaml(ROLES_PATH).get("categories", [])
        for role in category.get("roles", [])
    ]
    if not areas:
        raise ValueError("empty hh.ru area split list")
    if not roles:
        raise ValueError("empty hh.ru professional_role split list")
    return areas, roles


def _new_progress(
    root: Segment,
    max_depth: int,
    max_vacancies: int,
    rate_limit_sec: float,
) -> dict[str, Any]:
    now = _utcnow_iso()
    return {
        "started_at": now,
        "root": root.to_dict(),
        "max_depth": max_depth,
        "max_vacancies": max_vacancies,
        "rate_limit_sec": rate_limit_sec,
        "closed_segments": [],
        "uncovered": [],
        "failed_segments": [],
        "stats": {
            "requests": 0,
            "vacancies_fetched": 0,
            "segments_done": 0,
            "last_update": now,
        },
    }


def _total_results(payload: dict[str, Any]) -> int:
    vsr = payload.get("vacancySearchResult") or {}
    total = vsr.get("totalResults")
    return int(total) if total is not None else len(extract_vacancies(payload))


def _last_page(payload: dict[str, Any]) -> int | None:
    vsr = payload.get("vacancySearchResult") or {}
    paging = vsr.get("paging") or {}
    last = paging.get("lastPage") or {}
    page = last.get("page")
    return int(page) if page is not None else None


def _append_unique(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    if item not in items:
        items.append(item)


def _record_failure(progress: dict[str, Any], segment: Segment, reason: str) -> None:
    progress.setdefault("failed_segments", []).append(
        {**segment.to_dict(), "reason": reason, "at": _utcnow_iso()}
    )


def _maybe_log(progress: dict[str, Any], last_log: dict[str, float]) -> None:
    now = time.monotonic()
    if now - last_log["at"] < 30:
        return
    stats = progress["stats"]
    elapsed = _format_elapsed(progress.get("started_at"))
    print(
        "[crawler] "
        f"elapsed={elapsed} | requests={stats['requests']} | "
        f"vacancies={stats['vacancies_fetched']} | "
        f"segments_done={stats['segments_done']} | "
        f"current={progress.get('current', '')}"
    )
    last_log["at"] = now


def _format_elapsed(started_at: str | None) -> str:
    if not started_at:
        return "0m"
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    seconds = max(int((datetime.now(timezone.utc) - started).total_seconds()), 0)
    minutes = seconds // 60
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60}m"


def _segment_label(segment: Segment) -> str:
    parts = [f"area={segment.area}"]
    if segment.professional_role is not None:
        parts.append(f"role={segment.professional_role}")
    if segment.period is not None:
        parts.append(f"period={segment.period}")
    if segment.schedule is not None:
        parts.append(f"schedule={segment.schedule}")
    return ",".join(parts)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
