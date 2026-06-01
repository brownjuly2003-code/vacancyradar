"""`vradar publish {slim,events,weekly,embeddings,snapshots,neon,hf-mirror}` impls.

Extracted from src/cli.py per Kimi audit P1-1. `_upload_blob` deliberately
stays in src/cli.py so tests' `monkeypatch.setattr(cli, "_upload_blob", ...)`
contract is preserved — each function below does a function-scoped
`from src.cli import _upload_blob` which re-resolves the module attribute on
every call (i.e., picks up monkey-patched replacements). The same lazy-import
pattern works for `_load_blob_cfg` (moved here, not re-exported back).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.publish.blob_push import BlobConfig


def _publish(args: argparse.Namespace) -> int:
    if args.target == "slim":
        return _publish_slim(args)
    if args.target == "events":
        return _publish_events(args)
    if args.target == "weekly":
        return _publish_weekly(args)
    if args.target == "embeddings":
        return _publish_embeddings(args)
    if args.target == "snapshots":
        return _publish_snapshots(args)
    if args.target == "neon":
        return _publish_neon(args)
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


def _publish_neon(args: argparse.Namespace) -> int:
    import os

    from dotenv import load_dotenv

    from src.publish.neon_sync import ShrinkageGuardError, sync_parquet_to_neon

    load_dotenv()
    database_url = os.environ.get("NEON_DATABASE_URL")
    if not database_url:
        print("[err] NEON_DATABASE_URL not set — see .env.example", file=sys.stderr)
        return 3

    parquet_path = Path("derived/slim_active.parquet")
    if not parquet_path.exists():
        print(
            "[err] derived/slim_active.parquet missing — run `vradar publish slim` first",
            file=sys.stderr,
        )
        return 3

    force = getattr(args, "force", False)

    if getattr(args, "dry", False):
        print(
            f"[neon] dry: would sync {parquet_path} to Neon "
            f"(init={args.init}, force={force})"
        )
        return 0

    try:
        stats = sync_parquet_to_neon(
            parquet_path,
            database_url,
            init_schema=args.init,
            dry=False,
            force=force,
        )
    except ShrinkageGuardError as exc:
        print(f"[neon][abort] shrinkage guard: {exc}", file=sys.stderr)
        return 4
    print(
        f"[neon] rows_read={stats['rows_read']} "
        f"upserted={stats['rows_upserted']} "
        f"deleted={stats['rows_deleted']}"
    )
    return 0


def _publish_snapshots(args: argparse.Namespace) -> int:
    import json
    import os

    import polars as pl

    from src.cli import _upload_blob  # late-bound; honors monkeypatch on cli._upload_blob
    from src.publish.snapshots import build_snapshots, iter_blob_paths

    cfg, exit_code = _load_blob_cfg("snapshots", getattr(args, "dry", False))
    if cfg is None:
        if getattr(args, "dry", False) or exit_code != 0:
            return exit_code

    slim_path = Path("derived/slim_active.parquet")
    if not slim_path.exists():
        print(
            "[err] derived/slim_active.parquet missing — run `vradar publish slim` first",
            file=sys.stderr,
        )
        return 3

    weekly_dir = Path("derived/agg")
    weekly: dict[str, pl.DataFrame] = {}
    for stem in (
        "weekly_market_pulse",
        "weekly_employer_top",
        "weekly_skill_velocity",
        "weekly_role_salary",
    ):
        weekly_path = weekly_dir / f"{stem}.parquet"
        weekly[stem] = pl.read_parquet(weekly_path) if weekly_path.exists() else pl.DataFrame()

    slim = pl.read_parquet(slim_path)
    out_dir = Path("derived")
    written = build_snapshots(slim, weekly, out_dir)

    snapshots_dir = out_dir / "snapshots"
    for name, path in written.items():
        size_kb = path.stat().st_size / 1024
        print(f"[snapshots] {name}: {size_kb:.1f} KB → {path}")

    # Upload to Vercel Blob. May 403 when the store is suspended — we log and
    # continue, because the Neon `aggregates` upsert below makes /api/* routes
    # Blob-independent. Don't let a suspended Blob block the recovery path.
    if cfg is not None:
        for local_path, blob_pathname in iter_blob_paths(snapshots_dir):
            try:
                _upload_blob(local_path, blob_pathname, cfg, content_type="application/json")
            except Exception as exc:
                print(f"[snapshots] blob upload failed for {blob_pathname}: {exc}", file=sys.stderr)

    database_url = os.environ.get("NEON_DATABASE_URL")
    if database_url:
        try:
            import psycopg
            from psycopg.types.json import Jsonb
        except ImportError:
            print("[snapshots] psycopg not installed — skipping Neon upsert", file=sys.stderr)
        else:
            # schema_version=1 stamp ensures routes can detect mid-deploy
            # shape skew (route expecting v2 ignores v1 payload).
            # CX audit 2026-05-17 P2.
            from src.publish.snapshots import CURRENT_AGGREGATE_SCHEMA_VERSION

            inserts = 0
            with psycopg.connect(database_url) as conn, conn.cursor() as cur:
                for name, path in written.items():
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO aggregates (name, payload, schema_version, refreshed_at) "
                        "VALUES (%s, %s, %s, now()) "
                        "ON CONFLICT (name) DO UPDATE SET "
                        "payload = EXCLUDED.payload, "
                        "schema_version = EXCLUDED.schema_version, "
                        "refreshed_at = now()",
                        (name, Jsonb(payload), CURRENT_AGGREGATE_SCHEMA_VERSION),
                    )
                    inserts += 1
                conn.commit()
            print(f"[snapshots] upserted {inserts} aggregates → Neon")
    return 0


def _publish_embeddings(args: argparse.Namespace) -> int:
    from src.cli import _upload_blob
    from src.publish.embeddings_export import export_to_parquet

    cfg, exit_code = _load_blob_cfg("embeddings", getattr(args, "dry", False))
    if cfg is None:
        if getattr(args, "dry", False) or exit_code != 0:
            return exit_code

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
    if cfg is not None:
        try:
            _upload_blob(out, "agg/embeddings.parquet", cfg)
        except Exception as exc:
            print(
                f"[embeddings] blob upload failed for agg/embeddings.parquet: {exc}",
                file=sys.stderr,
            )
    return 0


def _publish_weekly(args: argparse.Namespace) -> int:
    import polars as pl

    from src.cli import _upload_blob
    from src.transform.weekly_aggregates import build_all_weekly, write_weekly_aggregates

    cfg, exit_code = _load_blob_cfg("weekly", getattr(args, "dry", False))
    if cfg is None:
        if getattr(args, "dry", False) or exit_code != 0:
            return exit_code

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

    # Freshness gate. Empty aggregate files break /trends silently — frontend
    # gets a 0-row Parquet and renders an empty chart with no error.
    # In strict mode (CI / scheduled) we exit non-zero so the run fails loud;
    # otherwise we WARN and skip upload of the empty artifact, leaving the
    # previous-good Blob copy intact.
    empty_stems: list[str] = []
    for path in written:
        rows = aggregates[path.stem].height
        size_kb = path.stat().st_size / 1024
        if rows == 0:
            empty_stems.append(path.stem)
            print(
                f"[weekly] {path.stem}: 0 rows — SKIPPED upload "
                f"(would overwrite previous-good Blob with empty file)",
                file=sys.stderr,
            )
            continue
        print(f"[weekly] {path.stem}: {rows} rows ({size_kb:.1f} KB)")
        # Blob upload is non-fatal — mirrors the snapshots pattern in
        # _publish_snapshots above. While the store is suspended (egress
        # overage) the local parquet is still written and `publish snapshots`
        # downstream rebuilds the Neon `aggregates` row from it, which is what
        # the Vercel routes actually read. Pre-2026-05-17 each cron exited 1
        # at this point and reported "DONE with errors" even though the data
        # made it to Neon successfully.
        if cfg is not None:
            try:
                _upload_blob(path, f"agg/{path.name}", cfg)
            except Exception as exc:
                print(
                    f"[weekly] blob upload failed for agg/{path.name}: {exc}",
                    file=sys.stderr,
                )

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

    from src.cli import _upload_blob
    from src.transform.slim_events import (
        build_slim_events_30d,
        list_partition_uploads,
        write_slim_events_partitioned,
    )

    cfg, exit_code = _load_blob_cfg("events", getattr(args, "dry", False))
    if cfg is None:
        if getattr(args, "dry", False) or exit_code != 0:
            return exit_code

    events_db = Path("master/events.duckdb")
    out_root = Path("derived/slim_events_30d")

    df = build_slim_events_30d(events_db)

    if df.is_empty():
        print("[done] no events in last 30 days — nothing to publish")
    else:
        # Wipe local out_root so partitions outside the rolling 30-day window
        # don't survive into the next publish (they would otherwise be re-uploaded
        # by list_partition_uploads and break the events_30d v1 invariant
        # `ts ∈ [now()-30d, now()]`). Blob-side cleanup of stale paths is the
        # prune pass below.
        if out_root.exists():
            shutil.rmtree(out_root)
        written = write_slim_events_partitioned(df, out_root)
        total_size = sum(p.stat().st_size for p in written)
        print(
            f"[events] {df.height} events × {len(df.columns)} cols → "
            f"{len(written)} partitions in {out_root} ({total_size/1024:.1f} KB)"
        )

        # Blob upload is non-fatal — same rationale as _publish_weekly. Local
        # partitioned parquet remains written so DuckDB+httpfs fallback (or
        # a future migration to GitHub Pages mirror) can pick it up.
        if cfg is not None:
            for local, pathname in list_partition_uploads(out_root):
                try:
                    _upload_blob(local, pathname, cfg)
                except Exception as exc:
                    print(
                        f"[events] blob upload failed for {pathname}: {exc}",
                        file=sys.stderr,
                    )

    if cfg is not None and not args.no_prune:
        from datetime import datetime, timezone

        from src.publish.blob_ttl import prune_events_30d

        # Use UTC date — build_slim_events_30d also filters by UTC. Local date
        # near midnight in UTC+N timezones could advance the cutoff a day ahead
        # of the build window and delete a freshly-uploaded cutoff-day partition.
        today_utc = datetime.now(timezone.utc).date()
        try:
            prune = prune_events_30d(
                cfg,
                today=today_utc,
                dry_run=args.prune_dry_run,
            )
        except Exception as exc:
            print(f"[events] blob prune failed: {exc}", file=sys.stderr)
            return 0
        prefix = "[prune dry-run]" if prune.dry_run else "[prune]"
        action = "would delete" if prune.dry_run else "deleted"
        print(
            f"{prefix} kept={prune.kept} {action}={prune.pruned} cutoff={prune.cutoff}"
        )
        if prune.skipped_unparseable:
            print(
                f"{prefix} skipped (unparseable pathname): "
                f"{len(prune.skipped_unparseable)} blob(s)"
            )
        for p in prune.pruned_pathnames:
            print(f"{prefix}   {p}")
    return 0


def _load_blob_cfg(
    target: str, dry: bool
) -> "tuple[BlobConfig | None, int]":
    """Resolve BlobConfig from .env.

    Contract: caller's idiom is
        cfg, exit_code = _load_blob_cfg(...)
        if cfg is None:
            return exit_code
        ... use cfg ...

    cfg=None + exit_code=2 → env validation failed; surface to shell.
    cfg=None + exit_code=0 → --dry or HF-primary mode; skip Blob upload.
    cfg=BlobConfig + exit_code=0 → continue.
    """
    import os

    from dotenv import load_dotenv

    from src.publish.blob_push import BlobConfig

    load_dotenv()
    token = os.environ.get("BLOB_READ_WRITE_TOKEN")
    base = os.environ.get("BLOB_PUBLIC_BASE_URL")
    if base and "huggingface.co/datasets/" in base.lower():
        print(
            f"[{target}] blob upload disabled: BLOB_PUBLIC_BASE_URL points to Hugging Face"
        )
        if dry:
            print(f"[dry] publish {target} — skip build + upload")
        return None, 0
    if not token or not base:
        print(
            "[err] BLOB_READ_WRITE_TOKEN / BLOB_PUBLIC_BASE_URL not in .env",
            file=sys.stderr,
        )
        return None, 2
    if dry:
        print(f"[dry] publish {target} — env ok, skip build + upload")
        return None, 0
    return BlobConfig(token=token, public_base_url=base), 0


def _publish_slim(args: argparse.Namespace) -> int:
    from datetime import datetime, timedelta, timezone

    import polars as pl

    from src.cli import _upload_blob
    from src.transform.slim_export import (
        apply_cross_source_dedup,
        build_slim_active,
        write_slim_active,
    )

    cfg, exit_code = _load_blob_cfg("slim", getattr(args, "dry", False))
    if cfg is None:
        if getattr(args, "dry", False) or exit_code != 0:
            return exit_code

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
    # is older than 24h, daily_refresh has likely been failing — публикация
    # на Vercel Blob продолжалась бы со stale данными, и frontend ничего бы
    # не показал. Strict mode → exit non-zero для scheduled task / CI.
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

    # Blob upload non-fatal — slim/active.parquet is consumed downstream by
    # `publish weekly` / `publish snapshots` reading the LOCAL parquet, not
    # the Blob copy. Pre-2026-05-17 this raised 403 while the store was
    # suspended, marking the cron run as failed even though Neon-side
    # publication kept the dashboard live.
    if cfg is not None:
        try:
            _upload_blob(out, "slim/active.parquet", cfg)
        except Exception as exc:
            print(
                f"[slim] blob upload failed for slim/active.parquet: {exc}",
                file=sys.stderr,
            )
    return 0
