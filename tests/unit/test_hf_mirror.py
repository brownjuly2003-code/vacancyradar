from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.publish.hf_mirror import (
    HfMirrorConfig,
    HfUploadItem,
    _is_transient_stderr,
    build_upload_plan,
    missing_required_paths,
    public_base_url,
    upload_items,
)


def test_public_base_url_uses_dataset_resolve_path():
    assert public_base_url("owner/repo") == "https://huggingface.co/datasets/owner/repo/resolve/main"
    assert public_base_url("owner/repo", revision="prod") == (
        "https://huggingface.co/datasets/owner/repo/resolve/prod"
    )


def test_is_transient_stderr_handles_empty_and_known_transient_values():
    assert _is_transient_stderr(None) is False
    assert _is_transient_stderr("") is False
    assert _is_transient_stderr("NameResolutionError: Failed to resolve") is True


def test_build_upload_plan_maps_current_artifact_layout(tmp_path: Path):
    (tmp_path / "derived" / "snapshots").mkdir(parents=True)
    (tmp_path / "derived" / "agg").mkdir(parents=True)
    (tmp_path / "derived" / "slim_events_30d").mkdir(parents=True)
    (tmp_path / "derived" / "slim_active.parquet").write_bytes(b"slim")
    (tmp_path / "derived" / "snapshots" / "facets.json").write_text("{}", encoding="utf-8")
    (tmp_path / "derived" / "agg" / "weekly_market_pulse.parquet").write_bytes(b"agg")
    (tmp_path / "derived" / "slim_events_30d" / "events.parquet").write_bytes(b"events")

    plan = build_upload_plan(tmp_path)

    assert [(item.local_path.relative_to(tmp_path), item.path_in_repo) for item in plan] == [
        (Path("derived/slim_active.parquet"), "slim/active.parquet"),
        (Path("derived/snapshots"), "slim/snapshots"),
        (Path("derived/agg"), "agg"),
        (Path("derived/slim_events_30d"), "slim/events_30d"),
    ]


def test_missing_required_paths_reports_only_required_artifacts(tmp_path: Path):
    missing = missing_required_paths(tmp_path)

    assert missing == [
        Path("derived/slim_active.parquet"),
        Path("derived/snapshots/facets.json"),
        Path("derived/agg/weekly_market_pulse.parquet"),
    ]


def test_upload_items_uses_env_token_not_command_argument(tmp_path: Path):
    item_path = tmp_path / "derived" / "slim_active.parquet"
    item_path.parent.mkdir(parents=True)
    item_path.write_bytes(b"slim")
    plan = build_upload_plan(tmp_path)
    captured: dict[str, object] = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        captured["check"] = kwargs["check"]
        captured["timeout"] = kwargs["timeout"]

        class Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Completed()

    cfg = HfMirrorConfig(repo_id="owner/repo", token="hf_TEST_SECRET", timeout=12.5)
    upload_items(plan, cfg, runner=fake_runner)

    cmd = captured["cmd"]
    assert cmd == [
        "huggingface-cli",
        "upload",
        "owner/repo",
        str(item_path),
        "slim/active.parquet",
        "--repo-type",
        "dataset",
        "--revision",
        "main",
        "--commit-message",
        "VacancyRadar artifact mirror",
        "--quiet",
    ]
    assert "--token" not in cmd
    assert "hf_TEST_SECRET" not in cmd
    assert captured["env"]["HF_TOKEN"] == "hf_TEST_SECRET"
    assert captured["check"] is False
    assert captured["timeout"] == 12.5


def test_upload_items_uploads_each_plan_item(tmp_path: Path):
    first = tmp_path / "derived" / "slim_active.parquet"
    second = tmp_path / "derived" / "snapshots"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"slim")
    second.mkdir()
    calls: list[list[str]] = []

    def fake_runner(cmd, **_kwargs):
        calls.append(cmd)
        return _make_completed(0)

    cfg = HfMirrorConfig(repo_id="owner/repo", token="hf_T")
    upload_items(
        [
            HfUploadItem(first, "slim/active.parquet"),
            HfUploadItem(second, "slim/snapshots"),
        ],
        cfg,
        runner=fake_runner,
    )

    assert [cmd[3] for cmd in calls] == [str(first), str(second)]
    assert [cmd[4] for cmd in calls] == ["slim/active.parquet", "slim/snapshots"]


