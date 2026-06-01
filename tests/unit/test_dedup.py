"""TDD для cross-source dedup (Phase 4 part 1)."""
from __future__ import annotations

import pytest

import src.transform.dedup as dedup_mod
from src.transform.dedup import (
    DuplicatePair,
    VacancyForDedup,
    find_duplicates,
    normalize_text,
    shingles,
)


def _v(vid: str, source: str, title: str, employer: str, city: str, text: str) -> VacancyForDedup:
    return VacancyForDedup(
        vacancy_id=vid,
        source=source,
        title=title,
        employer_name=employer,
        city=city,
        text=text,
    )


def test_normalize_text_strips_punct_and_lowercases():
    assert normalize_text("Senior Python Developer", "Yandex.Eda LLC", "Москва", None) == (
        "senior python developer yandex eda llc москва"
    )


def test_normalize_text_handles_all_none():
    assert normalize_text(None, None, None) == ""


def test_shingles_short_text_returns_single_shingle():
    assert shingles("hello", k=4) == {"hello"}


def test_shingles_rolling_window():
    out = shingles("a b c d e", k=3)
    assert out == {"a b c", "b c d", "c d e"}


def test_known_pair_hh_telegram_same_role_finds_duplicate():
    """Одна вакансия на hh.ru и в Telegram — текст похож, source разный."""
    base_text = (
        "ищем сильного python разработчика опыт с fastapi django postgresql redis "
        "docker kubernetes ci cd микросервисы high load удалённая работа полная занятость"
    )
    vacancies = [
        _v(
            "hh:1",
            "hh",
            "Senior Python Developer (FastAPI Django)",
            "Acme",
            "Москва",
            base_text,
        ),
        _v(
            "tg:1",
            "telegram",
            "Senior Python Developer (FastAPI/Django)",
            "Acme",
            "Москва",
            base_text + " от 250к",
        ),
    ]
    pairs = find_duplicates(vacancies)
    assert len(pairs) == 1
    p = pairs[0]
    assert {p.id_a, p.id_b} == {"hh:1", "tg:1"}
    assert {p.source_a, p.source_b} == {"hh", "telegram"}
    assert p.jaccard >= 0.88


def test_known_non_pair_different_roles():
    """Совсем разные роли — не должны слипнуться."""
    vacancies = [
        _v(
            "hh:1",
            "hh",
            "Senior Python Developer FastAPI",
            "Acme",
            "Москва",
            "backend python fastapi postgres",
        ),
        _v(
            "tg:1",
            "telegram",
            "Курьер пеший Яндекс Еда",
            "Yandex",
            "Москва",
            "доставка заказов график свободный",
        ),
    ]
    pairs = find_duplicates(vacancies)
    assert pairs == []


def test_known_non_pair_same_role_different_employer_different_city():
    """Одинаковый title но разный работодатель и город — non-pair."""
    vacancies = [
        _v(
            "hh:1",
            "hh",
            "Python Developer",
            "Acme",
            "Москва",
            "fastapi postgres redis docker kubernetes",
        ),
        _v(
            "tg:1",
            "telegram",
            "Python Developer",
            "Beta Corp",
            "Новосибирск",
            "django mysql memcached nginx ansible",
        ),
    ]
    pairs = find_duplicates(vacancies)
    assert pairs == []


def test_cross_source_only_default_excludes_same_source():
    """Дефолт cross_source_only=True: hh×hh пары не возвращаются даже если текст идентичен."""
    vacancies = [
        _v("hh:1", "hh", "Senior Python", "Acme", "Москва", "fastapi postgres redis docker"),
        _v("hh:2", "hh", "Senior Python", "Acme", "Москва", "fastapi postgres redis docker"),
    ]
    pairs = find_duplicates(vacancies)
    assert pairs == []


def test_cross_source_only_false_includes_same_source():
    vacancies = [
        _v("hh:1", "hh", "Senior Python", "Acme", "Москва", "fastapi postgres redis docker"),
        _v("hh:2", "hh", "Senior Python", "Acme", "Москва", "fastapi postgres redis docker"),
    ]
    pairs = find_duplicates(vacancies, cross_source_only=False)
    assert len(pairs) == 1
    assert pairs[0].jaccard >= 0.88


def test_threshold_filter():
    """Низкий threshold пускает разные роли в одну пару, высокий — нет."""
    vacancies = [
        _v(
            "hh:1",
            "hh",
            "Senior Python Backend Developer",
            "Acme",
            "Москва",
            "fastapi django postgresql redis backend",
        ),
        _v(
            "tg:1",
            "telegram",
            "Middle Python Backend Developer",
            "Acme",
            "Москва",
            "fastapi django postgresql backend middle",
        ),
    ]
    high = find_duplicates(vacancies, threshold=0.95)
    low = find_duplicates(vacancies, threshold=0.5)
    assert len(low) >= len(high)


def test_empty_input_returns_empty():
    assert find_duplicates([]) == []


def test_vacancy_with_no_text_skipped():
    """Пустые поля → нет shingle → не попадает в индекс, не падает."""
    vacancies = [
        _v("hh:1", "hh", None, None, None, None),
        _v(
            "tg:1",
            "telegram",
            "Senior Python Developer",
            "Acme",
            "Москва",
            "fastapi django postgres",
        ),
    ]
    pairs = find_duplicates(vacancies)
    assert pairs == []


def test_lsh_candidate_missing_from_items_is_ignored(monkeypatch):
    class FakeLSH:
        def __init__(self, *, threshold, num_perm):
            pass

        def insert(self, vacancy_id, minhash):
            pass

        def query(self, minhash):
            return ["ghost"]

    monkeypatch.setattr(dedup_mod, "MinHashLSH", FakeLSH)

    vacancies = [_v("hh:1", "hh", "Senior Python", "Acme", "Москва", "fastapi postgres")]

    assert find_duplicates(vacancies) == []


def test_candidate_below_exact_jaccard_threshold_is_filtered(monkeypatch):
    class FakeLSH:
        def __init__(self, *, threshold, num_perm):
            self.ids = []

        def insert(self, vacancy_id, minhash):
            self.ids.append(vacancy_id)

        def query(self, minhash):
            return list(self.ids)

    monkeypatch.setattr(dedup_mod, "MinHashLSH", FakeLSH)

    vacancies = [
        _v("hh:1", "hh", "Python Backend", "Acme", "Москва", "fastapi postgres"),
        _v("tg:1", "telegram", "Java Analyst", "Beta", "Казань", "excel bi reporting"),
    ]

    assert find_duplicates(vacancies, threshold=0.99) == []


def test_pair_dataclass_immutable():
    p = DuplicatePair(id_a="a", id_b="b", source_a="hh", source_b="tg", jaccard=0.9)
    with pytest.raises(Exception):
        p.jaccard = 0.5  # type: ignore[misc]
