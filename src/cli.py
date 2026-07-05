from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vradar")
    sub = parser.add_subparsers(dest="cmd")

    ingest = sub.add_parser("ingest", help="Ingest data from a source")
    ingest.add_argument("source", choices=["hh", "hh-crawl", "telegram", "cbr"])
    ingest.add_argument("--dry", action="store_true", help="Plan only, no IO")
    ingest.add_argument("--scope", default=None, help="market scope from config.yaml, e.g. it")
    ingest.add_argument("--pages", type=int, default=1, help="hh: number of pages to fetch (smoke)")
    ingest.add_argument("--page-start", type=int, default=1, help="hh: 1-based first search page")
    ingest.add_argument("--overlap-pages", type=int, default=0, help="hh: overlap to use when planning next window")
    ingest.add_argument("--per-page", type=int, default=50, help="hh: items per page")
    ingest.add_argument("--area", type=int, default=113, help="hh: area code (113=Russia)")
    ingest.add_argument("--root", default="area=113", help="hh-crawl: initial segment, e.g. area=1,professional_role=156")
    ingest.add_argument("--max-depth", type=int, default=4, help="hh-crawl: adaptive split depth cap")
    ingest.add_argument("--rate", type=float, default=1.0, help="hh-crawl: seconds between requests")
    ingest.add_argument("--max-vacancies", type=int, default=2_000_000, help="hh-crawl: stop after this many vacancies")
    ingest.add_argument("--reset", action="store_true", help="hh-crawl: ignore existing master/crawl_progress.json")
    ingest.add_argument("--channels", type=int, default=None, help="tg: ограничить N первыми каналами (default: все)")
    ingest.add_argument(
        "--channel-start",
        "--channel-offset",
        dest="channel_start",
        type=int,
        default=0,
        help="tg: zero-based channel index to start from",
    )
    ingest.add_argument("--channel-file", default=None, help="tg: text file with one channel username per line")
    ingest.add_argument("--limit", type=int, default=200, help="tg: messages per channel (default: 200)")
    ingest.add_argument(
        "--transport",
        choices=["shards", "api"],
        default="shards",
        help="hh: 'shards' (default, public web JSON via curl_cffi) or 'api' (api.hh.ru OAuth Bearer)",
    )
    ingest.add_argument(
        "--detect-closed",
        action="store_true",
        help=(
            "emit `closed` events for vacancy_id present in previous but absent from current run. "
            "Off by default because partial ingest (--pages 10 --area 113) cannot see the full "
            "active set. Enable only after a full sweep."
        ),
    )
    ingest.add_argument(
        "--full-sweep",
        action="store_true",
        help=(
            "hh shards: drain every role completely. Roles whose totalResults exceed the "
            "~2000-item shards result window are auto-segmented (experience buckets, then "
            "Moscow/SPb/rest-of-Russia areas) so the sweep covers the whole active set — "
            "the prerequisite for --detect-closed and `publish slim --active-days`. "
            "Ignores --pages; requires --page-start 1 and --transport shards."
        ),
    )

    refdata = sub.add_parser("refdata", help="Fetch/cache reference data")
    refdata.add_argument("kind", choices=["roles", "areas"])
    refdata.add_argument("--refresh", action="store_true", help="fetch from api.hh.ru even if cache exists")

    auth = sub.add_parser("auth", help="Acquire access tokens")
    auth.add_argument("provider", choices=["hh", "tg"])
    auth.add_argument("--client-id", default=None, help="hh.ru OAuth client ID (or HH_CLIENT_ID env)")
    auth.add_argument(
        "--client-secret", default=None, help="hh.ru OAuth client secret (or HH_CLIENT_SECRET env)"
    )
    auth.add_argument("--phone", default=None, help="tg: phone number in E.164, e.g. +79001234567")

    publish = sub.add_parser("publish", help="Publish derived artifacts")
    publish.add_argument(
        "target",
        choices=["slim", "events", "weekly", "embeddings", "hf-mirror"],
    )
    publish.add_argument("--scope", default=None, help="slim: market scope filter from config.yaml, e.g. it")
    publish.add_argument(
        "--dry",
        action="store_true",
        help="parse args + plan only, skip artifact build and uploads",
    )
    publish.add_argument(
        "--dedup",
        action="store_true",
        help="slim: drop telegram rows that have a near-duplicate hh row (MinHash LSH)",
    )
    publish.add_argument(
        "--strict",
        action="store_true",
        help=(
            "fail-fast freshness gates. "
            "weekly: exit 4 if any aggregate is empty (default: warn + skip upload). "
            "slim: exit 4 if last_seen_at older than 24h (default: warn + upload)."
        ),
    )
    publish.add_argument(
        "--active-days",
        type=int,
        default=None,
        help=(
            "slim: include only vacancies with last_seen_at >= now - N days. "
            "Без флага: legacy режим, dashboard видит весь corpus (accumulated). "
            "Включать ТОЛЬКО после успешного full sweep recrawl — иначе обрежет "
            "live данные до stale tail."
        ),
    )

    enrich = sub.add_parser("enrich", help="Enrich raw lake with derived fields")
    enrich.add_argument("kind", choices=["hh-details", "embeddings"])
    enrich.add_argument("--rate", type=float, default=1.0, help="seconds between hh.ru fetches (politeness)")
    enrich.add_argument("--limit", type=int, default=None, help="cap fetches for smoke runs")
    enrich.add_argument("--batch-size", type=int, default=32, help="embeddings: encoder batch size")
    enrich.add_argument("--force", action="store_true", help="embeddings: re-encode all even if hash unchanged")
    enrich.add_argument(
        "--scope",
        default=None,
        help="hh-details: market scope filter from config.yaml, e.g. it — only fetch details for vacancies labeled with that scope",
    )

    prune = sub.add_parser("prune", help="Prune historical data with retention windows")
    prune.add_argument("target", choices=["events", "lake"])
    prune.add_argument(
        "--older-than-days",
        type=int,
        default=180,
        help=(
            "events: drop rows with ts < now - N days. Default 180 covers "
            "monthly_digest (any month within 6mo) + weekly_market_pulse (90d) + "
            "weekly_employer_top (12wk ≈ 84d). Anything older is unused by web "
            "surfaces and rarely consulted in reports."
        ),
    )
    prune.add_argument(
        "--dry",
        action="store_true",
        help="show what would be deleted without calling DELETE",
    )
    prune.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "run CHECKPOINT after DELETE to reclaim disk space. Off by default "
            "because CHECKPOINT can be slow on multi-GB DBs."
        ),
    )
    prune.add_argument(
        "--lake-root",
        type=str,
        default="master/vacancies_raw.parquet",
        help="lake: raw lake root with year=/month=/source= Hive partitions",
    )
    prune.add_argument(
        "--trash-dir",
        type=str,
        default="master/.lake_compact_trash",
        help=(
            "lake: originals are moved here (outside the lake glob) instead of "
            "deleted; remove manually after verifying reads"
        ),
    )

    report = sub.add_parser("report", help="Generate ad-hoc report")
    report.add_argument("kind", choices=["monthly", "employer", "skill"])
    report.add_argument("--month", type=str, default=None)
    report.add_argument("--employer", type=str, default=None)
    report.add_argument(
        "--scope",
        default=None,
        help=(
            "market scope: 'it' reuses live IT slim (derived/slim_active.parquet); "
            "anything else (e.g. 'full') builds a fresh full-market slim from raw lake "
            "into derived/slim_full.parquet and points the report at it via VRADAR_SLIM_PATH. "
            "Full-market path never writes to Turso/Blob."
        ),
    )

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    if args.cmd == "ingest":
        return _ingest(args)
    if args.cmd == "auth":
        return _auth(args)
    if args.cmd == "publish":
        return _publish(args)
    if args.cmd == "enrich":
        return _enrich(args)
    if args.cmd == "report":
        return _report(args)
    if args.cmd == "refdata":
        return _refdata(args)
    if args.cmd == "prune":
        return _prune(args)
    print(f"[stub] cmd={args.cmd} args={vars(args)}")
    return 0


