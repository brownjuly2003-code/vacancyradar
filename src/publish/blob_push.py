"""Push files to Vercel Blob via REST API.

Vercel Blob is NOT s3-compatible — это REST endpoint:
  PUT https://blob.vercel-storage.com/<pathname>
       Authorization: Bearer <token>
       x-content-type: <mime>
       x-add-random-suffix: 0     ← keep deterministic pathname (idempotent overwrite)
       x-allow-overwrite: 1       ← required when pathname already exists; otherwise 409
       body: raw file bytes

Quirk: Vercel ignores `?addRandomSuffix=0` query string, parameters MUST be
sent as `x-*` request headers. Otherwise the API silently appends a random
suffix to the stored URL while still returning 200 + the original pathname,
and the deterministic public URL 404s.

Public read URL (no auth):
  https://<store-id-lowercase>.public.blob.vercel-storage.com/<pathname>
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlobConfig:
    token: str
    public_base_url: str
    api_base: str = "https://blob.vercel-storage.com"
    timeout: float = 300.0


@dataclass(frozen=True)
class BlobUploadResult:
    pathname: str
    url: str
    public_url: str
    content_type: str | None
    response: dict[str, Any]


class BlobStoreSuspendedError(Exception):
    """Raised when Vercel Blob returns 403 (store_suspended / forbidden)."""


# Process-scoped circuit breaker. Once Vercel Blob returns 403, subsequent
# upload_file calls in the same process raise BlobStoreSuspendedError
# without making the HTTP request. Reset between processes (i.e. fresh on
# each cron run) so the breaker self-heals if the store comes back online.
_suspended: bool = False


def reset_suspended_cache() -> None:
    """Clear the process-scoped suspended flag. Intended for tests."""
    global _suspended
    _suspended = False


def public_url(pathname: str, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/{pathname.lstrip('/')}"


def upload_file(
    local_path: Path,
    pathname: str,
    cfg: BlobConfig,
    *,
    content_type: str = "application/octet-stream",
    allow_overwrite: bool = True,
    put: Callable[..., requests.Response] = requests.put,
) -> BlobUploadResult:
    """PUT one file to Vercel Blob; return upload metadata."""
    global _suspended
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    if _suspended:
        raise BlobStoreSuspendedError(
            f"blob store previously returned 403 in this process; skipping {pathname}"
        )
    pathname = pathname.lstrip("/")
    url = f"{cfg.api_base}/{pathname}"
    headers = {
        "Authorization": f"Bearer {cfg.token}",
        "x-content-type": content_type,
        "x-add-random-suffix": "0",
    }
    if allow_overwrite:
        headers["x-allow-overwrite"] = "1"
    with local_path.open("rb") as f:
        response = put(
            url,
            headers=headers,
            data=f,
            timeout=cfg.timeout,
        )
    if response.status_code == 403 and b"store_suspended" in (response.content or b""):
        _suspended = True
        raise BlobStoreSuspendedError(
            f"blob store returned 403 for {pathname}; "
            "disabling further uploads in this process"
        )
    response.raise_for_status()
    body = response.json() if response.content else {}
    return BlobUploadResult(
        pathname=body.get("pathname", pathname),
        url=body.get("url", url),
        public_url=public_url(pathname, cfg.public_base_url),
        content_type=body.get("contentType") or content_type,
        response=body,
    )
