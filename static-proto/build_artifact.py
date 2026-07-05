"""Build the browser data artifact for the static VacancyRadar storefront.

Reads slim_active.parquet (local path or the live HF mirror) and emits a
compact gzipped JSON the page loads + filters entirely client-side — no DB.

Usage:
    python build_artifact.py [parquet_path] [out_dir]

Defaults: pull from the HF dataset mirror, write data.json.gz next to this file.
Run it on the same schedule that refreshes the HF mirror to keep the page fresh.

Safety: the build refuses to emit an artifact with fewer than MIN_ROWS rows
(env `VR_MIN_ROWS`, default 20000) so a truncated or empty upstream parquet can
never silently replace the live storefront with an empty page. Same absolute
floor philosophy as the Neon-sync shrinkage guard.
"""
from __future__ import annotations
import sys
import os
import json
import gzip
import re
import urllib.request
import tempfile

import polars as pl

HF_BASE = "https://huggingface.co/datasets/liovina/vacancyradar-data/resolve/main"
HF_URL = f"{HF_BASE}/slim/active.parquet"
# Weekly pre-aggregated trends (market history) — published to the HF mirror,
# so the dashboard's time-dynamics work entirely from the cloud artifact.
# (market_pulse is intentionally not used: total_active/disclosure/age are only
# populated on the latest run and closed is cumulative — no honest daily series.)
HF_TRENDS = {
    "salary": f"{HF_BASE}/agg/weekly_role_salary.parquet",
    "skills": f"{HF_BASE}/agg/weekly_skill_velocity.parquet",
}
SAL_LEVELS = ["junior", "middle", "senior", "lead"]
# Daily intake "pulse" — built from the raw events_30d source (Hive-partitioned),
# so it reads the whole tree via the HF dataset API, not a single resolve URL.
HF_DATASET_REPO = "liovina/vacancyradar-data"
HF_EVENTS_PREFIX = "slim/events_30d/"
# Trailing FULL days shown in the pulse. 18 keeps the settled twice-daily-sweep
# regime and drops the early one-off backfill spikes (e.g. 06-04/05 ~5.7k vs the
# ~2k norm) that would otherwise crush the y-scale.
PULSE_DAYS = 18
HERE = os.path.dirname(os.path.abspath(__file__))

# Absolute floor: a healthy active corpus is ~42-52k rows; refuse to publish an
# artifact below this so a truncated pull can't blank the live page.
MIN_ROWS = int(os.environ.get("VR_MIN_ROWS", "20000"))


class ArtifactTooSmallError(RuntimeError):
    """Raised when the built artifact is below MIN_ROWS — never written/deployed."""

# Markdown + emoji/pictograph stripping so Telegram-sourced titles read clean.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF"
    "←-⇿⌀-⏿⬀-⯿‍♀♂⚕❤️]+",
    flags=re.UNICODE,
)

def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("​", "").replace("﻿", "")
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # [text](url) -> text
    s = re.sub(r"\]\([^)\s]*\)?", "", s)              # dangling ](url fragment
    s = re.sub(r"https?://\S+", "", s)                # bare URLs
    s = re.sub(r"[*_`>#\[\]]+", "", s)                # md emphasis / headers / leftover brackets
    s = _EMOJI.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" \t-–—|·•:")
    return s

def load(parquet: str) -> pl.DataFrame:
    if parquet.startswith("http"):
        tmp = os.path.join(tempfile.gettempdir(), "vr_active.parquet")
        print(f"downloading {parquet}")
        urllib.request.urlretrieve(parquet, tmp)
        parquet = tmp
    return pl.read_parquet(parquet)

def build_rows(df: pl.DataFrame) -> list[dict]:
    """Project + clean the slim parquet into the compact browser row dicts."""
    def to_date(c):
        return pl.col(c).dt.strftime("%Y-%m-%d")

    slim = df.select([
        pl.col("vacancy_id").alias("id"),
        pl.col("title").fill_null(""),
        pl.col("employer_name").alias("e"),
        pl.col("city").alias("c"),
        pl.col("region").alias("rg"),
        pl.col("salary_rub_min").alias("smin"),
        pl.col("salary_rub_max").alias("smax"),
        pl.col("remote_type").fill_null("unknown").alias("rt"),
        pl.col("seniority").fill_null("unknown").alias("sr"),
        pl.col("source").alias("src"),
        pl.col("source_url").alias("u"),
        pl.col("skills").alias("sk"),
        to_date("last_seen_at").alias("ls"),
        to_date("posted_at").alias("ps"),
        pl.col("description_teaser").fill_null("").alias("_ds"),
    ])

    rows_in = slim.to_dicts()
    rows = []
    for r in rows_in:
        title = _clean(r.pop("title"))
        ds = _clean(r.pop("_ds"))
        # title that cleaned away to nothing (emoji/links only): rescue from
        # the teaser, or drop the row if there is nothing usable left.
        if not title:
            if ds:
                title = ds[:80].rstrip() + ("…" if len(ds) > 80 else "")
                ds = ""
            else:
                continue
        # drop teaser that just repeats the title
        if ds and ds.lower().startswith(title.lower()):
            ds = ds[len(title):].strip(" \t-–—|·•:")
        r["t"] = title
        if len(ds) > 160:
            ds = ds[:160].rstrip() + "…"
        if ds:
            r["ds"] = ds
        out = {}
        for k, v in r.items():
            if v is None or v == "":
                continue
            if k == "sk" and not v:
                continue
            out[k] = v
        rows.append(out)
    return rows


