"""CLI `vradar report --scope` routing.

Не вызывает реальный quarto — заменяет subprocess.run и build_slim_active
мокером, чтобы убедиться, что:
- без --scope или --scope it: env VRADAR_SLIM_PATH не выставлен (qmd читает живой derived/slim_active.parquet)
- --scope full (или любая non-"it" строка): CLI строит full-market slim
  через build_slim_active(lake) → derived/slim_full.parquet и передаёт
  путь через env VRADAR_SLIM_PATH в Quarto
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import polars as pl

from src.cli import _report
from src.transform.slim_export import SLIM_ACTIVE_SCHEMA


def _make_quarto_stub(tmp_path: Path) -> Path:
    """Stub quarto.exe at a fake path so shutil.which fallback is satisfied."""
    quarto = tmp_path / "fake_quarto"
    quarto.write_text("# fake", encoding="utf-8")
    return quarto


def _patch_quarto_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("shutil.which", lambda _: str(_make_quarto_stub(tmp_path)))


def test_report_default_scope_does_not_set_slim_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "monthly_digest.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    seen_env: dict = {}

    def fake_run(cmd, *, check, env):
        seen_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="monthly", month=None, employer=None, scope=None)) == 0
    assert "VRADAR_SLIM_PATH" not in seen_env


def test_report_scope_full_builds_slim_and_sets_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "monthly_digest.qmd").write_text("placeholder", encoding="utf-8")
    (tmp_path / "master" / "vacancies_raw.parquet").mkdir(parents=True)
    _patch_quarto_path(monkeypatch, tmp_path)

    captured = {}

    def fake_build(lake_root, *args, **kwargs):
        captured["lake"] = lake_root
        captured["kwargs"] = kwargs
        return pl.DataFrame(schema=SLIM_ACTIVE_SCHEMA)

    seen_env: dict = {}

    def fake_run(cmd, *, check, env):
        seen_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build)
    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="monthly", month=None, employer=None, scope="full")) == 0

    assert captured["lake"] == Path("master/vacancies_raw.parquet")
    # Full-market build does not pass market_scope filter
    assert captured["kwargs"].get("market_scope") is None
    full_slim = tmp_path / "derived" / "slim_full.parquet"
    assert full_slim.exists()
    assert seen_env["VRADAR_SLIM_PATH"] == str(full_slim.resolve())


def test_report_scope_it_sets_explicit_scope_env(monkeypatch, tmp_path):
    """--scope it should mark VRADAR_SCOPE=it (for qmd label) but NOT override
    SLIM path — the live IT slim already lives at derived/slim_active.parquet."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "monthly_digest.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    seen_env: dict = {}

    def fake_run(cmd, *, check, env):
        seen_env.update(env)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="monthly", month=None, employer=None, scope="it")) == 0
    assert seen_env.get("VRADAR_SCOPE") == "it"
    assert "VRADAR_SLIM_PATH" not in seen_env


def test_report_monthly_passes_month_param(monkeypatch, tmp_path):
    """Verifies `-P month:VALUE` makes it into the quarto cmdline when --month set."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "monthly_digest.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    seen_cmd: list[str] = []

    def fake_run(cmd, *, check, env):
        seen_cmd.extend(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="monthly", month="2026-04", employer=None, scope=None)) == 0
    assert "-P" in seen_cmd
    assert "month:2026-04" in seen_cmd


def test_report_employer_passes_employer_id_param(monkeypatch, tmp_path):
    """Verifies `-P employer_id:VALUE` for kind=employer."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "employer_profile.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    seen_cmd: list[str] = []

    def fake_run(cmd, *, check, env):
        seen_cmd.extend(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="employer", month=None, employer="hh:1373", scope=None)) == 0
    assert "employer_id:hh:1373" in seen_cmd


def test_report_returns_quarto_exit_code_on_non_zero(monkeypatch, tmp_path, capsys):
    """Quarto subprocess non-zero exit propagates as the CLI return code."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "skill_landscape.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    def fake_run(cmd, *, check, env):
        class _R:
            returncode = 7

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    rc = _report(Namespace(kind="skill", month=None, employer=None, scope=None))
    assert rc == 7
    assert "quarto render exit 7" in capsys.readouterr().err


def test_report_moves_rendered_html_into_out_dir(monkeypatch, tmp_path):
    """After a successful render, src/reports/<kind>.html is moved into derived/reports/."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    template = tmp_path / "src" / "reports" / "skill_landscape.qmd"
    template.write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    rendered = tmp_path / "src" / "reports" / "skill_landscape.html"

    def fake_run(cmd, *, check, env):
        # Simulate quarto writing the HTML output next to the .qmd
        rendered.write_text("<html>fake report</html>", encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="skill", month=None, employer=None, scope=None)) == 0
    target = tmp_path / "derived" / "reports" / "skill_landscape.html"
    assert target.exists()
    assert not rendered.exists()  # was renamed/replaced into derived/reports/


def test_report_replace_overwrites_existing_target(monkeypatch, tmp_path):
    """Win Path.rename FileExistsError guard — .replace overwrites old artifact."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "reports").mkdir(parents=True)
    (tmp_path / "src" / "reports" / "skill_landscape.qmd").write_text("placeholder", encoding="utf-8")
    _patch_quarto_path(monkeypatch, tmp_path)

    # Pre-existing report from a previous render
    out_dir = tmp_path / "derived" / "reports"
    out_dir.mkdir(parents=True)
    (out_dir / "skill_landscape.html").write_text("old", encoding="utf-8")

    rendered = tmp_path / "src" / "reports" / "skill_landscape.html"

    def fake_run(cmd, *, check, env):
        rendered.write_text("new", encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _report(Namespace(kind="skill", month=None, employer=None, scope=None)) == 0
    assert (out_dir / "skill_landscape.html").read_text(encoding="utf-8") == "new"