def _load_yaml(path):
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data


def _load_settings_for_cwd():
    from pathlib import Path

    from src.config import load_settings

    return load_settings(Path("config.yaml").resolve())


def _resolve_hh_scope_role_ids(scope_name: str) -> tuple[object, list[int]]:
    from pathlib import Path

    settings = _load_settings_for_cwd()
    scope = settings.market.require_scope(scope_name)
    roles_data = _load_yaml(Path(scope.hh.roles_file))
    return scope, scope.hh.resolve_role_ids(roles_data)


def _write_hh_completed_sweep_state(
    scope_name: str,
    *,
    fetched_at,
    transport: str,
    area: int,
    per_page: int,
    page_start: int,
    pages: int,
    overlap_pages: int,
    role_ids: list[int],
    vacancy_count: int,
) -> None:
    import json
    from pathlib import Path

    path = Path("master/run_state/hh_completed_sweeps.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    state[scope_name] = {
        "source": "hh",
        "scope": scope_name,
        "complete": True,
        "completed_at": fetched_at.isoformat(),
        "transport": transport,
        "area": area,
        "per_page": per_page,
        "page_start": page_start,
        "pages": pages,
        "overlap_pages": overlap_pages,
        "role_ids": role_ids,
        "vacancies": vacancy_count,
    }
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_telegram_channels(path):
    data = _load_yaml(path)
    return data.get("channels", data) if isinstance(data, dict) else data


