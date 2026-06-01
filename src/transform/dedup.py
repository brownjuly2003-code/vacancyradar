"""Cross-source vacancy deduplication via MinHash LSH on word shingles.

Используется для пар (hh + telegram) одной и той же вакансии после Phase 4 ingest.
Возвращает пары с Jaccard similarity >= threshold.

Схема:
- Сигнал: title + employer + city + description_teaser (нормализуем).
- Шинглы: k-word rolling window (default k=4).
- Подпись: MinHash(num_perm=128).
- Индекс: MinHashLSH(threshold).
- Filter: cross-source only (source_a != source_b), one pair per id-pair.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from datasketch import MinHash, MinHashLSH


DEFAULT_THRESHOLD = 0.88
DEFAULT_NUM_PERM = 128
DEFAULT_SHINGLE_SIZE = 4

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


@dataclass(frozen=True)
class VacancyForDedup:
    """Минимальный shape для дедупа. Заполняется из slim_active или raw lake."""

    vacancy_id: str
    source: str
    title: str | None
    employer_name: str | None
    city: str | None
    text: str | None  # description_teaser ИЛИ description_fts ИЛИ полный текст из telegram


@dataclass(frozen=True)
class DuplicatePair:
    id_a: str
    id_b: str
    source_a: str
    source_b: str
    jaccard: float


def normalize_text(*parts: str | None) -> str:
    """Lowercase + extract word tokens, объединить в одну строку через пробел."""
    joined = " ".join(p for p in parts if p)
    tokens = _WORD_RE.findall(joined.lower())
    return " ".join(tokens)


def shingles(text: str, k: int = DEFAULT_SHINGLE_SIZE) -> set[str]:
    """k-word rolling window. Если слов < k — вернёт {text} (одиночный shingle)."""
    tokens = text.split()
    if not tokens:
        return set()
    if len(tokens) < k:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def minhash_for(shingle_set: set[str], num_perm: int = DEFAULT_NUM_PERM) -> MinHash:
    mh = MinHash(num_perm=num_perm)
    for sh in shingle_set:
        mh.update(sh.encode("utf-8"))
    return mh


def find_duplicates(
    vacancies: Iterable[VacancyForDedup],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    num_perm: int = DEFAULT_NUM_PERM,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
    cross_source_only: bool = True,
) -> list[DuplicatePair]:
    """Найти near-duplicate пары через MinHash LSH.

    cross_source_only=True (default) фильтрует пары до hh×tg (разные source).
    Same-source пары (hh×hh) обычно не near-duplicates — это либо republish (events
    layer) либо разные роли. Phase 4 use-case = cross-source.
    """
    items: list[tuple[VacancyForDedup, set[str], MinHash]] = []
    for v in vacancies:
        text = normalize_text(v.title, v.employer_name, v.city, v.text)
        sh = shingles(text, k=shingle_size)
        if not sh:
            continue
        mh = minhash_for(sh, num_perm=num_perm)
        items.append((v, sh, mh))

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    for v, _, mh in items:
        lsh.insert(v.vacancy_id, mh)

    seen: set[tuple[str, str]] = set()
    pairs: list[DuplicatePair] = []

    for v, sh_a, mh_a in items:
        for cand_id in lsh.query(mh_a):
            if cand_id == v.vacancy_id:
                continue
            key = tuple(sorted((v.vacancy_id, cand_id)))
            if key in seen:
                continue
            seen.add(key)

            cand = next((c for c in items if c[0].vacancy_id == cand_id), None)
            if cand is None:
                continue
            v_b, sh_b, _ = cand

            if cross_source_only and v.source == v_b.source:
                continue

            jaccard = len(sh_a & sh_b) / max(len(sh_a | sh_b), 1)
            if jaccard < threshold:
                continue

            id_a, id_b = key
            source_a = v.source if v.vacancy_id == id_a else v_b.source
            source_b = v_b.source if v.vacancy_id == id_a else v.source
            pairs.append(
                DuplicatePair(
                    id_a=id_a,
                    id_b=id_b,
                    source_a=source_a,
                    source_b=source_b,
                    jaccard=round(jaccard, 4),
                )
            )

    return pairs
