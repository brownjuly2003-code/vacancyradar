"""TDD для Vercel Blob TTL pass (Phase 5 tech-debt #2)."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from src.publish.blob_push import BlobConfig
from src.publish.blob_ttl import (
    BlobMeta,
    delete_blobs,
    list_blobs,
    parse_partition_date,
    prune_events_30d,
)


def _cfg() -> BlobConfig:
    return BlobConfig(
        token="test-token",
        public_base_url="https://test.public.blob.vercel-storage.com",
    )


def _meta(pathname: str, url: str | None = None) -> BlobMeta:
    return BlobMeta(
        pathname=pathname,
        url=url or f"https://blob.vercel-storage.com/{pathname}",
        size=1024,
        uploaded_at="2026-04-27T00:00:00.000Z",
    )


class TestParsePartitionDate:
    def test_hive_layout_extracts_date(self):
        assert parse_partition_date(
            "slim/events_30d/year=2026/month=04/day=15/events.parquet"
        ) == date(2026, 4, 15)

    def test_no_match_returns_none(self):
        assert parse_partition_date("slim/active.parquet") is None
        assert parse_partition_date("random/path.parquet") is None

    def test_invalid_date_returns_none(self):
        assert parse_partition_date("year=2026/month=13/day=01/x.parquet") is None
        assert parse_partition_date("year=2026/month=02/day=31/x.parquet") is None


class TestListBlobs:
    def test_single_page_returns_all_blobs(self):
        get = MagicMock()
        get.return_value.json.return_value = {
            "blobs": [
                {"pathname": "slim/active.parquet", "url": "https://x/y", "size": 100, "uploadedAt": "2026-04-27"},
            ],
            "hasMore": False,
        }
        get.return_value.raise_for_status = lambda: None
        result = list_blobs("slim/", _cfg(), get=get)
        assert len(result) == 1
        assert result[0].pathname == "slim/active.parquet"
        get.assert_called_once()
        # auth header must be present
        _, kwargs = get.call_args
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")
        assert kwargs["params"]["prefix"] == "slim/"

    def test_paginates_via_cursor(self):
        get = MagicMock()
        get.return_value.raise_for_status = lambda: None
        get.return_value.json.side_effect = [
            {"blobs": [{"pathname": "a.parquet", "url": "u1"}], "hasMore": True, "cursor": "c1"},
            {"blobs": [{"pathname": "b.parquet", "url": "u2"}], "hasMore": False},
        ]
        result = list_blobs("slim/", _cfg(), get=get)
        assert [b.pathname for b in result] == ["a.parquet", "b.parquet"]
        assert get.call_count == 2

    def test_empty_response(self):
        get = MagicMock()
        get.return_value.raise_for_status = lambda: None
        get.return_value.json.return_value = {"blobs": [], "hasMore": False}
        assert list_blobs("slim/", _cfg(), get=get) == []


class TestDeleteBlobs:
    def test_posts_urls(self):
        post = MagicMock()
        post.return_value.raise_for_status = lambda: None
        delete_blobs(["https://x/a", "https://x/b"], _cfg(), post=post)
        post.assert_called_once()
        _, kwargs = post.call_args
        assert kwargs["json"] == {"urls": ["https://x/a", "https://x/b"]}
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_empty_list_is_noop(self):
        post = MagicMock()
        delete_blobs([], _cfg(), post=post)
        post.assert_not_called()


class TestPruneEvents30d:
    def test_dry_run_does_not_call_delete(self):
        old = _meta("slim/events_30d/year=2026/month=01/day=15/events.parquet")
        new = _meta("slim/events_30d/year=2026/month=04/day=20/events.parquet")
        delete = MagicMock()
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            dry_run=True,
            list_fn=lambda: [old, new],
            delete_fn=delete,
        )
        assert result.kept == 1
        assert result.pruned == 1
        assert old.pathname in result.pruned_pathnames
        assert result.dry_run is True
        delete.assert_not_called()

    def test_apply_calls_delete_with_old_urls(self):
        old = _meta("slim/events_30d/year=2026/month=01/day=15/events.parquet")
        new = _meta("slim/events_30d/year=2026/month=04/day=20/events.parquet")
        delete = MagicMock()
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            dry_run=False,
            list_fn=lambda: [old, new],
            delete_fn=delete,
        )
        assert result.pruned == 1
        delete.assert_called_once_with([old.url])

    def test_keeps_partition_exactly_at_cutoff(self):
        """Партиция с date == cutoff остаётся (>= cutoff)."""
        cutoff = date(2026, 3, 28)  # today - 30 days = 2026-04-27 - 30 = 2026-03-28
        at_cutoff = _meta("slim/events_30d/year=2026/month=03/day=28/events.parquet")
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            dry_run=True,
            list_fn=lambda: [at_cutoff],
            delete_fn=MagicMock(),
        )
        assert result.kept == 1
        assert result.pruned == 0
        assert result.cutoff == cutoff

    def test_unparseable_pathname_is_skipped_not_deleted(self):
        """Safety: blob с pathname без YYYY/MM/DD НЕ удаляется."""
        weird = _meta("slim/events_30d/legacy_format.parquet")
        delete = MagicMock()
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            dry_run=False,
            list_fn=lambda: [weird],
            delete_fn=delete,
        )
        assert result.pruned == 0
        assert weird.pathname in result.skipped_unparseable
        delete.assert_not_called()

    def test_empty_blob_list(self):
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            list_fn=lambda: [],
            delete_fn=MagicMock(),
        )
        assert result.kept == 0
        assert result.pruned == 0

    def test_custom_keep_days(self):
        old = _meta("slim/events_30d/year=2026/month=04/day=20/events.parquet")
        result = prune_events_30d(
            _cfg(),
            today=date(2026, 4, 27),
            keep_days=3,  # cutoff = 2026-04-24
            dry_run=True,
            list_fn=lambda: [old],
            delete_fn=MagicMock(),
        )
        assert result.pruned == 1
        assert result.cutoff == date(2026, 4, 24)
