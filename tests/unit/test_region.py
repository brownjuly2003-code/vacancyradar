from __future__ import annotations

from pathlib import Path

import pytest

from src.enrich.region import _load_city_index, region_for_city


@pytest.fixture(autouse=True)
def _clear_cache():
    _load_city_index.cache_clear()
    yield
    _load_city_index.cache_clear()


def test_none_or_empty_city():
    assert region_for_city(None) is None
    assert region_for_city("") is None
    assert region_for_city("   ") is None


def test_known_cities_map_to_districts():
    assert region_for_city("Москва") == "ЦФО"
    assert region_for_city("Санкт-Петербург") == "СЗФО"
    assert region_for_city("Новосибирск") == "СФО"
    assert region_for_city("Казань") == "ПФО"
    assert region_for_city("Екатеринбург") == "УФО"
    assert region_for_city("Краснодар") == "ЮФО"
    assert region_for_city("Махачкала") == "СКФО"
    assert region_for_city("Владивосток") == "ДФО"


def test_case_and_whitespace_insensitive():
    assert region_for_city("  МОСКВА  ") == "ЦФО"
    assert region_for_city("санкт-петербург") == "СЗФО"


def test_yo_e_variants_both_recognised():
    assert region_for_city("Орёл") == "ЦФО"
    assert region_for_city("Орел") == "ЦФО"


def test_unknown_city_returns_none():
    assert region_for_city("Зажопинск") is None
    assert region_for_city("Алматы") is None  # казахстанский, не в РФ-таксономии


def test_hh_format_with_oblast_in_parens():
    """hh.ru формат: 'Город (Область)' → распознаём по oblast."""
    assert region_for_city("Киров (Кировская область)") == "ПФО"
    assert region_for_city("Бронницы (Московская область)") == "ЦФО"
    assert region_for_city("Реутов (Московская область)") == "ЦФО"
    assert region_for_city("Иваново (Ивановская область)") == "ЦФО"
    assert region_for_city("Волжский (Самарская область)") == "ПФО"


def test_hh_format_admin_prefix_stripped():
    """'городской округ Химки' → 'Химки' → ЦФО."""
    assert region_for_city("городской округ Химки") == "ЦФО"
    assert region_for_city("город Москва") == "ЦФО"


def test_extra_spaces_in_parens_format():
    """hh.ru иногда даёт двойной пробел перед скобкой."""
    assert region_for_city("Жуковский  (Московская область)") == "ЦФО"


def test_unknown_oblast_in_parens_returns_none():
    assert region_for_city("Фейкград (Неизвестная область)") is None


def test_republic_form_recognised():
    assert region_for_city("Уфа (Башкортостан)") == "ПФО"
    assert region_for_city("Казань (Республика Татарстан)") == "ПФО"


def test_custom_yaml_path(tmp_path: Path):
    custom = tmp_path / "regions.yaml"
    custom.write_text("FakeFO:\n  - Test City\n", encoding="utf-8")
    assert region_for_city("test city", regions_path=custom) == "FakeFO"
    assert region_for_city("Москва", regions_path=custom) is None
