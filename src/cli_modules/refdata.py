"""`vradar refdata {roles,areas}` impl. Extracted from src/cli.py (Kimi P1-1)."""
from __future__ import annotations

import argparse
from pathlib import Path


def _refdata(args: argparse.Namespace) -> int:
    from src.ingest.refdata import (
        fetch_areas,
        fetch_professional_roles,
        load_areas_yaml,
        load_roles_yaml,
        russia_subjects,
        save_areas_yaml,
        save_roles_yaml,
    )

    if args.kind == "roles":
        path = Path("data/professional_roles.yaml")
        if args.refresh or not path.exists():
            data = fetch_professional_roles()
            save_roles_yaml(data, path)
        else:
            data = load_roles_yaml(path)
        n_roles = sum(len(c.get("roles", [])) for c in data.get("categories", []))
        print(f"[refdata] roles: {len(data.get('categories', []))} categories, {n_roles} roles → {path}")
        return 0
    if args.kind == "areas":
        path = Path("data/areas.yaml")
        if args.refresh or not path.exists():
            areas = fetch_areas()
            save_areas_yaml(areas, path)
        else:
            areas = load_areas_yaml(path)
        subjects = russia_subjects(areas)
        print(f"[refdata] areas: {len(subjects)} Russia subjects → {path}")
        return 0
    return 1
