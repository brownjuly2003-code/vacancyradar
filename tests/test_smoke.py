from src.cli import main


def test_cli_help_exits_clean():
    assert main([]) == 0


def test_cli_ingest_dry_runs():
    assert main(["ingest", "hh", "--dry"]) == 0


def test_cli_publish_slim_dry_runs(monkeypatch):
    """Smoke: publish slim --dry should validate env and exit 0 without
    touching Vercel Blob or building the 349k-row slim_active. The real
    publish path is exercised by the daily refresh script, not unit tests.
    """
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "test-token")
    monkeypatch.setenv("BLOB_PUBLIC_BASE_URL", "https://example.invalid")
    assert main(["publish", "slim", "--dry"]) == 0
