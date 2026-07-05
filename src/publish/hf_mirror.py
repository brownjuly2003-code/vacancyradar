from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class HfMirrorConfig:
    repo_id: str
    token: str
    revision: str = "main"
    repo_type: str = "dataset"
    timeout: float = 900.0
    attempts: int = 3
    backoff_seconds: float = 5.0


# Stderr substrings seen on transient network failures (DNS, TCP reset, gateway).
# 2026-05-25 cron lost both hf-mirror runs to a single getaddrinfo blip.
# Bare "ConnectionError" was intentionally dropped — too broad (matches any
# class with that substring in name). Specific subclasses below cover the real
# transient cases without false-positiving on hypothetical auth wrappers.
_TRANSIENT_PATTERNS = (
    "getaddrinfo failed",
    "NameResolutionError",
    "Failed to resolve",
    "Failed to establish a new connection",
    "Temporary failure in name resolution",
    "Connection reset",
    "ConnectionResetError",
    "NewConnectionError",
    "requests.exceptions.ConnectionError",
    "ConnectTimeout",
    "ConnectTimeoutError",
    "ReadTimeoutError",
    "Read timed out",
    "Remote end closed connection",
    "502 Server Error",
    "503 Server Error",
    "504 Gateway Timeout",
)


def _is_transient_stderr(stderr: str | None) -> bool:
    if not stderr:
        return False
    return any(pattern in stderr for pattern in _TRANSIENT_PATTERNS)


@dataclass(frozen=True)
class HfUploadItem:
    local_path: Path
    path_in_repo: str


def public_base_url(repo_id: str, *, revision: str = "main") -> str:
    return f"https://huggingface.co/datasets/{repo_id}/resolve/{revision}"


def missing_required_paths(root: Path = Path(".")) -> list[Path]:
    # weekly_role_salary is the canary for the whole agg/ family — it is what
    # the storefront's time-dynamics section actually consumes.
    required = [
        Path("derived/slim_active.parquet"),
        Path("derived/agg/weekly_role_salary.parquet"),
    ]
    return [path for path in required if not (root / path).exists()]


def build_upload_plan(root: Path = Path(".")) -> list[HfUploadItem]:
    derived = root / "derived"
    items = [
        HfUploadItem(derived / "slim_active.parquet", "slim/active.parquet"),
    ]
    agg = derived / "agg"
    if agg.exists():
        items.append(HfUploadItem(agg, "agg"))
    events = derived / "slim_events_30d"
    if events.exists():
        items.append(HfUploadItem(events, "slim/events_30d"))
    return [item for item in items if item.local_path.exists()]


def upload_items(
    items: Sequence[HfUploadItem],
    cfg: HfMirrorConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    env = os.environ.copy()
    env["HF_TOKEN"] = cfg.token
    if cfg.attempts < 1:
        raise ValueError("attempts must be >= 1")
    for item in items:
        cmd = [
            "huggingface-cli",
            "upload",
            cfg.repo_id,
            str(item.local_path),
            item.path_in_repo,
            "--repo-type",
            cfg.repo_type,
            "--revision",
            cfg.revision,
            "--commit-message",
            "VacancyRadar artifact mirror",
            "--quiet",
        ]
        for attempt in range(1, cfg.attempts + 1):
            is_last = attempt >= cfg.attempts
            try:
                result = runner(
                    cmd,
                    check=False,
                    text=True,
                    capture_output=True,
                    timeout=cfg.timeout,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                # Network hangs are exactly what retry is for; treat the
                # subprocess deadline as transient and retry until exhausted.
                if is_last:
                    raise
                sleeper(cfg.backoff_seconds * attempt)
                continue
            if result.returncode == 0:
                break
            if is_last or not _is_transient_stderr(result.stderr):
                raise subprocess.CalledProcessError(
                    result.returncode,
                    cmd,
                    output=result.stdout,
                    stderr=result.stderr,
                )
            sleeper(cfg.backoff_seconds * attempt)
