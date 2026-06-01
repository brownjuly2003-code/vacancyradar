"""Domain split of the formerly-monolithic `src/cli.py` (Kimi audit P1-1).

Implementation modules per CLI subcommand domain (`enrich`, `ingest`, etc.).
`src/cli.py` re-exports the symbols this package provides so that:

  * `from src.cli import _publish_slim` keeps working,
  * `monkeypatch.setattr(cli, "_upload_blob", ...)` in tests keeps working
    (the symbol lives in cli.py's globals via re-export; module-level
    rebinding affects all call sites that look it up by name through `cli`),
  * `python -m src.cli ...` still routes through cli.py's `main()`.

When extending: add a new submodule here, re-export from `src/cli.py`,
ensure any cross-module helper sharing goes through `shared.py`.
"""