def _scoped_telegram_channels(scope_name: str, explicit_file: str | None = None) -> list[dict]:
    from pathlib import Path

    if explicit_file:
        return _load_telegram_channels(Path(explicit_file))
    settings = _load_settings_for_cwd()
    scope = settings.market.require_scope(scope_name)
    channels_path = Path(scope.telegram.channels_file or settings.telegram.channels_file)
    return scope.telegram.filter_channels(_load_telegram_channels(channels_path))


# --- Subcommand re-exports (Kimi audit P1-1) -----------------------------------
# All `_*` symbols below are imported solely to provide the historical
# `from src.cli import _foo` surface that tests + monkeypatch fixtures depend
# on (see tests/unit/test_cli_*.py — many sites assert
# `monkeypatch.setattr(cli, "_foo", fake)`). `__all__` declares them as
# intentional public re-exports so ruff F401 doesn't strip them, and so a
# future `from src.cli import *` works for compat.
from src.cli_modules.auth import _auth, _auth_tg  # noqa: E402, F401
from src.cli_modules.enrich import (  # noqa: E402, F401
    _cache_size,
    _enrich,
    _enrich_embeddings,
    _enrich_hh_details,
)
from src.cli_modules.ingest import (  # noqa: E402, F401
    _ingest,
    _ingest_cbr,
    _ingest_hh,
    _ingest_hh_crawl,
    _ingest_telegram,
    _parse_hh_crawl_root,
)
from src.cli_modules.prune import _prune, _prune_events  # noqa: E402, F401
from src.cli_modules.publish import (  # noqa: E402, F401
    _publish,
    _publish_embeddings,
    _publish_events,
    _publish_hf_mirror,
    _publish_slim,
    _publish_weekly,
)
from src.cli_modules.refdata import _refdata  # noqa: E402, F401
from src.cli_modules.report import _report  # noqa: E402, F401

__all__ = [
    "main",
    # Top-level entry points + shared helpers (referenced by cli_modules/*).
    "_auth",
    "_auth_tg",
    "_cache_size",
    "_enrich",
    "_enrich_embeddings",
    "_enrich_hh_details",
    "_ingest",
    "_ingest_cbr",
    "_ingest_hh",
    "_ingest_hh_crawl",
    "_ingest_telegram",
    "_load_settings_for_cwd",
    "_load_telegram_channels",
    "_load_yaml",
    "_parse_hh_crawl_root",
    "_prune",
    "_prune_events",
    "_publish",
    "_publish_embeddings",
    "_publish_events",
    "_publish_hf_mirror",
    "_publish_slim",
    "_publish_weekly",
    "_refdata",
    "_report",
    "_resolve_hh_scope_role_ids",
    "_scoped_telegram_channels",
    "_write_hh_completed_sweep_state",
]


if __name__ == "__main__":
    sys.exit(main())