def build_trends(salary: pl.DataFrame, skills: pl.DataFrame) -> dict:
    """Compact market time-series from the weekly aggregates.

    - salary:          market-wide weekly median (weighted by vacancy count) + p25/p75.
    - salary_by_level: weekly weighted median per seniority grade (multi-line).
    - skills:          top movers (up/down) of the latest week.
    """
    out: dict = {}

    # Salary trend — weighted median proxy per week (mean of role medians
    # weighted by n_vacancies; robust enough for a market-wide trend line).
    if salary.height:
        s = (
            salary.filter(pl.col("salary_rub_median").is_not_null() & (pl.col("n_vacancies") > 0))
            .group_by("week_start")
            .agg(
                med=(pl.col("salary_rub_median") * pl.col("n_vacancies")).sum() / pl.col("n_vacancies").sum(),
                p25=(pl.col("salary_rub_p25") * pl.col("n_vacancies")).sum() / pl.col("n_vacancies").sum(),
                p75=(pl.col("salary_rub_p75") * pl.col("n_vacancies")).sum() / pl.col("n_vacancies").sum(),
                n=pl.col("n_vacancies").sum(),
            )
            .sort("week_start")
        )
        out["salary"] = [
            {
                "w": r["week_start"].strftime("%Y-%m-%d") if hasattr(r["week_start"], "strftime") else str(r["week_start"]),
                "med": round(r["med"]),
                "p25": round(r["p25"]),
                "p75": round(r["p75"]),
                "n": r["n"],
            }
            for r in s.iter_rows(named=True)
        ]
    else:
        out["salary"] = []

    # Salary by seniority over time — vacancy-weighted weekly median per grade;
    # only grades with enough weekly history are emitted (clean multi-line).
    out["salary_by_level"] = {"weeks": [], "series": {}}
    if salary.height:
        lv = (
            salary.filter(
                pl.col("salary_rub_median").is_not_null()
                & (pl.col("n_vacancies") > 0)
                & pl.col("seniority").is_in(SAL_LEVELS)
            )
            .group_by(["week_start", "seniority"])
            .agg(
                med=(pl.col("salary_rub_median") * pl.col("n_vacancies")).sum() / pl.col("n_vacancies").sum(),
                n=pl.col("n_vacancies").sum(),
            )
            .filter(pl.col("n") >= 15)
            .sort("week_start")
        )
        if lv.height:
            weeks = sorted({r["week_start"] for r in lv.iter_rows(named=True)})
            wkey = [w.strftime("%Y-%m-%d") if hasattr(w, "strftime") else str(w) for w in weeks]
            series = {}
            for lvl in SAL_LEVELS:
                by_week = {
                    r["week_start"]: round(r["med"])
                    for r in lv.filter(pl.col("seniority") == lvl).iter_rows(named=True)
                }
                if len(by_week) >= 3:  # enough points to be a line
                    series[lvl] = [by_week.get(w) for w in weeks]
            out["salary_by_level"] = {"weeks": wkey, "series": series}

    # Skill velocity — latest week movers (significant mentions only).
    if skills.height:
        last_week = skills.select(pl.col("week_start").max()).item()
        cur = skills.filter(
            (pl.col("week_start") == last_week)
            & (pl.col("delta_pct").is_not_null())
            & (pl.col("mentions_this_week") >= 25)
        )
        def _mover(row):
            return {
                "s": row["skill"],
                "n": row["mentions_this_week"],
                "prev": row["mentions_prev_week"],
                "d": round(row["delta_pct"], 1),
            }
        up = cur.sort("delta_pct", descending=True).head(8)
        down = cur.sort("delta_pct").head(8)
        out["skills"] = {
            "week": last_week.strftime("%Y-%m-%d") if hasattr(last_week, "strftime") else str(last_week),
            "up": [_mover(r) for r in up.iter_rows(named=True)],
            "down": [_mover(r) for r in down.iter_rows(named=True)],
        }
    else:
        out["skills"] = {"week": None, "up": [], "down": []}

    return out


