from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
from telethon.errors import FloodWaitError

from src.cli import _ingest_telegram, _scoped_telegram_channels
from src.ingest.tg_client import TGMessage, TGSessionError


class FakeClient:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


def _args(
    *,
    channel_file: str | None = None,
    channels: int | None = None,
    channel_start: int = 0,
    scope: str | None = None,
) -> Namespace:
    return Namespace(
        dry=False,
        scope=scope,
        channels=channels,
        limit=10,
        channel_file=channel_file,
        channel_start=channel_start,
    )


def test_scoped_telegram_channels_uses_explicit_file(tmp_path: Path):
    channel_file = tmp_path / "channels.yaml"
    channel_file.write_text(
        "channels:\n"
        "  - username: alpha\n"
        "    role: dev\n"
        "  - username: beta\n"
        "    role: data\n",
        encoding="utf-8",
    )

    channels = _scoped_telegram_channels("missing-scope", explicit_file=str(channel_file))

    assert channels == [
        {"username": "alpha", "role": "dev"},
        {"username": "beta", "role": "data"},
    ]


def test_ingest_telegram_reads_channel_file(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\n\nbeta\n", encoding="utf-8")
    client = FakeClient()
    seen: list[str] = []

    def fake_fetch(_client, username: str, *, limit: int):
        seen.append(username)
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=10,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 0

    assert seen == ["alpha", "beta"]
    assert client.disconnected is True
    df = pl.read_parquet("master/vacancies_raw.parquet/year=2026/month=04/source=telegram/*.parquet")
    assert sorted(df["vacancy_id"].to_list()) == ["tg:alpha:1", "tg:beta:1"]


def test_ingest_telegram_duplicate_run_reports_no_diff(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\n", encoding="utf-8")
    client = FakeClient()

    def fake_fetch(_client, username: str, *, limit: int):
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=10,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 0
    capsys.readouterr()

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 0
    assert "[tg-events] no diff" in capsys.readouterr().out


def test_ingest_telegram_stops_on_flood_wait_and_writes_collected(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    client = FakeClient()
    seen: list[str] = []

    def fake_fetch(_client, username: str, *, limit: int):
        seen.append(username)
        if username == "beta":
            raise FloodWaitError(request=None, capture=3600)
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=None,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 75

    assert seen == ["alpha", "beta"]
    assert client.disconnected is True
    captured = capsys.readouterr()
    assert "[done] tg ingest: 1 messages from 1 channels; attempted=2/3" in captured.out
    # FloodWait must NOT advance past the failed channel (off-by-one fix).
    assert "[tg] resume with --channel-start 1" in captured.out
    df = pl.read_parquet("master/vacancies_raw.parquet/year=2026/month=04/source=telegram/*.parquet")
    assert df["vacancy_id"].to_list() == ["tg:alpha:1"]

    # KM re-audit 2026-05-17 P1: resume state must persist so daily_refresh
    # can read it before next run instead of restarting from channel 0.
    resume_path = tmp_path / "master" / "run_state" / "tg_resume.json"
    assert resume_path.exists()
    payload = json.loads(resume_path.read_text(encoding="utf-8"))
    assert payload["resume_index"] == 1
    assert payload["wait_seconds"] == 3600
    assert payload["retry_after_epoch"] > 0


def test_ingest_telegram_clean_run_clears_existing_resume_state(tmp_path: Path, monkeypatch):
    """KM re-audit 2026-05-17 P1: successful run должен снять resume lock,
    иначе следующий daily run будет вечно стартовать с offset последнего FloodWait."""
    monkeypatch.chdir(tmp_path)
    resume_path = tmp_path / "master" / "run_state" / "tg_resume.json"
    resume_path.parent.mkdir(parents=True, exist_ok=True)
    resume_path.write_text(
        json.dumps({"resume_index": 99, "wait_seconds": 0, "retry_after_epoch": 0}),
        encoding="utf-8",
    )
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\nbeta\n", encoding="utf-8")
    client = FakeClient()

    def fake_fetch(_client, username: str, *, limit: int):
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=10,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 0
    assert not resume_path.exists()


def test_ingest_telegram_clean_run_ignores_resume_unlink_error(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    resume_path = tmp_path / "master" / "run_state" / "tg_resume.json"
    resume_path.parent.mkdir(parents=True, exist_ok=True)
    resume_path.write_text(
        json.dumps({"resume_index": 99, "wait_seconds": 0, "retry_after_epoch": 0}),
        encoding="utf-8",
    )
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\n", encoding="utf-8")
    client = FakeClient()

    def fake_fetch(_client, username: str, *, limit: int):
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=10,
            )
        ]

    real_unlink = Path.unlink

    def fake_unlink(path: Path, *args, **kwargs):
        if path.name == "tg_resume.json":
            raise OSError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(Path, "unlink", fake_unlink)

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 0
    assert resume_path.exists()


def test_ingest_telegram_partial_failure_returns_76_and_logs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    client = FakeClient()

    def fake_fetch(_client, username: str, *, limit: int):
        if username == "beta":
            raise RuntimeError("channel banned")
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=None,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    # Non-FloodWait failures must surface via exit 76 so the wrapper logs them
    # instead of silently advancing state past the broken channel.
    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 76

    failed_log = tmp_path / "master" / "run_state" / "tg_failed.jsonl"
    entries = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 1
    assert entries[0]["username"] == "beta"
    assert entries[0]["index"] == 1
    assert "channel banned" in entries[0]["error"]


def test_ingest_telegram_aborts_after_consecutive_network_failures(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    client = FakeClient()
    seen: list[str] = []

    def fake_fetch(_client, username: str, *, limit: int):
        seen.append(username)
        raise ConnectionError("Connection to Telegram failed 5 time(s)")

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 76

    assert seen == ["alpha", "beta", "gamma"]
    assert client.disconnected is True
    err = capsys.readouterr().err
    assert "aborting after 3 consecutive network failures" in err
    failed_log = tmp_path / "master" / "run_state" / "tg_failed.jsonl"
    entries = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").splitlines()]
    assert [entry["username"] for entry in entries] == ["alpha", "beta", "gamma"]


def test_ingest_telegram_starts_at_channel_offset(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    client = FakeClient()
    seen: list[str] = []

    def fake_fetch(_client, username: str, *, limit: int):
        seen.append(username)
        return [
            TGMessage(
                channel=username,
                message_id=1,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy",
                views=10,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(channel_file=str(channel_file), channels=2, channel_start=1)) == 0

    assert seen == ["beta", "gamma"]
    df = pl.read_parquet("master/vacancies_raw.parquet/year=2026/month=04/source=telegram/*.parquet")
    assert sorted(df["vacancy_id"].to_list()) == ["tg:beta:1", "tg:gamma:1"]


def test_ingest_telegram_scope_labels_raw_rows_with_market_scope(tmp_path: Path, monkeypatch):
    """Regression: --scope it must propagate market_scope into raw lake rows
    so publish slim --scope it can pick them up. Previously _ingest_telegram
    dropped scope_name when constructing RawRecord.from_telegram_message,
    so TG rows landed with market_scope=NULL and the IT slim stayed hh-only.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "config.yaml").write_text(
        "market:\n"
        "  live_scope: it\n"
        "  scopes:\n"
        "    it:\n"
        "      telegram:\n"
        "        roles: [dev]\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "tg_channels.yaml").write_text(
        "channels:\n  - username: dev_jobs\n    role: dev\n",
        encoding="utf-8",
    )
    client = FakeClient()

    def fake_fetch(_client, username: str, *, limit: int):
        return [
            TGMessage(
                channel=username,
                message_id=42,
                date=datetime(2026, 4, 29, tzinfo=timezone.utc),
                text=f"{username} vacancy text",
                views=10,
            )
        ]

    monkeypatch.setattr("src.ingest.tg_client.open_session", lambda: client)
    monkeypatch.setattr("src.ingest.tg_client.fetch_channel_messages", fake_fetch)
    monkeypatch.setattr(
        "src.ingest.raw_lake.utcnow",
        lambda: datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
    )

    assert _ingest_telegram(_args(scope="it")) == 0

    df = pl.read_parquet(
        "master/vacancies_raw.parquet/year=2026/month=04/source=telegram/*.parquet"
    )
    assert df["market_scope"].to_list() == ["it"]


def test_ingest_telegram_session_error_returns_exit_3(tmp_path: Path, monkeypatch, capsys):
    """KM audit 2026-05-17 P1: revoked/missing session must surface a distinct
    exit code (3) so daily_refresh.ps1 separates auth issues from FloodWait (75)
    and per-channel failures (76)."""
    monkeypatch.chdir(tmp_path)
    channel_file = tmp_path / "channels.txt"
    channel_file.write_text("alpha\n", encoding="utf-8")

    def fake_open():
        raise TGSessionError("session vradar_session not authorized; run `vradar auth tg`")

    monkeypatch.setattr("src.ingest.tg_client.open_session", fake_open)

    assert _ingest_telegram(_args(channel_file=str(channel_file))) == 3
    err = capsys.readouterr().err
    assert "[tg][auth-error]" in err
    assert "not authorized" in err


def test_ingest_telegram_dry_scope_prints_it_channels(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "config.yaml").write_text(
        "market:\n"
        "  live_scope: it\n"
        "  scopes:\n"
        "    it:\n"
        "      telegram:\n"
        "        roles: [dev, data]\n",
        encoding="utf-8",
    )
    (tmp_path / "data" / "tg_channels.yaml").write_text(
        "channels:\n"
        "  - username: dev_jobs\n"
        "    role: dev\n"
        "  - username: hr_jobs\n"
        "    role: hr\n"
        "  - username: data_jobs\n"
        "    role: data\n",
        encoding="utf-8",
    )

    args = _args(scope="it")
    args.dry = True

    assert _ingest_telegram(args) == 0

    out = capsys.readouterr().out
    assert "scope=it" in out
    assert "channels=2" in out
    assert "@dev_jobs" in out
    assert "@data_jobs" in out
    assert "hr_jobs" not in out


def test_ingest_telegram_dry_scope_resolution_failure_returns_2(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)

    def boom(_scope_name, _explicit_file=None):
        raise ValueError("unknown scope 'missing'")

    monkeypatch.setattr("src.cli._scoped_telegram_channels", boom)

    args = _args(scope="missing")
    args.dry = True

    assert _ingest_telegram(args) == 2
    assert "unknown scope 'missing'" in capsys.readouterr().err
