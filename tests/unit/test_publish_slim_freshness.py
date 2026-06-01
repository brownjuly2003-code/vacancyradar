"""Slim freshness gate — defends against stalled-ingest silent publishes.

Daily refresh runs `ingest hh → publish slim` on a Win Scheduled Task. If
the ingest step starts failing (network blip, hh.ru change, low-memory
crash) but publish slim still runs, it would happily push days-old data
to Vercel Blob and the frontend would surface stale vacancies as if
they were fresh.

The gate compares max(last_seen_at) against now(); >24h old → warn
(default) or exit 4 (`--strict`).
"""
from __future__ import annotations

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

import src.cli as cli
from src.publish.blob_push import BlobConfig, BlobUploadResult


def _args(*, strict: bool = False, dedup: bool = False, scope: str | None = None) -> Namespace:
    return Namespace(target="slim", dry=False, strict=strict, dedup=dedup, scope=scope)


@pytest.fixture
def patched_publish(monkeypatch, tmp_path, blob_env):
    monkeypatch.chdir(tmp_path)

    upload_calls: list[tuple[Path, str]] = []

    def fake_upload(local_path: Path, pathname: str, cfg: BlobConfig, **_kwargs):
        upload_calls.append((local_path, pathname))
        return BlobUploadResult(
            pathname=pathname,
            url=f"https://blob.example/{pathname}",
            public_url=f"https://blob.example/{pathname}",
            content_type="application/octet-stream",
            response={},
        )

    monkeypatch.setattr("src.publish.blob_push.upload_file", fake_upload)

    def fake_write(_df, out_path: Path):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"\x00")

    monkeypatch.setattr("src.transform.slim_export.write_slim_active", fake_write)
    return upload_calls


def _slim_with_last_seen(last_seen: datetime) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "vacancy_id": ["hh:1"],
            "last_seen_at": [last_seen],
            "first_seen_at": [last_seen],
        }
    )


def test_publish_slim_warns_when_last_seen_old_default(patched_publish, monkeypatch, capsys):
    upload_calls = patched_publish
    stale_seen = datetime.now(timezone.utc) - timedelta(hours=48)
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda _lake, **_kwargs: _slim_with_last_seen(stale_seen),
    )

    exit_code = cli._publish_slim(_args())

    assert exit_code == 0
    assert len(upload_calls) == 1  # default mode still uploads
    captured = capsys.readouterr()
    assert "[warn] slim freshness" in captured.err
    assert "ingest may have stalled" in captured.err


def test_publish_slim_strict_exits_when_last_seen_old(patched_publish, monkeypatch, capsys):
    upload_calls = patched_publish
    stale_seen = datetime.now(timezone.utc) - timedelta(hours=48)
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda _lake, **_kwargs: _slim_with_last_seen(stale_seen),
    )

    exit_code = cli._publish_slim(_args(strict=True))

    assert exit_code == 4
    # Strict failure happens BEFORE upload — caller decides if uploading
    # stale data was intended.
    assert len(upload_calls) == 0
    captured = capsys.readouterr()
    assert "[err]" in captured.err
    assert "exceeds 24h threshold" in captured.err


def test_publish_slim_silent_when_fresh(patched_publish, monkeypatch, capsys):
    upload_calls = patched_publish
    fresh_seen = datetime.now(timezone.utc) - timedelta(hours=2)
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda _lake, **_kwargs: _slim_with_last_seen(fresh_seen),
    )

    exit_code = cli._publish_slim(_args(strict=True))

    assert exit_code == 0
    assert len(upload_calls) == 1
    captured = capsys.readouterr()
    assert "freshness" not in captured.err


def test_publish_slim_silent_when_last_seen_is_null(patched_publish, monkeypatch, capsys):
    upload_calls = patched_publish
    monkeypatch.setattr(
        "src.transform.slim_export.build_slim_active",
        lambda _lake, **_kwargs: pl.DataFrame(
            {
                "vacancy_id": ["hh:1"],
                "last_seen_at": [None],
                "first_seen_at": [None],
            }
        ),
    )

    exit_code = cli._publish_slim(_args(strict=True))

    assert exit_code == 0
    assert len(upload_calls) == 1
    assert "freshness" not in capsys.readouterr().err


def test_publish_slim_passes_market_scope_filter(patched_publish, monkeypatch):
    seen_kwargs: dict = {}
    fresh_seen = datetime.now(timezone.utc) - timedelta(hours=2)

    def fake_build(_lake, **kwargs):
        seen_kwargs.update(kwargs)
        return _slim_with_last_seen(fresh_seen)

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build)

    assert cli._publish_slim(_args(scope="it")) == 0

    assert seen_kwargs["market_scope"] == "it"
