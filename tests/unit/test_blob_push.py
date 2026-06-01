from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import requests

from src.publish.blob_push import (
    BlobConfig,
    BlobStoreSuspendedError,
    public_url,
    reset_suspended_cache,
    upload_file,
)


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_suspended_cache()
    yield
    reset_suspended_cache()


class FakeResponse:
    def __init__(self, status_code: int = 200, json_body: dict | None = None,
                 content: bytes | None = None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content if content is not None else b'{"ok":true}'

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._json


@pytest.fixture
def cfg() -> BlobConfig:
    return BlobConfig(
        token="vercel_blob_rw_TEST_xyz",
        public_base_url="https://teststore.public.blob.vercel-storage.com",
    )


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    p = tmp_path / "data.parquet"
    p.write_bytes(b"hello-blob")
    return p


def test_public_url_concatenation():
    assert public_url("slim/active.parquet", "https://x.example.com") == \
        "https://x.example.com/slim/active.parquet"
    # tolerate trailing slash on base + leading on path
    assert public_url("/slim/active.parquet", "https://x.example.com/") == \
        "https://x.example.com/slim/active.parquet"


def test_upload_sends_put_with_correct_url_and_headers(cfg: BlobConfig, sample_file: Path):
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers")
        captured["data"] = kwargs.get("data").read() if hasattr(kwargs.get("data"), "read") else kwargs.get("data")
        captured["timeout"] = kwargs.get("timeout")
        return FakeResponse(200, {
            "pathname": "slim/active.parquet",
            "url": "https://blob.vercel-storage.com/slim/active.parquet",
            "contentType": "application/octet-stream",
        })

    result = upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)

    assert captured["url"] == "https://blob.vercel-storage.com/slim/active.parquet"
    assert captured["headers"]["Authorization"] == "Bearer vercel_blob_rw_TEST_xyz"
    assert captured["headers"]["x-content-type"] == "application/octet-stream"
    # Vercel quirk: control flags must travel as headers, not query params
    assert captured["headers"]["x-add-random-suffix"] == "0"
    assert captured["headers"]["x-allow-overwrite"] == "1"
    assert captured["data"] == b"hello-blob"
    assert captured["timeout"] == cfg.timeout
    assert result.public_url == "https://teststore.public.blob.vercel-storage.com/slim/active.parquet"
    assert result.content_type == "application/octet-stream"


def test_upload_strips_leading_slash_from_pathname(cfg: BlobConfig, sample_file: Path):
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kwargs):
        captured["url"] = url
        return FakeResponse(200, {})

    upload_file(sample_file, "/slim/active.parquet", cfg, put=fake_put)
    assert captured["url"] == "https://blob.vercel-storage.com/slim/active.parquet"


def test_upload_without_overwrite_omits_allow_header(cfg: BlobConfig, sample_file: Path):
    captured: dict[str, Any] = {}

    def fake_put(url: str, **kwargs):
        captured["headers"] = kwargs.get("headers")
        return FakeResponse(200, {})

    upload_file(sample_file, "slim/active.parquet", cfg, allow_overwrite=False, put=fake_put)
    assert "x-allow-overwrite" not in captured["headers"]
    assert captured["headers"]["x-add-random-suffix"] == "0"


def test_upload_raises_on_4xx(cfg: BlobConfig, sample_file: Path):
    def fake_put(url: str, **kwargs):
        return FakeResponse(409)

    with pytest.raises(requests.HTTPError):
        upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)


def test_upload_missing_file_raises_filenotfound(cfg: BlobConfig, tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        upload_file(tmp_path / "nope.parquet", "slim/active.parquet", cfg)


def test_upload_handles_empty_response_body(cfg: BlobConfig, sample_file: Path):
    def fake_put(url: str, **kwargs):
        return FakeResponse(200, {}, content=b"")

    result = upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)
    assert result.pathname == "slim/active.parquet"
    assert result.public_url.endswith("slim/active.parquet")


