"""CLI `cli.main()` argparse + cmd dispatch coverage.

Each test replaces one dispatcher (`_ingest` / `_auth` / `_publish` / ...) with
a stub that records the parsed args and returns a sentinel exit code. This
exercises the entire `main()` body (argparse setup + subparser registration +
dispatch branches) without touching real lakes, Blob, Neon, or Quarto.
"""
from __future__ import annotations

import pytest

from src import cli


def test_main_no_cmd_prints_help_and_returns_zero(capsys):
    rc = cli.main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert "vradar" in captured.out
    assert "ingest" in captured.out


def test_main_dispatches_ingest(monkeypatch):
    captured: dict = {}

    def fake_ingest(args):
        captured["cmd"] = args.cmd
        captured["source"] = args.source
        captured["dry"] = args.dry
        captured["pages"] = args.pages
        return 11

    monkeypatch.setattr(cli, "_ingest", fake_ingest)

    rc = cli.main(["ingest", "hh", "--dry", "--pages", "3"])
    assert rc == 11
    assert captured == {"cmd": "ingest", "source": "hh", "dry": True, "pages": 3}


def test_main_dispatches_auth(monkeypatch):
    captured: dict = {}

    def fake_auth(args):
        captured["cmd"] = args.cmd
        captured["provider"] = args.provider
        captured["client_id"] = args.client_id
        return 12

    monkeypatch.setattr(cli, "_auth", fake_auth)

    rc = cli.main(["auth", "hh", "--client-id", "ID", "--client-secret", "SECRET"])
    assert rc == 12
    assert captured == {"cmd": "auth", "provider": "hh", "client_id": "ID"}


def test_main_dispatches_publish(monkeypatch):
    captured: dict = {}

    def fake_publish(args):
        captured["cmd"] = args.cmd
        captured["target"] = args.target
        captured["dry"] = args.dry
        captured["strict"] = args.strict
        captured["scope"] = args.scope
        captured["active_days"] = args.active_days
        return 13

    monkeypatch.setattr(cli, "_publish", fake_publish)

    rc = cli.main(["publish", "slim", "--dry", "--strict", "--scope", "it", "--active-days", "30"])
    assert rc == 13
    assert captured == {
        "cmd": "publish",
        "target": "slim",
        "dry": True,
        "strict": True,
        "scope": "it",
        "active_days": 30,
    }


def test_main_dispatches_enrich(monkeypatch):
    captured: dict = {}

    def fake_enrich(args):
        captured["cmd"] = args.cmd
        captured["kind"] = args.kind
        captured["rate"] = args.rate
        captured["limit"] = args.limit
        captured["batch_size"] = args.batch_size
        captured["force"] = args.force
        return 14

    monkeypatch.setattr(cli, "_enrich", fake_enrich)

    rc = cli.main(
        [
            "enrich",
            "embeddings",
            "--rate",
            "2.5",
            "--limit",
            "100",
            "--batch-size",
            "16",
            "--force",
        ]
    )
    assert rc == 14
    assert captured == {
        "cmd": "enrich",
        "kind": "embeddings",
        "rate": 2.5,
        "limit": 100,
        "batch_size": 16,
        "force": True,
    }


def test_main_dispatches_report(monkeypatch):
    captured: dict = {}

    def fake_report(args):
        captured["cmd"] = args.cmd
        captured["kind"] = args.kind
        captured["month"] = args.month
        captured["scope"] = args.scope
        return 15

    monkeypatch.setattr(cli, "_report", fake_report)

    rc = cli.main(["report", "monthly", "--month", "2026-04", "--scope", "full"])
    assert rc == 15
    assert captured == {"cmd": "report", "kind": "monthly", "month": "2026-04", "scope": "full"}


def test_main_dispatches_refdata(monkeypatch):
    captured: dict = {}

    def fake_refdata(args):
        captured["cmd"] = args.cmd
        captured["kind"] = args.kind
        captured["refresh"] = args.refresh
        return 16

    monkeypatch.setattr(cli, "_refdata", fake_refdata)

    rc = cli.main(["refdata", "roles", "--refresh"])
    assert rc == 16
    assert captured == {"cmd": "refdata", "kind": "roles", "refresh": True}


def test_main_dispatches_prune(monkeypatch):
    captured: dict = {}

    def fake_prune(args):
        captured["cmd"] = args.cmd
        captured["target"] = args.target
        captured["older_than_days"] = args.older_than_days
        captured["dry"] = args.dry
        captured["vacuum"] = args.vacuum
        return 17

    monkeypatch.setattr(cli, "_prune", fake_prune)

    rc = cli.main(["prune", "events", "--older-than-days", "60", "--dry", "--vacuum"])
    assert rc == 17
    assert captured == {
        "cmd": "prune",
        "target": "events",
        "older_than_days": 60,
        "dry": True,
        "vacuum": True,
    }


def test_main_ingest_hh_crawl_argparse(monkeypatch):
    """hh-crawl shares the `ingest` subparser; verify its specific flags parse."""
    captured: dict = {}

    def fake_ingest(args):
        captured["source"] = args.source
        captured["root"] = args.root
        captured["max_depth"] = args.max_depth
        captured["rate"] = args.rate
        captured["max_vacancies"] = args.max_vacancies
        captured["reset"] = args.reset
        return 0

    monkeypatch.setattr(cli, "_ingest", fake_ingest)

    rc = cli.main(
        [
            "ingest",
            "hh-crawl",
            "--root",
            "area=1,professional_role=156",
            "--max-depth",
            "5",
            "--rate",
            "0.5",
            "--max-vacancies",
            "1000",
            "--reset",
        ]
    )
    assert rc == 0
    assert captured == {
        "source": "hh-crawl",
        "root": "area=1,professional_role=156",
        "max_depth": 5,
        "rate": 0.5,
        "max_vacancies": 1000,
        "reset": True,
    }


def test_main_ingest_telegram_channel_options(monkeypatch):
    """Verify TG-specific flags including the dual --channel-start/--channel-offset alias."""
    captured: dict = {}

    def fake_ingest(args):
        captured["source"] = args.source
        captured["channels"] = args.channels
        captured["channel_start"] = args.channel_start
        captured["channel_file"] = args.channel_file
        captured["limit"] = args.limit
        return 0

    monkeypatch.setattr(cli, "_ingest", fake_ingest)

    rc = cli.main(
        [
            "ingest",
            "telegram",
            "--channels",
            "5",
            "--channel-offset",
            "3",  # alias for --channel-start
            "--channel-file",
            "channels.txt",
            "--limit",
            "50",
        ]
    )
    assert rc == 0
    assert captured["source"] == "telegram"
    assert captured["channels"] == 5
    assert captured["channel_start"] == 3
    assert captured["channel_file"] == "channels.txt"
    assert captured["limit"] == 50


def test_main_invalid_subcommand_exits_via_argparse(capsys):
    """Unknown subparser → argparse SystemExit(2). Confirms add_subparsers
    rejects values not declared via add_parser(), so the trailing print-stub
    branch in main() is effectively unreachable."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["definitely-not-a-real-cmd"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err or "argument" in err.lower()
