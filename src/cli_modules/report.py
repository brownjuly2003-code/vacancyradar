"""`vradar report {monthly,employer,skill}` impl. Extracted (Kimi P1-1)."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _report(args: argparse.Namespace) -> int:
    """Wrapper над `quarto render`. Quarto установлен через winget."""
    quarto = shutil.which("quarto") or "C:/Program Files/Quarto/bin/quarto.exe"
    if not Path(quarto).exists():
        print("[err] quarto не найден. winget install Posit.Quarto", file=sys.stderr)
        return 2

    template_map = {
        "monthly": "src/reports/monthly_digest.qmd",
        "employer": "src/reports/employer_profile.qmd",
        "skill": "src/reports/skill_landscape.qmd",
    }
    template = template_map.get(args.kind)
    if not template or not Path(template).exists():
        print(f"[err] нет шаблона для kind={args.kind}", file=sys.stderr)
        return 2

    out_dir = Path("derived/reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    # Quarto python_engine resolves /usr/bin/env python, which on this machine
    # hits Python 3.13 (no jupyter installed). Pin to 3.12 (where pip lives) so
    # `quarto render` doesn't crash with "Jupyter is not available".
    env.setdefault("QUARTO_PYTHON", "D:/Python/Python312/python.exe")
    scope_name = getattr(args, "scope", None)
    if scope_name and scope_name != "it":
        # Full-market report: build a one-off slim from raw lake (does not
        # touch derived/slim_active.parquet — that stays the live IT artifact)
        # and never push to Blob/Turso. Point the .qmd at it via env var.
        from src.transform.slim_export import build_slim_active, write_slim_active

        lake = Path("master/vacancies_raw.parquet")
        full_slim_path = Path("derived/slim_full.parquet")
        print(f"[report] building full-market slim from raw lake → {full_slim_path}")
        df = build_slim_active(lake)
        write_slim_active(df, full_slim_path)
        env["VRADAR_SLIM_PATH"] = str(full_slim_path.resolve())
        print(f"[report] scope=full ({len(df)} rows, {full_slim_path.stat().st_size // 1024} KB)")
    elif scope_name == "it":
        # Default behavior already targets the live IT slim — make it explicit
        # so the .qmd can render the scope label.
        env["VRADAR_SCOPE"] = "it"

    cmd = [quarto, "render", template, "--to", "html"]
    # Quarto принимает `-P key:value` (NB: двоеточие, не равно — equal-sign
    # форма работает в новых версиях, но колоночная — каноничная и стабильная
    # между версиями).
    if args.kind == "monthly" and args.month:
        cmd += ["-P", f"month:{args.month}"]
    if args.kind == "employer" and args.employer:
        cmd += ["-P", f"employer_id:{args.employer}"]

    print(f"[report] rendering {template} → derived/reports/ (scope={scope_name or 'default-it'})")
    result = subprocess.run(cmd, check=False, env=env)
    if result.returncode != 0:
        print(f"[err] quarto render exit {result.returncode}", file=sys.stderr)
        return result.returncode

    # Quarto пишет рядом с .qmd — переносим в derived/reports.
    # На Windows Path.rename падает FileExistsError если target есть от
    # предыдущего рендера → replace=overwrite-friendly.
    rendered = Path(template).with_suffix(".html")
    if rendered.exists():
        target = out_dir / rendered.name
        rendered.replace(target)
        print(f"[report] {target} ({target.stat().st_size // 1024} KB)")
    return 0