def _make_completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["huggingface-cli"], returncode=returncode, stdout="", stderr=stderr
    )


def _one_item_plan(tmp_path: Path) -> list:
    item = tmp_path / "derived" / "slim_active.parquet"
    item.parent.mkdir(parents=True)
    item.write_bytes(b"slim")
    return build_upload_plan(tmp_path)


def test_upload_items_retries_transient_dns_then_succeeds(tmp_path: Path):
    plan = _one_item_plan(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []

    def runner(cmd, **kwargs):
        calls.append(len(calls) + 1)
        if len(calls) < 3:
            return _make_completed(
                1,
                stderr=(
                    "urllib3.exceptions.NameResolutionError: "
                    "Failed to resolve 'huggingface.co' "
                    "([Errno 11001] getaddrinfo failed)"
                ),
            )
        return _make_completed(0)

    cfg = HfMirrorConfig(
        repo_id="owner/repo",
        token="hf_T",
        attempts=3,
        backoff_seconds=0.01,
    )
    upload_items(plan, cfg, runner=runner, sleeper=sleeps.append)

    assert calls == [1, 2, 3]
    assert sleeps == [0.01, 0.02]


def test_upload_items_does_not_retry_non_transient_error(tmp_path: Path):
    plan = _one_item_plan(tmp_path)
    calls: list[int] = []

    def runner(cmd, **kwargs):
        calls.append(1)
        return _make_completed(1, stderr="401 Unauthorized: invalid token")

    cfg = HfMirrorConfig(
        repo_id="owner/repo",
        token="hf_T",
        attempts=4,
        backoff_seconds=0.0,
    )
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        upload_items(plan, cfg, runner=runner, sleeper=lambda _s: None)

    assert excinfo.value.returncode == 1
    assert "401 Unauthorized" in (excinfo.value.stderr or "")
    assert calls == [1]


def test_upload_items_rejects_non_positive_attempts(tmp_path: Path):
    plan = _one_item_plan(tmp_path)

    def runner(cmd, **kwargs):
        raise AssertionError("runner must not be called for invalid attempts")

    cfg = HfMirrorConfig(repo_id="owner/repo", token="hf_T", attempts=0)

    with pytest.raises(ValueError, match="attempts must be >= 1"):
        upload_items(plan, cfg, runner=runner)


def test_upload_items_empty_plan_does_not_call_runner():
    def runner(cmd, **kwargs):
        raise AssertionError("runner must not be called for empty plan")

    cfg = HfMirrorConfig(repo_id="owner/repo", token="hf_T")

    upload_items([], cfg, runner=runner)


def test_upload_items_raises_after_exhausting_transient_attempts(tmp_path: Path):
    plan = _one_item_plan(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []

    def runner(cmd, **kwargs):
        calls.append(1)
        return _make_completed(1, stderr="ConnectTimeout: timed out")

    cfg = HfMirrorConfig(
        repo_id="owner/repo",
        token="hf_T",
        attempts=3,
        backoff_seconds=0.1,
    )
    with pytest.raises(subprocess.CalledProcessError):
        upload_items(plan, cfg, runner=runner, sleeper=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]


def test_upload_items_retries_on_subprocess_timeout_then_succeeds(tmp_path: Path):
    plan = _one_item_plan(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []

    def runner(cmd, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1.0))
        return _make_completed(0)

    cfg = HfMirrorConfig(
        repo_id="owner/repo",
        token="hf_T",
        attempts=3,
        backoff_seconds=0.05,
    )
    upload_items(plan, cfg, runner=runner, sleeper=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [0.05, 0.10]


def test_upload_items_raises_after_exhausting_timeouts(tmp_path: Path):
    plan = _one_item_plan(tmp_path)
    calls: list[int] = []
    sleeps: list[float] = []

    def runner(cmd, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 1.0))

    cfg = HfMirrorConfig(
        repo_id="owner/repo",
        token="hf_T",
        attempts=3,
        backoff_seconds=0.1,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        upload_items(plan, cfg, runner=runner, sleeper=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [0.1, 0.2]
