from src.cli import main


def test_cli_help_exits_clean():
    assert main([]) == 0


def test_cli_ingest_dry_runs():
    assert main(["ingest", "hh", "--dry"]) == 0


def test_cli_publish_slim_dry_runs():
    """Smoke: publish slim --dry should plan and exit 0 without building the
    slim_active artifact. The real publish path is exercised by the collection
    runner, not unit tests.
    """
    assert main(["publish", "slim", "--dry"]) == 0
