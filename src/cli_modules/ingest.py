"""`vradar ingest {hh,hh-crawl,telegram,cbr}` impls.

Extracted from src/cli.py per Kimi audit P1-1. Shared helpers
(`_resolve_hh_scope_role_ids`, `_write_hh_completed_sweep_state`,
`_scoped_telegram_channels`, `_load_settings_for_cwd`) stay in src/cli.py so
tests' `monkeypatch.setattr(cli, "_resolve_hh_scope_role_ids", ...)` (and
similar) keep working — this module does function-scope
`from src.cli import _resolve_hh_scope_role_ids` to honor monkey-patched
attributes at call time.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# --- full-sweep segmentation (hh shards) -----------------------------------
# hh shards serves at most ~2000 items per query (lastPage caps at
# 2000/per_page regardless of page size; pages beyond it return an error
# page — verified live 2026-06-05). Roles above the window are drained via
# disjoint sub-segments: `experience` first (required single-valued field,
# bucket totals sum exactly to the role total), then an area partition
# (Moscow / SPb / all other RF subjects in one multi-area query). `schedule`
# and `employment` were probed and rejected (multi-valued / barely cut);
# `date_from`/`date_to`/`period` are ignored by shards.
_EXPERIENCE_BUCKETS: tuple[str, ...] = (
    "noExperience",
    "between1And3",
    "between3And6",
    "moreThan6",
)
_MOSCOW_AREA_ID = 1
_SPB_AREA_ID = 2
_RUSSIA_AREA_ID = 113
# Safety page cap per segment: the window is ~2000/per_page pages today; 120
# only guards against a pathological lastPage so a server bug can't loop us.
_FULL_SWEEP_MAX_PAGES = 120


def _drain_shards_segment(
    client: Any,
    search_kwargs: dict,
    items: list[dict],
    label: str,
) -> tuple[bool, int]:
    """Drain one shards query to its lastPage, appending into `items`.

    Returns (oversized, pages_done). oversized=True means page 0 shows
    totalResults beyond the reachable window ((lastPage+1) × per_page): the
    caller must split the segment into finer ones. Page-0 items are kept even
    then — duplicates collapse downstream (seen_current_ids / unique()).
    HHTransientError/RateLimited propagate to the caller.
    """
    from src.ingest.hh_shards import extract_vacancies

    pages = 0
    per_page = int(search_kwargs.get("per_page") or 50)
    first_total: int | None = None
    for page_num, data in enumerate(
        client.iter_pages(start_page=0, max_pages=_FULL_SWEEP_MAX_PAGES, **search_kwargs)
    ):
        page_items = extract_vacancies(data)
        items.extend(page_items)
        pages += 1
        if page_num == 0:
            vsr = data.get("vacancySearchResult") or {}
            total_raw = vsr.get("totalResults")
            last_obj = (vsr.get("paging") or {}).get("lastPage") or {}
            last_raw = last_obj.get("page") if isinstance(last_obj, dict) else None
            first_total = int(total_raw) if total_raw is not None else None
            if first_total is not None and last_raw is not None:
                reachable = (int(last_raw) + 1) * per_page
                if first_total > reachable:
                    print(
                        f"[sweep] {label}: total={first_total} > window={reachable} "
                        "— segmenting"
                    )
                    return True, pages
    print(f"[sweep] {label}: drained {pages} pages (total={first_total})")
    return False, pages


def _full_sweep_role(
    client: Any,
    *,
    base_kwargs: dict,
    role_label: str,
    rest_areas: Callable[[], list[int]],
) -> tuple[list[dict], bool, int, int]:
    """Drain one role completely, auto-segmenting window-capped queries.

    Returns (items, role_complete, pages_done, transient_skips).
    role_complete=False on any transient skip or any leaf still capped after
    max segmentation — the detect-closed guard then refuses the partial sweep
    exactly like the non-sweep path.
    """
    from src.ingest.hh_shards import HHTransientError, RateLimited

    items: list[dict] = []
    pages_total = 0
    transient_skips = 0

    def attempt(kwargs: dict, label: str) -> bool | None:
        """True=drained, False=window-capped (split me), None=transient."""
        nonlocal pages_total, transient_skips
        try:
            oversized, pages = _drain_shards_segment(client, kwargs, items, label)
        except (HHTransientError, RateLimited) as exc:
            print(
                f"[warn] hh shards transient error {label}, skipping segment: {exc}",
                file=sys.stderr,
            )
            transient_skips += 1
            return None
        pages_total += pages
        return not oversized

    outcome = attempt(base_kwargs, role_label)
    if outcome is None:
        return items, False, pages_total, transient_skips
    if outcome:
        return items, True, pages_total, transient_skips

    role_complete = True
    for exp in _EXPERIENCE_BUCKETS:
        exp_kwargs = {**base_kwargs, "experience": exp}
        exp_label = f"{role_label} experience={exp}"
        exp_outcome = attempt(exp_kwargs, exp_label)
        if exp_outcome:
            continue
        if exp_outcome is None:
            role_complete = False
            continue
        if base_kwargs.get("area") != _RUSSIA_AREA_ID:
            print(
                f"[warn] uncovered hh segment {exp_label}: "
                "area partition requires --area 113",
                file=sys.stderr,
            )
            role_complete = False
            continue
        try:
            rest = rest_areas()
        except Exception as exc:  # noqa: BLE001 — refdata load/fetch failure
            print(
                f"[warn] uncovered hh segment {exp_label}: area split unavailable ({exc})",
                file=sys.stderr,
            )
            role_complete = False
            continue

        def drain_area_subset(
            area_ids: list[int],
            exp_label: str = exp_label,
            exp_kwargs: dict = exp_kwargs,
        ) -> bool:
            """Drain the experience bucket within area_ids, bisecting on overflow.

            Junior-heavy roles overflow even the rest-of-RF bucket (live
            2026-06-05: role=121 noExperience rest[86] > window), so the area
            list splits recursively until every leaf fits. A single area that
            still overflows (e.g. Moscow for a future fatter market) cannot
            be split further — uncovered warn, role incomplete.
            """
            area_value: int | list[int] = area_ids[0] if len(area_ids) == 1 else area_ids
            label = (
                f"{exp_label} area={area_ids[0]}"
                if len(area_ids) == 1
                else f"{exp_label} areas[{area_ids[0]}..{area_ids[-1]}#{len(area_ids)}]"
            )
            outcome = attempt({**exp_kwargs, "area": area_value}, label)
            if outcome is True:
                return True
            if outcome is None:
                return False
            if len(area_ids) == 1:
                print(
                    f"[warn] uncovered hh leaf {label}: "
                    "single area still window-capped",
                    file=sys.stderr,
                )
                return False
            mid = len(area_ids) // 2
            left_ok = drain_area_subset(area_ids[:mid])
            right_ok = drain_area_subset(area_ids[mid:])
            return left_ok and right_ok

        for area_subset in ([_MOSCOW_AREA_ID], [_SPB_AREA_ID], rest):
            if not drain_area_subset(area_subset):
                role_complete = False
    return items, role_complete, pages_total, transient_skips


def _ingest(args: argparse.Namespace) -> int:
    if args.source == "hh":
        return _ingest_hh(args)
    if args.source == "hh-crawl":
        return _ingest_hh_crawl(args)
    if args.source == "telegram":
        return _ingest_telegram(args)
    if args.source == "cbr":
        return _ingest_cbr(args)
    return 1


def _ingest_telegram(args: argparse.Namespace) -> int:
    import yaml
    from dotenv import load_dotenv
    from telethon.errors import FloodWaitError

    from src.cli import _load_settings_for_cwd, _scoped_telegram_channels
    from src.ingest.raw_lake import RawRecord, utcnow, write_batch
    from src.ingest.tg_client import TGSessionError, fetch_channel_messages, open_session

    load_dotenv()
    channel_start = getattr(args, "channel_start", 0)
    if channel_start < 0:
        print("[err] --channel-start must be >= 0", file=sys.stderr)
        return 2
    if args.channels is not None and args.channels < 1:
        print("[err] --channels must be >= 1", file=sys.stderr)
        return 2
    scope_name = getattr(args, "scope", None)
    if args.dry:
        if scope_name:
            try:
                scope_channels = _scoped_telegram_channels(scope_name, args.channel_file)
            except (FileNotFoundError, ValueError) as exc:
                print(f"[err] {exc}", file=sys.stderr)
                return 2
            channel_end = channel_start + args.channels if args.channels else None
            channels = scope_channels[channel_start:channel_end]
            sample = ", ".join(f"@{ch['username']}" for ch in channels[:20])
            print(
                f"[dry] tg ingest scope={scope_name} channel_start={channel_start} "
                f"channels={len(channels)} total_scope_channels={len(scope_channels)} "
                f"channel_file={args.channel_file} limit={args.limit} selected={sample}"
            )
            return 0
        print(
            f"[dry] tg ingest channel_start={channel_start} channels={args.channels} "
            f"channel_file={args.channel_file} limit={args.limit}"
        )
        return 0

    if getattr(args, "channel_file", None):
        all_channels = [
            {"username": line.strip().lstrip("@")}
            for line in Path(args.channel_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    else:
        channels_path = Path("data/tg_channels.yaml")
        data = yaml.safe_load(channels_path.read_text(encoding="utf-8"))
        all_channels = data.get("channels", data) if isinstance(data, dict) else data
    if scope_name and not getattr(args, "channel_file", None):
        settings = _load_settings_for_cwd()
        scope = settings.market.require_scope(scope_name)
        all_channels = scope.telegram.filter_channels(all_channels)
    channel_end = channel_start + args.channels if args.channels else None
    channels = all_channels[channel_start:channel_end]

    fetched_at = utcnow()
    records: list[RawRecord] = []
    failed: list[tuple[int, str, str]] = []
    flood_wait_seconds: int | None = None
    flood_wait_index: int | None = None
    attempted = 0
    succeeded = 0
    consecutive_network_failures = 0
    max_consecutive_network_failures = 3

    try:
        client = open_session()
    except TGSessionError as exc:
        # Distinct exit code (3) so daily_refresh.ps1 surfaces auth/session
        # issues separately from FloodWait (75) or per-channel failures (76).
        # KM audit 2026-05-17 P1: silent zero-ingest after session revoke.
        print(f"[tg][auth-error] {exc}", file=sys.stderr)
        return 3
    try:
        for offset, ch in enumerate(channels):
            username = ch["username"]
            attempted += 1
            try:
                msgs = fetch_channel_messages(client, username, limit=args.limit)
                records.extend(
                    RawRecord.from_telegram_message(m, fetched_at, market_scope=scope_name)
                    for m in msgs
                )
                succeeded += 1
                consecutive_network_failures = 0
                print(f"[tg] @{username}: {len(msgs)} messages")
            except FloodWaitError as exc:
                flood_wait_seconds = int(exc.seconds)
                flood_wait_index = channel_start + offset
                print(
                    f"[tg] @{username}: FLOOD_WAIT {flood_wait_seconds}s — stopping run",
                    file=sys.stderr,
                )
                break
            except (ConnectionError, OSError, TimeoutError) as exc:
                consecutive_network_failures += 1
                failed.append((channel_start + offset, username, str(exc)))
                print(f"[tg] @{username}: FAIL ({exc})", file=sys.stderr)
                if consecutive_network_failures >= max_consecutive_network_failures:
                    print(
                        "[tg] aborting after "
                        f"{consecutive_network_failures} consecutive network failures",
                        file=sys.stderr,
                    )
                    break
            except Exception as exc:  # noqa: BLE001
                consecutive_network_failures = 0
                failed.append((channel_start + offset, username, str(exc)))
                print(f"[tg] @{username}: FAIL ({exc})", file=sys.stderr)
    finally:
        client.disconnect()

    if records:
        from src.ingest.raw_lake import latest_snapshot_meta
        from src.transform.events_derivation import append_events, derive_events

        lake_root = Path("master/vacancies_raw.parquet")
        events_db = Path("master/events.duckdb")

        # Snapshot previous-state IDs for raw lake source="telegram" BEFORE writing the new
        # batch — otherwise the latest snapshot would already include the
        # current records and every appeared event would collapse into
        # desc_changed/no-op. Matches the HH ingest ordering (`load_raw_json_for`
        # is called pre-`write_batch` for the same reason).
        previous_meta = latest_snapshot_meta(lake_root, source="telegram")
        import polars as pl  # local import keeps cold-CLI imports lean

        current = (
            pl.DataFrame(
                {
                    "vacancy_id": [r.vacancy_id for r in records],
                    "employer_id": [r.employer_id for r in records],
                    "content_hash": [r.content_hash for r in records],
                    "raw_json": [r.raw_json for r in records],
                }
            )
            .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
            .sort("vacancy_id")
        )
        # Limit prev → curr_ids only; TG ingest is partial-sweep (channels start
        # offset, daily 5-message slice per channel) so vacancy_ids missing from
        # `current` are NOT closed, just out of scope. Mirrors HH behavior when
        # `--detect-closed` is off (it has no flag here yet — TG sweep is
        # always partial).
        if not previous_meta.is_empty():
            previous_meta = previous_meta.filter(
                pl.col("vacancy_id").is_in(current["vacancy_id"].to_list())
            )
        previous = previous_meta.with_columns(
            pl.lit(None, dtype=pl.String).alias("raw_json")
        )

        path = write_batch(records, lake_root)
        print(f"[tg-lake] {len(records)} records → {path}")

        events = derive_events(previous, current, fetched_at, source="tg")
        if events.is_empty():
            print("[tg-events] no diff — first run or all duplicates")
        else:
            appended = append_events(events, events_db)
            type_counts = (
                events.group_by("type").agg(pl.len().alias("n")).sort("type").to_dicts()
            )
            summary = ", ".join(f"{t['type']}={t['n']}" for t in type_counts)
            print(f"[tg-events] +{appended} → {events_db} ({summary})")
    if failed:
        failed_log = Path("master/run_state/tg_failed.jsonl")
        failed_log.parent.mkdir(parents=True, exist_ok=True)
        ts = fetched_at.isoformat()
        with failed_log.open("a", encoding="utf-8") as f:
            for idx, username, reason in failed:
                f.write(
                    json.dumps(
                        {"ts": ts, "index": idx, "username": username, "error": reason},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(
            f"[tg] {len(failed)} channels failed → {failed_log}",
            file=sys.stderr,
        )
    print(
        f"[done] tg ingest: {len(records)} messages from {succeeded} channels; "
        f"attempted={attempted}/{len(channels)}"
    )
    if flood_wait_index is not None:
        resume_index = flood_wait_index
    else:
        resume_index = channel_start + attempted
    print(f"[tg] resume with --channel-start {resume_index}")

    # Persist resume state так чтобы daily_refresh мог продолжить с правильного
    # offset на следующий run (KM re-audit 2026-05-17 P1). Без этого FloodWait
    # на channel 50 → next day start from 0 → re-hit того же FloodWait.
    resume_path = Path("master/run_state/tg_resume.json")
    resume_path.parent.mkdir(parents=True, exist_ok=True)
    if flood_wait_seconds is not None:
        retry_after_ts = datetime.now(timezone.utc).timestamp() + flood_wait_seconds
        resume_path.write_text(
            json.dumps(
                {
                    "resume_index": resume_index,
                    "wait_seconds": flood_wait_seconds,
                    "retry_after_epoch": int(retry_after_ts),
                    "stopped_at": fetched_at.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"[tg] stopped on FloodWait; retry after ~{flood_wait_seconds}s", file=sys.stderr)
        return 75

    # Successful completion (или per-channel failures без FloodWait): сбросить
    # resume state, чтобы next run начинал с 0.
    if resume_path.exists():
        try:
            resume_path.unlink()
        except OSError:
            pass

    if failed:
        return 76
    return 0


def _ingest_hh_crawl(args: argparse.Namespace) -> int:
    from src.ingest.hh_crawler import crawl
    from src.ingest.hh_shards import HHShardsClient, HHShardsConfig

    root = _parse_hh_crawl_root(args.root)
    progress_path = Path("master/crawl_progress.json")
    if args.reset and progress_path.exists():
        progress_path.unlink()
    if args.dry:
        print(
            f"[dry] hh-crawl root={root.to_dict()} max_depth={args.max_depth} "
            f"rate={args.rate}s → {progress_path}"
        )
        return 0

    client = HHShardsClient(
        HHShardsConfig(requests_per_second=1.0 / max(args.rate, 0.001))
    )
    progress = crawl(
        root,
        max_depth=args.max_depth,
        max_vacancies=args.max_vacancies,
        rate_limit_sec=args.rate,
        progress_path=progress_path,
        lake_root=Path("master/vacancies_raw.parquet"),
        client=client,
    )
    stats = progress["stats"]
    print(
        f"[crawler] done requests={stats['requests']} "
        f"vacancies={stats['vacancies_fetched']} segments_done={stats['segments_done']}"
    )
    return 0


def _parse_hh_crawl_root(raw: str):
    from src.ingest.hh_crawler import Segment

    values: dict[str, str] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise SystemExit(f"[err] invalid --root part: {part}")
        key, value = part.split("=", 1)
        values[key.strip()] = value.strip()

    allowed = {"area", "professional_role", "period", "schedule"}
    unknown = set(values) - allowed
    if unknown:
        raise SystemExit(f"[err] unknown --root key(s): {', '.join(sorted(unknown))}")

    area = int(values.get("area", "113"))
    role = int(values["professional_role"]) if values.get("professional_role") else None
    period = int(values["period"]) if values.get("period") else None
    schedule = values.get("schedule") or None
    if schedule is not None:
        depth = 4
    elif period is not None:
        depth = 3
    elif role is not None:
        depth = 2
    elif area != 113:
        depth = 1
    else:
        depth = 0
    return Segment(
        area=area,
        professional_role=role,
        period=period,
        schedule=schedule,
        depth=depth,
    )


def _ingest_cbr(args: argparse.Namespace) -> int:
    from src.ingest.cbr_rates import fetch_rates, load_rates_for, utc_today, write_rates

    on = utc_today()
    if args.dry:
        print(f"[dry] cbr.ru/scripts/XML_daily on={on} → master/ref/cbr_rates.parquet")
        return 0

    print(f"[cbr] fetching rates for {on}")
    rates = fetch_rates(on)
    out = Path("master/ref/cbr_rates.parquet")
    if not rates:
        existing = load_rates_for(out, on)
        usable_existing = {k: v for k, v in existing.items() if k not in {"RUR", "RUB"}}
        if usable_existing:
            print(
                f"[cbr] no rates returned for {on}; keeping existing {out}",
                file=sys.stderr,
            )
            return 0
        print(
            f"[cbr] no rates returned for {on} — is it a holiday/weekend? "
            "ЦБ не публикует котировки в нерабочие дни.",
            file=sys.stderr,
        )
        return 4

    write_rates(rates, out)
    snapshot = ", ".join(f"{r.char_code}={r.value}" for r in rates if r.char_code in {"USD", "EUR", "CNY"})
    print(f"[cbr] {len(rates)} currencies → {out} | {snapshot}")
    return 0


def _ingest_hh(args: argparse.Namespace) -> int:
    transport = getattr(args, "transport", "shards")
    page_start = getattr(args, "page_start", 1)
    overlap_pages = getattr(args, "overlap_pages", 0)
    scope_name = getattr(args, "scope", None)
    if args.pages < 1:
        print("[err] --pages must be >= 1", file=sys.stderr)
        return 2
    if page_start < 1:
        print("[err] --page-start must be >= 1", file=sys.stderr)
        return 2
    if overlap_pages < 0 or overlap_pages >= args.pages:
        print("[err] --overlap-pages must be >= 0 and lower than --pages", file=sys.stderr)
        return 2
    full_sweep = bool(getattr(args, "full_sweep", False))
    if full_sweep and transport != "shards":
        print("[err] --full-sweep requires --transport shards", file=sys.stderr)
        return 2
    if full_sweep and page_start != 1:
        print("[err] --full-sweep requires --page-start 1", file=sys.stderr)
        return 2
    page_start_zero = page_start - 1
    page_end = page_start + args.pages - 1
    next_page_start = page_start + args.pages - overlap_pages

    # Lazy import — honors test monkeypatch on cli._resolve_hh_scope_role_ids
    # and cli._write_hh_completed_sweep_state (see test_cli_misc.py P1-1
    # ratchet).
    from src.cli import _resolve_hh_scope_role_ids, _write_hh_completed_sweep_state

    if args.dry:
        scope_suffix = ""
        if scope_name:
            try:
                _scope, role_ids = _resolve_hh_scope_role_ids(scope_name)
            except (FileNotFoundError, ValueError) as exc:
                print(f"[err] {exc}", file=sys.stderr)
                return 2
            role_csv = ",".join(str(role_id) for role_id in role_ids)
            scope_suffix = f" scope={scope_name} professional_role={role_csv}"
        sweep_suffix = " full-sweep" if full_sweep else ""
        print(
            f"[dry] hh.ru transport={transport} area={args.area} per_page={args.per_page} "
            f"pages={page_start}-{page_end} overlap={overlap_pages}{scope_suffix} "
            f"next_page_start={next_page_start}{sweep_suffix} → master/lake + events"
        )
        return 0
    from dotenv import load_dotenv

    from src.config import load_settings
    from src.ingest.raw_lake import (
        RawRecord,
        latest_snapshot_meta,
        load_raw_json_for,
        utcnow,
        write_batch,
    )
    from src.transform.events_derivation import append_events, derive_events

    load_dotenv()
    import polars as pl

    settings = load_settings()
    if scope_name:
        try:
            _scope, scope_role_ids = _resolve_hh_scope_role_ids(scope_name)
        except (FileNotFoundError, ValueError) as exc:
            print(f"[err] {exc}", file=sys.stderr)
            return 2
    else:
        scope_role_ids = []
    lake_root = Path("master/vacancies_raw.parquet")
    events_db = Path("master/events.duckdb")
    fetched_at = utcnow()
    previous_scope = scope_name if scope_name and args.detect_closed else None

    # Stage 1 of derive_events: только meta (без raw_json) для diff
    # identification. raw_json подгружается ниже только для тех vacancies,
    # у которых content_hash изменился (см. ниже Stage 2).
    previous_meta = latest_snapshot_meta(lake_root, source="hh", market_scope=previous_scope)
    items_collected_count = 0
    pages_done = 0
    transient_skips = 0
    role_sweep_complete: dict[int, bool] = {}
    defer_record_processing = bool(scope_name and args.detect_closed)
    deferred_record_batches: list[list[RawRecord]] = []
    seen_current_ids: set[str] = set()
    events_appended = 0
    event_type_counts: dict[str, int] = {}

    def _current_frame(records: list[RawRecord]) -> pl.DataFrame:
        return (
            pl.DataFrame(
                {
                    "vacancy_id": [r.vacancy_id for r in records],
                    "employer_id": [r.employer_id for r in records],
                    "content_hash": [r.content_hash for r in records],
                    "raw_json": [r.raw_json for r in records],
                }
            )
            .unique(subset=["vacancy_id"], keep="last", maintain_order=True)
            .sort("vacancy_id")
        )

    def _previous_for_current(current: pl.DataFrame) -> pl.DataFrame:
        if current.is_empty() or previous_meta.is_empty():
            return previous_meta.head(0).with_columns(
                pl.lit(None, dtype=pl.String).alias("raw_json")
            )
        current_ids = current["vacancy_id"].to_list()
        meta = previous_meta.filter(pl.col("vacancy_id").is_in(current_ids))
        if meta.is_empty():
            return meta.with_columns(pl.lit(None, dtype=pl.String).alias("raw_json"))
        prev_hashes = dict(
            zip(
                meta["vacancy_id"].to_list(),
                meta["content_hash"].to_list(),
            )
        )
        curr_hashes = dict(
            zip(current["vacancy_id"].to_list(), current["content_hash"].to_list())
        )
        changed_ids = {
            vid
            for vid, prev_hash in prev_hashes.items()
            if vid in curr_hashes and curr_hashes[vid] != prev_hash
        }
        if changed_ids:
            prev_raw = load_raw_json_for(lake_root, changed_ids, source="hh")
            return meta.join(prev_raw, on="vacancy_id", how="left")
        return meta.with_columns(pl.lit(None, dtype=pl.String).alias("raw_json"))

    def _record_event_summary(events: pl.DataFrame) -> None:
        nonlocal events_appended
        if events.is_empty():
            return
        appended = append_events(events, events_db)
        events_appended += appended
        for item in events.group_by("type").agg(pl.len().alias("n")).to_dicts():
            event_type = item["type"]
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + item["n"]

    def _write_and_emit(records: list[RawRecord]) -> None:
        current = _current_frame(records)
        if seen_current_ids:
            current = current.filter(~pl.col("vacancy_id").is_in(list(seen_current_ids)))
        # Previous raw_json must be loaded before this run's batch reaches the lake.
        previous = _previous_for_current(current)

        written_path = write_batch(records, lake_root)
        print(
            f"[lake] {len(records)} records → "
            f"{written_path.relative_to(Path.cwd()) if written_path.is_absolute() else written_path}"
        )

        if current.is_empty():
            return
        seen_current_ids.update(current["vacancy_id"].to_list())
        _record_event_summary(derive_events(previous, current, fetched_at))

    def _handle_records(records: list[RawRecord]) -> None:
        if defer_record_processing:
            deferred_record_batches.append(records)
        else:
            _write_and_emit(records)

    def _emit_closed_events() -> None:
        if not args.detect_closed or previous_meta.is_empty():
            return
        closed_ids = set(previous_meta["vacancy_id"].to_list()) - seen_current_ids
        if not closed_ids:
            return
        previous = previous_meta.filter(pl.col("vacancy_id").is_in(list(closed_ids)))
        previous = previous.with_columns(pl.lit(None, dtype=pl.String).alias("raw_json"))
        current = pl.DataFrame(
            schema={
                "vacancy_id": pl.String,
                "employer_id": pl.String,
                "content_hash": pl.String,
                "raw_json": pl.String,
            }
        )
        _record_event_summary(derive_events(previous, current, fetched_at))

    if transport == "shards":
        from src.ingest.hh_shards import (
            HHShardsClient,
            HHShardsConfig,
            HHTransientError,
            RateLimited,
            extract_vacancies,
        )

        rl = settings.hh.rate_limit
        client = HHShardsClient(
            HHShardsConfig(
                requests_per_second=rl.requests_per_second,
                backoff_min=rl.backoff_min,
                backoff_max=rl.backoff_max,
                max_retries=rl.max_retries,
            )
        )
        rest_areas_cache: list[int] = []

        def _rest_areas() -> list[int]:
            """RF subjects minus Moscow/SPb for the level-3 area partition."""
            if not rest_areas_cache:
                from src.ingest.refdata import (
                    fetch_areas,
                    load_areas_yaml,
                    russia_subjects,
                    save_areas_yaml,
                )

                areas_path = Path("data/areas.yaml")
                if not areas_path.exists():
                    save_areas_yaml(fetch_areas(), areas_path)
                subjects = [int(a["id"]) for a in russia_subjects(load_areas_yaml(areas_path))]
                rest = [a for a in subjects if a not in (_MOSCOW_AREA_ID, _SPB_AREA_ID)]
                if not rest:
                    raise ValueError("empty rest-of-Russia area list")
                rest_areas_cache.extend(rest)
            return rest_areas_cache

        search_role_ids: list[int | None] = list(scope_role_ids) if scope_role_ids else [None]
        for role_id in search_role_ids:
            role_complete = False
            shards_role_items: list[dict] = []
            if full_sweep:
                sweep_base: dict = {"area": args.area, "per_page": args.per_page}
                if role_id is not None:
                    sweep_base["professional_role"] = role_id
                sweep_label = f"role={role_id}" if role_id is not None else "all-roles"
                sweep_items, role_complete, sweep_pages, sweep_skips = _full_sweep_role(
                    client,
                    base_kwargs=sweep_base,
                    role_label=sweep_label,
                    rest_areas=_rest_areas,
                )
                shards_role_items.extend(sweep_items)
                items_collected_count += len(sweep_items)
                pages_done += sweep_pages
                transient_skips += sweep_skips
                print(
                    f"[sweep] {sweep_label}: "
                    f"{'complete' if role_complete else 'INCOMPLETE'} "
                    f"({len(sweep_items)} items, {sweep_pages} pages)"
                )
                if role_id is not None:
                    role_sweep_complete[role_id] = role_complete
                if shards_role_items:
                    _handle_records(
                        [
                            RawRecord.from_hh_shards_item(
                                item,
                                fetched_at,
                                market_scope=scope_name,
                            )
                            for item in shards_role_items
                        ]
                    )
                continue
            search_kwargs = {
                "area": args.area,
                "per_page": args.per_page,
                "max_pages": args.pages,
                "start_page": page_start_zero,
            }
            if role_id is not None:
                search_kwargs["professional_role"] = role_id
            role_label = f" role={role_id}" if role_id is not None else ""
            try:
                for page_num, data in enumerate(client.iter_pages(**search_kwargs)):
                    items = extract_vacancies(data)
                    shards_role_items.extend(items)
                    items_collected_count += len(items)
                    pages_done += 1
                    page_label = page_start + page_num
                    vsr = data.get("vacancySearchResult") or {}
                    total = vsr.get("totalResults")
                    last_page_obj = (vsr.get("paging") or {}).get("lastPage") or {}
                    last = last_page_obj.get("page") if isinstance(last_page_obj, dict) else None
                    last_page = int(last) if last is not None else None
                    # hh shards returns lastPage=None on the final page (matching
                    # the iter_pages stop condition), so treat that as the role's
                    # last page even though the numeric ceiling is missing.
                    if last_page is None or page_start_zero + page_num >= last_page:
                        role_complete = True
                    print(
                        f"page {page_label}{role_label}: {len(items)} items "
                        f"(total found: {total}, last page: {last})"
                    )
                    if page_num + 1 >= args.pages:
                        break
            except (HHTransientError, RateLimited) as exc:
                # Cloudflare 403 / 429 / 5xx after retries are exhausted. A
                # transient edge ban on one role must not abort the whole sweep:
                # skip this role (role_complete stays False so detect-closed will
                # not fire on a partial sweep) and keep whatever pages we already
                # collected before the error. The other roles still run.
                print(
                    f"[warn] hh shards transient error{role_label}, "
                    f"skipping role: {exc}",
                    file=sys.stderr,
                )
                role_complete = False
                transient_skips += 1
            if role_id is not None:
                role_sweep_complete[role_id] = role_complete
            if shards_role_items:
                _handle_records(
                    [
                        RawRecord.from_hh_shards_item(
                            item,
                            fetched_at,
                            market_scope=scope_name,
                        )
                        for item in shards_role_items
                    ]
                )
    else:
        import os

        from src.ingest.hh_api import HHClient, HHConfig

        token = os.environ.get("HH_ACCESS_TOKEN") or None
        if not token:
            print(
                "[warn] HH_ACCESS_TOKEN not set — api.hh.ru/vacancies returns 403.\n"
                "       Register an app at https://dev.hh.ru/admin or use --transport shards",
                file=sys.stderr,
            )
        rl = settings.hh.rate_limit
        client_api = HHClient(
            HHConfig(
                access_token=token,
                user_agent=settings.hh.user_agent,
                base=settings.hh.api_base,
                requests_per_second=rl.requests_per_second,
                backoff_min=rl.backoff_min,
                backoff_max=rl.backoff_max,
                max_retries=rl.max_retries,
            )
        )
        search_role_ids = list(scope_role_ids) if scope_role_ids else [None]
        for role_id in search_role_ids:
            role_complete = False
            api_role_items: list[dict] = []
            search_kwargs = {
                "area": args.area,
                "per_page": args.per_page,
                "max_pages": args.pages,
                "start_page": page_start_zero,
            }
            if role_id is not None:
                search_kwargs["professional_role"] = role_id
            for page_num, data in enumerate(client_api.iter_pages(**search_kwargs)):
                items = data.get("items", [])
                api_role_items.extend(items)
                items_collected_count += len(items)
                pages_done += 1
                page_label = page_start + page_num
                role_label = f" role={role_id}" if role_id is not None else ""
                pages_total = data.get("pages")
                last_page = max(int(pages_total) - 1, 0) if pages_total is not None else None
                if last_page is not None and page_start_zero + page_num >= last_page:
                    role_complete = True
                print(
                    f"page {page_label}{role_label}/{data.get('pages')}: "
                    f"{len(items)} items (total found: {data.get('found')})"
                )
                if page_num + 1 >= args.pages:
                    break
            if role_id is not None:
                role_sweep_complete[role_id] = role_complete
            if api_role_items:
                _handle_records(
                    [
                        RawRecord.from_hh_item(
                            item,
                            fetched_at,
                            market_scope=scope_name,
                        )
                        for item in api_role_items
                    ]
                )

    scope_sweep_complete = bool(
        scope_name
        and page_start == 1
        and set(role_sweep_complete) == set(scope_role_ids)
        and all(role_sweep_complete.values())
    )
    if items_collected_count == 0 and transient_skips > 0:
        # Every attempted role was skipped on a persistent transient error and
        # nothing was collected. This is a hard collection failure (sustained
        # Cloudflare/captcha block or dead network), NOT a legitimate empty
        # result, so it must surface as non-zero: the cron verdict logs FAIL
        # and the critical-ingest publish gate blocks a stale republish.
        # Partial success (any role collected rows → items_collected_count > 0)
        # still exits 0, preserving C1's per-role graceful skip.
        print(
            f"[err] hh ingest collected 0 vacancies; {transient_skips} role(s) "
            f"skipped on persistent transient errors (Cloudflare/captcha block "
            f"or network failure) — not a real empty result",
            file=sys.stderr,
        )
        return 3
    if items_collected_count == 0 and not (scope_name and args.detect_closed):
        print("[done] empty page — nothing to write")
        return 0
    closed_refused = bool(scope_name and args.detect_closed and not scope_sweep_complete)
    if closed_refused:
        if not full_sweep:
            print(
                "[err] current scoped hh sweep is incomplete; refusing to emit closed events",
                file=sys.stderr,
            )
            return 2
        # Full sweep with a transient hole or an uncovered leaf: discarding
        # the whole collection (the pre-sweep behavior) would trade a mostly
        # fresh corpus for a stale dashboard. Keep the records and events,
        # skip only the closed emission — absence is unprovable on a partial
        # view. last_seen advances only for actually-seen rows, so the
        # active-days window stays honest.
        print(
            "[warn] full sweep incomplete — keeping collected records, "
            "skipping closed-event emission this run",
            file=sys.stderr,
        )

    if defer_record_processing:
        for records in deferred_record_batches:
            _write_and_emit(records)

    if not closed_refused:
        _emit_closed_events()

    if events_appended == 0:
        print("[events] no diff — first run or unchanged snapshot")
    else:
        summary = ", ".join(
            f"{event_type}={event_type_counts[event_type]}"
            for event_type in sorted(event_type_counts)
        )
        print(f"[events] +{events_appended} → {events_db} ({summary})")

    if scope_sweep_complete and scope_name is not None:
        _write_hh_completed_sweep_state(
            scope_name,
            fetched_at=fetched_at,
            transport=transport,
            area=args.area,
            per_page=args.per_page,
            page_start=page_start,
            pages=args.pages,
            overlap_pages=overlap_pages,
            role_ids=scope_role_ids,
            vacancy_count=items_collected_count,
        )
        print(f"[state] completed hh scope sweep: {scope_name}")

    done_end = page_start + pages_done - 1
    done_next = page_start + pages_done - overlap_pages
    print(
        f"[done] fetched {items_collected_count} vacancies across pages {page_start}-{done_end}; "
        f"next --page-start {done_next}"
    )
    return 0