def test_403_trips_circuit_breaker_and_raises_suspended_error(
    cfg: BlobConfig, sample_file: Path
):
    calls: list[str] = []

    def fake_put(url: str, **kwargs):
        calls.append(url)
        return FakeResponse(403, content=b'{"error":{"code":"store_suspended"}}')

    with pytest.raises(BlobStoreSuspendedError) as excinfo:
        upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)

    assert "slim/active.parquet" in str(excinfo.value)
    assert "disabling further uploads" in str(excinfo.value)
    assert len(calls) == 1


def test_403_without_store_suspended_does_not_trip_breaker(
    cfg: BlobConfig, sample_file: Path
):
    calls: list[str] = []

    def fake_put(url: str, **kwargs):
        calls.append(url)
        return FakeResponse(403, content=b'{"error":{"code":"forbidden"}}')

    with pytest.raises(requests.HTTPError):
        upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)

    with pytest.raises(requests.HTTPError):
        upload_file(sample_file, "agg/weekly.parquet", cfg, put=fake_put)

    assert len(calls) == 2


def test_breaker_short_circuits_subsequent_uploads_without_http(
    cfg: BlobConfig, sample_file: Path
):
    calls: list[str] = []

    def fake_put(url: str, **kwargs):
        calls.append(url)
        return FakeResponse(403, content=b'{"error":{"code":"store_suspended"}}')

    with pytest.raises(BlobStoreSuspendedError):
        upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)

    with pytest.raises(BlobStoreSuspendedError) as excinfo:
        upload_file(sample_file, "agg/weekly.parquet", cfg, put=fake_put)

    assert "previously returned 403" in str(excinfo.value)
    assert "agg/weekly.parquet" in str(excinfo.value)
    assert len(calls) == 1  # second upload never made HTTP request


def test_reset_suspended_cache_clears_breaker(cfg: BlobConfig, sample_file: Path):
    def fail_then_succeed(url: str, **kwargs):
        fail_then_succeed.n += 1  # type: ignore[attr-defined]
        if fail_then_succeed.n == 1:  # type: ignore[attr-defined]
            return FakeResponse(403, content=b'{"error":{"code":"store_suspended"}}')
        return FakeResponse(200, {"pathname": "slim/active.parquet"})

    fail_then_succeed.n = 0  # type: ignore[attr-defined]

    with pytest.raises(BlobStoreSuspendedError):
        upload_file(sample_file, "slim/active.parquet", cfg, put=fail_then_succeed)

    reset_suspended_cache()

    result = upload_file(
        sample_file, "slim/active.parquet", cfg, put=fail_then_succeed
    )
    assert result.pathname == "slim/active.parquet"


def test_breaker_does_not_mask_missing_file(cfg: BlobConfig, tmp_path: Path):
    """When the breaker is tripped, a missing-file call must still surface
    FileNotFoundError rather than the suspended exception (so callers can
    distinguish 'no input to upload' from 'store dead')."""
    sample = tmp_path / "exists.parquet"
    sample.write_bytes(b"x")

    def fake_put(url: str, **kwargs):
        return FakeResponse(403, content=b'{"error":{"code":"store_suspended"}}')

    with pytest.raises(BlobStoreSuspendedError):
        upload_file(sample, "slim/active.parquet", cfg, put=fake_put)

    # Breaker is now tripped. A missing-file call should raise FileNotFoundError,
    # NOT BlobStoreSuspendedError — local inputs are checked first.
    missing = tmp_path / "nope.parquet"
    with pytest.raises(FileNotFoundError):
        upload_file(missing, "agg/weekly.parquet", cfg, put=fake_put)


def test_non_403_4xx_does_not_trip_breaker(cfg: BlobConfig, sample_file: Path):
    calls: list[str] = []

    def fake_put(url: str, **kwargs):
        calls.append(url)
        return FakeResponse(500)

    with pytest.raises(requests.HTTPError):
        upload_file(sample_file, "slim/active.parquet", cfg, put=fake_put)

    # Second attempt still tries HTTP — breaker was not tripped by 500.
    with pytest.raises(requests.HTTPError):
        upload_file(sample_file, "agg/weekly.parquet", cfg, put=fake_put)

    assert len(calls) == 2
