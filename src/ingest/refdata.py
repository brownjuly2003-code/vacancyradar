from __future__ import annotations

from pathlib import Path
from typing import Any

import requests
import yaml

ROLES_URL = "https://api.hh.ru/professional_roles"
AREAS_URL = "https://api.hh.ru/areas"


def fetch_professional_roles(session: requests.Session | None = None) -> dict[str, Any]:
    sess = session or requests.Session()
    response = sess.get(ROLES_URL, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise ValueError("hh.ru professional_roles response has no categories list")
    return data


def save_roles_yaml(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_roles_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a mapping")
    return data


def fetch_areas(session: requests.Session | None = None) -> list[dict[str, Any]]:
    sess = session or requests.Session()
    response = sess.get(AREAS_URL, timeout=30)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("hh.ru areas response is not a list")
    return data


def save_areas_yaml(data: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_areas_yaml(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} does not contain a list")
    return data


def russia_subjects(areas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return list of Russia subjects (республики/края/области) used as the
    coarsest area split in the crawler.

    hh.ru `api.hh.ru/areas` does NOT expose federal districts. Россия (id=113)
    has ~88 direct children — actual субъекты (id=1620 Татарстан, id=1530
    Ростовская обл., и т.п.). City ids 1=Москва / 2=СПб are SEPARATE entries
    in the same list, not children of any district.

    Crawler splits depth=0 → subjects (~88) → role (~270) → period → schedule.
    """
    russia = _find_area(areas, "113")
    if russia is None:
        raise ValueError("area id 113 (Russia) not found")
    subjects = list(russia.get("areas") or [])
    if not subjects:
        raise ValueError("area id 113 (Russia) has no children")
    return subjects


def _find_area(nodes: list[dict[str, Any]], area_id: str) -> dict[str, Any] | None:
    for node in nodes:
        if str(node.get("id")) == area_id:
            return node
        found = _find_area(list(node.get("areas") or []), area_id)
        if found is not None:
            return found
    return None
