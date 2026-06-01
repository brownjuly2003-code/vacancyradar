"""Lemma-aware skills extractor поверх spaCy `ru_core_news_sm`.

Дополнение к `src/enrich/skills_match.py` (regex baseline). NER-вариант
использует spaCy PhraseMatcher с `LEMMA` attribute, что находит
кириличные варианты в склонении («с питоном», «через постгрес»),
которых regex baseline пропускает.

Источник терминов — тот же `data/skills_taxonomy.yaml`.

Опционален. Для проектов без `make install-ml` — fallback на regex
matcher через `extract_skills_best_effort(text)`.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from src.enrich.skills_match import DEFAULT_TAXONOMY_PATH, extract_skills


if TYPE_CHECKING:  # pragma: no cover
    from spacy.language import Language


@lru_cache(maxsize=1)
def _load_nlp() -> "Language":
    """Lazy-load spaCy ru pipeline (≈250 MB resident, ~2s startup)."""
    import spacy

    return spacy.load("ru_core_news_sm", disable=["parser", "ner"])


@lru_cache(maxsize=4)
def _build_matcher(taxonomy_path_str: str):
    """Build PhraseMatcher с LEMMA attribute по всем variants."""
    from spacy.matcher import PhraseMatcher

    nlp = _load_nlp()
    raw = yaml.safe_load(Path(taxonomy_path_str).read_text(encoding="utf-8")) or []

    matcher = PhraseMatcher(nlp.vocab, attr="LEMMA")
    canonical_by_match_id: dict[int, str] = {}
    for entry in raw:
        canonical = entry["canonical"]
        match_id = nlp.vocab.strings.add(canonical)
        canonical_by_match_id[match_id] = canonical
        # Patterns должны пройти full pipeline (нужен lemmatizer assignment),
        # nlp.make_doc даёт только tokenizer → PhraseMatcher с attr=LEMMA падает.
        patterns = list(nlp.pipe([canonical, *entry.get("aliases", [])]))
        matcher.add(canonical, patterns)
    return matcher, canonical_by_match_id


def extract_skills_lemma(
    text: str | None,
    taxonomy_path: Path = DEFAULT_TAXONOMY_PATH,
) -> list[str]:
    """spaCy lemma-aware extraction. Возвращает sorted unique canonical names.

    Замечание: для не-кириличных идентификаторов (Python, ClickHouse) spaCy
    не лемматизирует, так что результат покрывает то же что regex baseline +
    дополнительно ловит склонённые кириличные варианты.
    """
    if not text:
        return []
    nlp = _load_nlp()
    matcher, canonical_by_match_id = _build_matcher(str(taxonomy_path))
    doc = nlp(text)
    found: set[str] = set()
    for match_id, _start, _end in matcher(doc):
        if (canonical := canonical_by_match_id.get(match_id)):
            found.add(canonical)
    return sorted(found)


def extract_skills_best_effort(
    text: str | None,
    taxonomy_path: Path = DEFAULT_TAXONOMY_PATH,
    *,
    use_ner: bool = True,
) -> list[str]:
    """Регexp baseline + (опционально) lemma matching, объединение.

    use_ner=False — чистый regex (для тестов / проектов без ML deps).
    Если spaCy недоступен — сваливается обратно на regex с предупреждением.
    """
    base = set(extract_skills(text, taxonomy_path))
    if not use_ner or not text:
        return sorted(base)
    try:
        ner = set(extract_skills_lemma(text, taxonomy_path))
    except (ImportError, OSError):
        return sorted(base)
    return sorted(base | ner)
