"""`vradar publish {slim,events,weekly,embeddings,hf-mirror}` impls.

Extracted from src/cli.py per Kimi audit P1-1. The Vercel Blob / Neon /
snapshots publish paths were removed 2026-07-05 together with the legacy
Next.js app — the only external publish surface is the Hugging Face dataset
mirror (`publish hf-mirror`); everything else builds local artifacts in
`derived/` that the mirror uploads.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _publish(args: argparse.Namespace) -> int:
    if args.target == "slim":
        return _publish_slim(args)
    if args.target == "events":
        return _publish_events(args)
    if args.target == "weekly":
        return _publish_weekly(args)
    if args.target == "embeddings":
        return _publish_embeddings(args)
    if args.target == "hf-mirror":
        return _publish_hf_mirror(args)
    return 1


def _publish_hf_mirror(args: argparse.Namespace) -> int:
    import os
    import subprocess

    from dotenv import load_dotenv

    from src.publish.hf_mirror import (
        HfMirrorConfig,
        build_upload_plan,
        missing_required_paths,
        public_base_url,
        upload_items,
    )

    load_dotenv()
    repo_id = os.environ.get("HF_REPO_ID")
    token = os.environ.get("HF_TOKEN")
    revision = os.environ.get("HF_REVISION", "main")
    if not repo_id or not token:
        print("[err] HF_REPO_ID / HF_TOKEN not in .env", file=sys.stderr)
        return 2

    missing = missing_required_paths(Path("."))
    if missing:
        print(
            "[err] HF mirror required artifacts missing: "
            + ", ".join(path.as_posix() for path in missing),
            file=sys.stderr,
        )
        return 3

    cfg = HfMirrorConfig(repo_id=repo_id, token=token, revision=revision)
    plan = build_upload_plan(Path("."))
    base_url = public_base_url(repo_id, revision=revision)
    if getattr(args, "dry", False):
        print(f"[dry] publish hf-mirror -> {base_url}")
        for item in plan:
            print(f"[dry]   {item.local_path} -> {item.path_in_repo}")
        return 0

    try:
        upload_items(plan, cfg)
    except FileNotFoundError as exc:
        print(f"[err] huggingface-cli not found: {exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else exc.stderr
        print(f"[err] huggingface-cli upload failed: {stderr}", file=sys.stderr)
        return 1

    print(f"[hf] mirror base -> {base_url}")
    for item in plan:
        print(f"[hf] uploaded {item.local_path} -> {item.path_in_repo}")
    return 0


def _publish_embeddings(args: argparse.Namespace) -> int:
    from src.publish.embeddings_export import export_to_parquet

    if getattr(args, "dry", False):
        print("[dry] publish embeddings — skip build")
        return 0

    lance_path = Path("master/embeddings.lance")
    out = Path("derived/embeddings.parquet")
    if not lance_path.exists():
        print(
            "[err] master/embeddings.lance missing — run `vradar enrich embeddings` first",
            file=sys.stderr,
        )
        return 3

    rows = export_to_parquet(out, lance_path)
    if rows == 0:
        print("[err] empty Lance store — nothing to publish", file=sys.stderr)
        return 3
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[embeddings] {rows} rows → {out} ({size_mb:.3f} MB)")
    return 0


def _publish_weekly(args: argparse.Namespace) -> int:
    import polars as pl

    from src.transform.weekly_aggregates import build_all_weekly, write_weekly_aggregates

    if getattr(args, "dry", False):
        print("[dry] publish weekly — skip build")
        return 0

    lake = Path("master/vacancies_raw.parquet")
    events_db = Path("master/events.duckdb")
    out_dir = Path("derived/agg")
    slim_path = Path("derived/slim_active.parquet")
    strict = getattr(args, "strict", False)

    # Reuse derived/slim_active.parquet written by the preceding `publish slim`
    # step. Daily refresh ordering enforces that: `publish slim --scope it` runs
    # right before us, freshness-gated at 24h via --strict (exit 4 on stale).
    # Pre-2026-05-17 we re-ran `build_slim_active(lake)` here on the full
    # 665k lake (~1h47m) and ignored the just-written 12 MB parquet — the
    # cron's largest single cost. Reading the parquet brings weekly inline
    # with the dashboard's IT-scoped corpus.
    if slim_path.exists():
        slim = pl.read_parquet(slim_path)
        size_mb = slim_path.stat().st_size / 1024 / 1024
        print(
            f"[weekly] reusing {slim_path} ({slim.height} rows, {size_mb:.1f} MB)"
        )
    else:
        from src.transform.slim_export import build_slim_active

        print(
            f"[weekly] {slim_path} missing — rebuilding from {lake}",
            file=sys.stderr,
        )
        slim = build_slim_active(lake)

    aggregates = build_all_weekly(events_db, slim)
    written = write_weekly_aggregates(aggregates, out_dir)

    # Freshness gate. Empty aggregate files break /trends silently — the
    # storefront gets a 0-row Parquet and renders an empty chart with no error.
    # In strict mode (CI / scheduled) we exit non-zero so the run fails loud;
    # otherwise we WARN so the previous-good mirror copy stays intact.
    empty_stems: list[str] = []
    for path in written:
        rows = aggregates[path.stem].height
        size_kb = path.stat().st_size / 1024
        if rows == 0:
            empty_stems.append(path.stem)
            print(
                f"[weekly] {path.stem}: 0 rows — empty aggregate",
                file=sys.stderr,
            )
            continue
        print(f"[weekly] {path.stem}: {rows} rows ({size_kb:.1f} KB)")

    if empty_stems:
        if strict:
            print(
                f"[err] weekly --strict: empty aggregates {empty_stems}",
                file=sys.stderr,
            )
            return 4
        print(
            f"[warn] {len(empty_stems)}/{len(written)} weekly aggregates empty: {empty_stems}",
            file=sys.stderr,
        )
    return 0


def _publish_events(args: argparse.Namespace) -> int:
    import shutil

    from src.transform.slim_events import (
        build_slim_events_30d,
        write_slim_events_partitioned,
    )

    if getattr(args, "dry", False):
        print("[dry] publish events — skip build")
        return 0

    events_db = Path("master/events.duckdb")
    out_root = Path("derived/slim_events_30d")

    df = build_slim_events_30d(events_db)

    if df.is_empty():
        print("[done] no events in last 30 days — nothing to publish")
        return 0

    # Wipe local out_root so partitions outside the rolling 30-day window
    # don't survive into the next publish (they would otherwise be re-uploaded
    # by the HF mirror and break the events_30d v1 invariant
    # `ts ∈ [now()-30d, now()]`).
    if out_root.exists():
        shutil.rmtree(out_root)
    written = write_slim_events_partitioned(df, out_root)
    total_size = sum(p.stat().st_size for p in written)
    print(
        f"[events] {df.height} events × {len(df.columns)} cols → "
        f"{len(written)} partitions in {out_root} ({total_size/1024:.1f} KB)"
    )
    return 0


def _publish_slim(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    import polars as pl

    from src.transform.slim_export import (
        apply_cross_source_dedup,
        build_slim_active,
        write_slim_active,
    )

    if getattr(args, "dry", False):
        print("[dry] publish slim — skip build")
        return 0

    lake = Path("master/vacancies_raw.parquet")
    out = Path("derived/slim_active.parquet")
    strict = getattr(args, "strict", False)

    active_days = getattr(args, "active_days", None)
    scope_name = getattr(args, "scope", None)
    df = build_slim_active(lake, active_window_days=active_days, market_scope=scope_name)
    if df.is_empty():
        print(
            "[err] empty lake — run `vradar ingest hh` first",
            file=sys.stderr,
        )
        return 3
    if scope_name:
        print(f"[slim] market-scope filter: {scope_name}")
    if active_days is not None:
        print(f"[slim] active-window filter: last_seen_at >= now - {active_days}d")

    # Freshness gate: detect stalled ingest. If the most-recently-seen vacancy
    # is older than 24h, the collection run has likely been failing — the HF
    # mirror would keep republishing stale data and the storefront would show
    # nothing new. Strict mode → exit non-zero для scheduled task / CI.
    last_seen = df.select(pl.col("last_seen_at").max()).item()
    if last_seen is not None:
        if isinstance(last_seen, datetime) and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_seen
        if age > timedelta(hours=24):
            msg = f"slim freshness: last_seen_at={last_seen.isoformat()} (age={age})"
            if strict:
                print(f"[err] {msg} — exceeds 24h threshold", file=sys.stderr)
                return 4
            print(f"[warn] {msg} — ingest may have stalled", file=sys.stderr)
    if getattr(args, "dedup", False):
        before = df.height
        df, pairs = apply_cross_source_dedup(df)
        print(
            f"[dedup] cross-source pairs={len(pairs)} dropped={before - df.height} "
            f"(kept hh, dropped telegram)"
        )
    write_slim_active(df, out)
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[slim] {df.height} rows × {len(df.columns)} cols → {out} ({size_mb:.3f} MB)")
    return 0