def load_events() -> pl.DataFrame:
    """Download + concat the Hive-partitioned events_30d parquet from the HF mirror.

    events_30d is a directory tree (one parquet per day), so it's read via the
    HF dataset file listing, not a single resolve URL. `huggingface_hub` is
    already a build/CI dependency (the deploy step uses it). Only the three
    columns the pulse needs are read. Returns an empty frame on any failure so
    the pulse degrades gracefully without failing the main artifact.
    """
    from huggingface_hub import HfApi, hf_hub_download  # already a CI dep

    api = HfApi()
    files = sorted(
        f
        for f in api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
        if f.startswith(HF_EVENTS_PREFIX) and f.endswith(".parquet")
    )
    frames = []
    for f in files:
        p = hf_hub_download(repo_id=HF_DATASET_REPO, repo_type="dataset", filename=f)
        frames.append(pl.read_parquet(p, columns=["ts", "type", "source"]))
    if not frames:
        return pl.DataFrame(schema={"ts": pl.Datetime, "type": pl.Utf8, "source": pl.Utf8})
    return pl.concat(frames, how="vertical_relaxed")


def build_pulse(events: pl.DataFrame, today, days: int = PULSE_DAYS) -> dict:
    """Honest daily intake of NEW vacancies (`appeared` events) per source.

    Only `appeared` is used. `closed` is structurally broken in this source —
    its daily counts run cumulative and far exceed the whole active corpus
    (e.g. 95k "closed" in a day vs ~53k active), the same dishonesty that
    retired the market_pulse chart — so it is deliberately not surfaced.

    Honesty rules baked in:
    - the current (partial) UTC day is excluded — only fully-elapsed days count,
      so the latest bar never reads as a false drop;
    - the window is the last `days` complete days, which drops the one-off
      sweep-backfill spikes from the noisy early history;
    - genuine collection-outage dips (an hh fetch that mostly failed) are KEPT —
      they are real, and the UI labels them.
    """
    empty = {"days": [], "hh": [], "tg": [], "ma7": [], "window_days": days, "asof": None}
    if not events.height:
        return empty
    from datetime import timedelta

    start = today - timedelta(days=days)
    daily = (
        events.filter(pl.col("type") == "appeared")
        .with_columns(pl.col("ts").cast(pl.Datetime).dt.date().alias("d"))
        .filter((pl.col("d") >= start) & (pl.col("d") < today))  # full days only
        .group_by(["d", "source"])
        .len()
        .sort("d")
    )
    if not daily.height:
        return empty
    wide = daily.pivot(values="len", index="d", on="source").sort("d")
    days_list = [d.strftime("%Y-%m-%d") for d in wide["d"].to_list()]

    def col(name: str) -> list[int]:
        if name not in wide.columns:
            return [0] * wide.height
        return [int(x or 0) for x in wide[name].to_list()]

    hh, tg = col("hh"), col("tg")
    total = [hh[i] + tg[i] for i in range(len(days_list))]
    # 7-day trailing moving average over present days (gaps are rare in the
    # settled regime and labeled honestly in the UI).
    ma7 = []
    for i in range(len(total)):
        win = total[max(0, i - 6): i + 1]
        ma7.append(round(sum(win) / len(win)) if win else None)
    return {
        "days": days_list,
        "hh": hh,
        "tg": tg,
        "ma7": ma7,
        "window_days": days,
        "asof": days_list[-1] if days_list else None,
    }


def _write_gz(obj, out_dir: str, name: str) -> int:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    gz = gzip.compress(raw, 9)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "wb") as f:
        f.write(gz)
    print(f"{name}: raw={len(raw)/1e6:.2f}MB gz={len(gz)/1e6:.2f}MB -> {path}")
    return len(gz)


def main() -> int:
    parquet = sys.argv[1] if len(sys.argv) > 1 else HF_URL
    out_dir = sys.argv[2] if len(sys.argv) > 2 else HERE
    df = load(parquet)
    rows = build_rows(df)

    # Floor guard: never overwrite the live artifact with a truncated/empty pull.
    if len(rows) < MIN_ROWS:
        raise ArtifactTooSmallError(
            f"built only {len(rows)} rows (< MIN_ROWS={MIN_ROWS}); refusing to "
            f"write data.json.gz so a bad upstream parquet can't blank the page"
        )

    print(f"rows={len(rows)}")
    _write_gz(rows, out_dir, "data.json.gz")

    # Market trends (time dynamics) — best-effort: the storefront search works
    # without it, and the dashboard degrades gracefully if trends.json.gz is
    # absent, so a weekly-aggregate hiccup must never fail the main artifact.
    try:
        trends = build_trends(
            load(HF_TRENDS["salary"]),
            load(HF_TRENDS["skills"]),
        )
        # Daily intake pulse — independently guarded so an events_30d hiccup
        # only drops the pulse, not the (separate) weekly salary/skill trends.
        try:
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).date()
            trends["pulse"] = build_pulse(load_events(), today)
            print(f"pulse: {len(trends['pulse']['days'])} days, asof {trends['pulse']['asof']}")
        except Exception as e:  # noqa: BLE001 — pulse is optional
            print(f"WARN: pulse skipped ({type(e).__name__}: {e})")
            trends["pulse"] = build_pulse(pl.DataFrame(), None)
        _write_gz(trends, out_dir, "trends.json.gz")
    except Exception as e:  # noqa: BLE001 — trends are optional, log and continue
        print(f"WARN: trends artifact skipped ({type(e).__name__}: {e})")

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
