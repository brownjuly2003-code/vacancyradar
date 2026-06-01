from __future__ import annotations

import pytest

from src.ingest.refdata import (
    fetch_areas,
    fetch_professional_roles,
    load_areas_yaml,
    load_roles_yaml,
    russia_subjects,
    save_areas_yaml,
    save_roles_yaml,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        return FakeResponse(self.payload)


def test_fetch_roles_parses_categories():
    payload = {
        "categories": [
            {"id": "1", "name": "IT", "roles": [{"id": "156", "name": "Developer"}]},
            {"id": "2", "name": "Sales", "roles": []},
        ]
    }
    session = FakeSession(payload)

    result = fetch_professional_roles(session=session)

    assert result["categories"][0]["roles"][0]["id"] == "156"
    assert session.calls == [("https://api.hh.ru/professional_roles", 30)]


def test_russia_subjects_returns_all_children():
    subjects = [
        {"id": "1620", "name": "Татарстан", "areas": []},
        {"id": "1530", "name": "Ростовская обл.", "areas": []},
        {"id": "2114", "name": "Крым", "areas": []},
    ]
    areas = [
        {"id": "5", "name": "Беларусь", "areas": []},
        {"id": "113", "name": "Россия", "areas": subjects},
    ]

    result = russia_subjects(areas)

    assert [item["id"] for item in result] == ["1620", "1530", "2114"]


def test_russia_subjects_raises_when_no_children():
    with pytest.raises(ValueError, match="no children"):
        russia_subjects([{"id": "113", "name": "Россия", "areas": []}])


def test_russia_subjects_raises_when_113_absent():
    """area id 113 missing — surface a clear error rather than indexing into None."""
    with pytest.raises(ValueError, match="not found"):
        russia_subjects([{"id": "5", "name": "Беларусь", "areas": []}])


def test_russia_subjects_finds_113_when_nested():
    """_find_area recurses; some refdata snapshots embed 113 inside a wrapper.
    This locks the recursive helper's contract.
    """
    payload = [
        {
            "id": "999",
            "name": "World",
            "areas": [
                {
                    "id": "113",
                    "name": "Россия",
                    "areas": [{"id": "1620", "name": "Татарстан", "areas": []}],
                }
            ],
        },
    ]
    result = russia_subjects(payload)
    assert [item["id"] for item in result] == ["1620"]


def test_fetch_professional_roles_validates_shape():
    with pytest.raises(ValueError, match="no categories"):
        fetch_professional_roles(session=FakeSession({"unexpected": "shape"}))


def test_fetch_areas_returns_list():
    payload = [{"id": "113", "name": "Россия", "areas": []}]
    session = FakeSession(payload)

    result = fetch_areas(session=session)

    assert result == payload
    assert session.calls == [("https://api.hh.ru/areas", 30)]


def test_fetch_areas_validates_list_shape():
    with pytest.raises(ValueError, match="not a list"):
        fetch_areas(session=FakeSession({"areas": []}))


def test_roles_yaml_roundtrip(tmp_path):
    data = {
        "categories": [
            {"id": "1", "name": "IT", "roles": [{"id": "156", "name": "Разработчик"}]}
        ]
    }
    path = tmp_path / "nested" / "roles.yaml"

    save_roles_yaml(data, path)
    loaded = load_roles_yaml(path)

    assert loaded == data
    assert "Разработчик" in path.read_text(encoding="utf-8")


def test_load_roles_yaml_rejects_non_mapping(tmp_path):
    path = tmp_path / "roles.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not contain a mapping"):
        load_roles_yaml(path)


def test_areas_yaml_roundtrip(tmp_path):
    payload = [
        {"id": "113", "name": "Россия", "areas": []},
        {"id": "5", "name": "Беларусь", "areas": []},
    ]
    path = tmp_path / "areas" / "areas.yaml"

    save_areas_yaml(payload, path)
    loaded = load_areas_yaml(path)

    assert loaded == payload


def test_load_areas_yaml_rejects_mapping(tmp_path):
    path = tmp_path / "areas.yaml"
    path.write_text("key: value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not contain a list"):
        load_areas_yaml(path)
