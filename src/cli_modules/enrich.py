"""`vradar enrich {hh-details,embeddings}` implementations.

Extracted from `src/cli.py` per Kimi audit P1-1 (monolithic CLI). Public surface
unchanged — `src.cli` re-exports `_enrich`, `_enrich_hh_details`,
`_enrich_embeddings`, `_cache_size`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _enrich(args: argparse.Namespace) -> int:
    if args.kind == "hh-details":
        return _enrich_hh_details(args)
    if args.kind == "embeddings":
        return _enrich_embeddings(args)
    return 1


def _enrich_hh_details(args: argparse.Namespace) -> int:
    import polars as pl

    from src.ingest.hh_detail import HH_DETAILS_PATH_DEFAULT, fetch_missing_details
    from src.ingest.raw_lake import read_lake

    lake = Path("master/vacancies_raw.parquet")
    cache = HH_DETAILS_PATH_DEFAULT
    scope_name = getattr(args, "scope", None)

    columns = ["vacancy_id"] + (["market_scope"] if scope_name else [])
    df = read_lake(lake, source="hh", columns=columns)
    if df.is_empty():
        print("[err] empty lake — run `vradar ingest hh` first", file=sys.stderr)
        return 3
    if scope_name:
        df = df.filter(pl.col("market_scope") == scope_name)
        if df.is_empty():
            print(
                f"[err] no hh rows tagged market_scope={scope_name} — run `ingest hh --scope {scope_name}` first",
                file=sys.stderr,
            )
            return 3

    ids = df["vacancy_id"].unique().to_list()

    cached_ids: set[str] = set()
    if cache.exists():
        cached_ids = set(pl.read_parquet(cache, columns=["vacancy_id"])["vacancy_id"].to_list())
    ids = [v for v in ids if v not in cached_ids]

    unknown_priority = 0
    slim_path = Path("derived/slim_active.parquet")
    if slim_path.exists() and ids:
        slim = pl.read_parquet(slim_path, columns=["vacancy_id", "source", "seniority"])
        unknown_ids = set(
            slim.filter((pl.col("source") == "hh") & (pl.col("seniority") == "unknown"))[
                "vacancy_id"
            ].to_list()
        )
        if unknown_ids:
            priority = [v for v in ids if v in unknown_ids]
            rest = [v for v in ids if v not in unknown_ids]
            ids = priority + rest
            unknown_priority = len(priority)

    if args.limit:
        ids = ids[: args.limit]

    scope_label = f" scope={scope_name}" if scope_name else ""
    priority_label = (
        f" (unknown_priority={min(unknown_priority, len(ids))})" if unknown_priority else ""
    )
    print(
        f"[enrich]{scope_label}{priority_label} {len(ids)} hh ids (non-cached) "
        f"→ cache {cache} (rate {args.rate}s/req)"
    )
    n = fetch_missing_details(ids, cache, rate_limit_sec=args.rate)
    attempted = len(ids)
    failed = max(0, attempted - n)
    failure_rate = (failed / attempted) if attempted else 0.0
    print(
        f"[enrich] detail_summary attempted={attempted} fetched={n} "
        f"failed={failed} failure_rate={failure_rate:.3f}"
    )
    print(f"[enrich] fetched {n} new detail(s); cache now has {_cache_size(cache)} entries total")
    return 0


def _enrich_embeddings(args: argparse.Namespace) -> int:
    import numpy as np

    from src.enrich.embeddings import (
        DEFAULT_LANCE_PATH,
        EMBEDDING_DIM,
        EmbeddingRow,
        encode_texts,
        needs_reencode,
        read_existing_vectors,
        write_lance_arrays,
    )
    from src.transform.slim_export import build_slim_active

    lake = Path("master/vacancies_raw.parquet")
    slim = build_slim_active(lake, limit=args.limit)
    if slim.is_empty():
        print("[err] empty slim — run `vradar ingest hh` first", file=sys.stderr)
        return 3

    rows: list[EmbeddingRow] = []
    for r in slim.iter_rows(named=True):
        text_parts = [r.get("title") or "", r.get("description_teaser") or ""]
        text = " ".join(p for p in text_parts if p).strip()
        if not text:
            continue
        rows.append(EmbeddingRow(vacancy_id=r["vacancy_id"], text=text))

    if not rows:
        print("[err] no slim rows with non-empty text", file=sys.stderr)
        return 3

    if args.limit:
        rows = rows[: args.limit]

    if args.force:
        existing_ids: list[str] = []
        existing_hashes: list[str] = []
        existing_vecs = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    else:
        existing_ids, existing_hashes, existing_vecs = read_existing_vectors(DEFAULT_LANCE_PATH)
    existing_map = dict(zip(existing_ids, existing_hashes))

    todo = rows if args.force else needs_reencode(rows, existing_map)

    print(
        f"[embeddings] slim={len(rows)} | existing_lance={len(existing_ids)} | "
        f"to_encode={len(todo)} (force={args.force})"
    )
    if not todo:
        print("[embeddings] nothing to encode — all hashes match. Use --force to re-encode.")
        return 0

    print("[embeddings] loading model (first run downloads ~1 GB)...")
    new_vectors = encode_texts([r.text for r in todo], batch_size=args.batch_size)
    print(f"[embeddings] encoded {len(new_vectors)} vectors")

    # Combine new (todo) с unchanged-частью existing store, иначе overwrite
    # потеряет embeddings всех вакансий вне todo. todo wins по vacancy_id.
    todo_ids = {r.vacancy_id for r in todo}
    keep_idx = [i for i, vid in enumerate(existing_ids) if vid not in todo_ids]
    keep_vecs = (
        existing_vecs[keep_idx]
        if keep_idx
        else np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    )
    combined_ids = [r.vacancy_id for r in todo] + [existing_ids[i] for i in keep_idx]
    combined_hashes = [r.text_hash() for r in todo] + [existing_hashes[i] for i in keep_idx]
    combined_vecs = (
        np.concatenate([new_vectors, keep_vecs]) if len(keep_vecs) else new_vectors
    )

    n = write_lance_arrays(combined_ids, combined_hashes, combined_vecs, DEFAULT_LANCE_PATH)
    print(
        f"[embeddings] wrote {n} rows → {DEFAULT_LANCE_PATH} "
        f"(reused {len(keep_idx)} existing, new {len(todo)})"
    )
    return 0


def _cache_size(path: Path) -> int:
    if not path.exists():
        return 0
    import polars as pl

    return pl.read_parquet(path).height
