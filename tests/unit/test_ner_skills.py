"""Tests для lemma-aware skill matcher. Требует spaCy + ru_core_news_sm.
Если они отсутствуют (ML-deps не установлены) — модуль гарантирует fallback
на regex baseline через `extract_skills_best_effort`.
"""
from __future__ import annotations

import pytest

from src.enrich.ner_skills import (
    _build_matcher,
    _load_nlp,
    extract_skills_best_effort,
    extract_skills_lemma,
)


@pytest.fixture(autouse=True)
def _clear_caches():
    _load_nlp.cache_clear()
    _build_matcher.cache_clear()
    yield
    _load_nlp.cache_clear()
    _build_matcher.cache_clear()


def test_best_effort_without_ner_uses_regex_baseline_only():
    skills = extract_skills_best_effort(
        "опыт с Python и Django", use_ner=False
    )
    assert "Python" in skills
    assert "Django" in skills


def test_best_effort_with_ner_includes_regex_matches():
    """Ner-mode не должен терять ничего из regex baseline."""
    text = "Python, Django, PostgreSQL"
    base = extract_skills_best_effort(text, use_ner=False)
    enriched = extract_skills_best_effort(text, use_ner=True)
    assert set(base).issubset(set(enriched))


def test_best_effort_empty_input():
    assert extract_skills_best_effort(None) == []
    assert extract_skills_best_effort("") == []


def test_lemma_empty_input_does_not_load_spacy(monkeypatch):
    def boom():
        raise AssertionError("spaCy must not load for empty text")

    monkeypatch.setattr("src.enrich.ner_skills._load_nlp", boom)

    assert extract_skills_lemma(None) == []
    assert extract_skills_lemma("") == []


def test_lemma_ignores_unknown_match_ids(monkeypatch, tmp_path):
    class FakeNlp:
        def __call__(self, text):
            return {"text": text}

    def fake_matcher(doc):
        assert doc == {"text": "Python and Django"}
        return [(1, 0, 1), (2, 1, 2)]

    monkeypatch.setattr("src.enrich.ner_skills._load_nlp", lambda: FakeNlp())
    monkeypatch.setattr(
        "src.enrich.ner_skills._build_matcher",
        lambda _path: (fake_matcher, {2: "Django"}),
    )

    assert extract_skills_lemma("Python and Django", tmp_path / "taxonomy.yaml") == ["Django"]


def test_best_effort_falls_back_when_lemma_extractor_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr("src.enrich.ner_skills.extract_skills", lambda text, path: ["Python"])

    def raise_import_error(text, path):
        raise ImportError("spacy missing")

    monkeypatch.setattr("src.enrich.ner_skills.extract_skills_lemma", raise_import_error)

    assert extract_skills_best_effort("Python", tmp_path / "taxonomy.yaml", use_ner=True) == [
        "Python"
    ]


@pytest.mark.slow
def test_lemma_matcher_finds_inflected_cyrillic():
    """spaCy lemma matching должен ловить «питоном» — регексп baseline
    с word-boundary lookarounds эту склонённую форму пропускает.

    Skip ifs: spaCy не поставлен / model не скачана / torch DLL init
    fail (Win + sentence-transformers известный конфликт DLL,
    не относится к тестируемой логике).
    """
    spacy = pytest.importorskip("spacy")
    try:
        spacy.load("ru_core_news_sm")
    except (OSError, ImportError) as exc:
        pytest.skip(f"spacy/ru-model/torch недоступен: {exc}")

    from src.enrich.ner_skills import extract_skills_lemma

    skills = extract_skills_lemma("Работаем с питоном ежедневно")
    assert "Python" in skills
