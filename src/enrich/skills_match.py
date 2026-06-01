"""Детерминированный skills matcher по `data/skills_taxonomy.yaml`.

Без NER/ML — Aho-Corasick automaton (pyahocorasick) с manual word-boundary
проверкой. AC даёт O(N + matches) на текст vs O(N * M) у `re.finditer` с
alternation (M = число вариантов). На 627 вариантах × 290 canonical
сокращает per-row cost с ~60 ms (legacy regex) до < 1 ms на типичном
HH description (~2 KB).

Поведение совпадает с прошлым re-based матчером:
- case-insensitive: текст и варианты приводятся к lower
- word-boundary lookalike (`(?<!\\w)variant(?!\\w)` в legacy regex):
  если граничный символ варианта alphanumeric/underscore, требуем не-word
  соседа; иначе boundary skipped (для `.NET`, `C++`, `C#`)
- longest-match-at-position wins: `React Native` "съедает" `React`, если
  они стартуют в одной позиции — повторяет re.finditer над sorted-by-len-desc
  alternation. AC сам по себе возвращает все matches, longest-greedy фильтр
  поверх восстанавливает legacy semantics.

Контракт: `extract_skills(text)` → отсортированный list[str] уникальных
canonical имён. `None`/пусто → `[]`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import ahocorasick
import yaml


DEFAULT_TAXONOMY_PATH = Path("data/skills_taxonomy.yaml")

# Session 33: URL slugs (e.g. `wantapply.com/backend-c-developer-at-nexters`)
# выдают FPs для context-anchored aliases типа `c-developer`. Stripping URLs
# перед AC-сканом убирает шум без потери реального signal — skill в живой
# вакансии всегда упоминается в prose, не только в линке.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _is_word_char(ch: str) -> bool:
    """Mirror Python regex `\\w` semantics: unicode alnum or underscore."""
    return ch.isalnum() or ch == "_"


@lru_cache(maxsize=4)
def _load_taxonomy(
    path_str: str,
) -> tuple[
    ahocorasick.Automaton,
    dict[str, str],
    dict[str, tuple[bool, bool]],
    dict[str, tuple[str, ...]],
]:
    """Build Aho-Corasick automaton + lookup maps from the taxonomy yaml.

    Returns ``(automaton, variant_lower→canonical,
    variant_lower→(check_left, check_right), variant_lower→deny_after_tuple)``.
    Boundary flags are True iff the variant edge char is alphanumeric/underscore —
    mirrors the legacy `(?<!\\w)variant(?!\\w)` form (skipped for `.NET`, `C++`
    whose edges are already non-word).

    `deny_after` (session 32): per-entry list of right-context patterns; matches
    where text после variant starts с одного из этих patterns отбрасываются
    в `extract_skills`. Используется для R (`R&D`, SAP `R/3` — не R-language).
    """
    raw = yaml.safe_load(Path(path_str).read_text(encoding="utf-8")) or []
    variant_to_canonical: dict[str, str] = {}
    variant_deny_after: dict[str, tuple[str, ...]] = {}
    for entry in raw:
        canonical = entry["canonical"]
        aliases = entry.get("aliases", [])
        aliases_lower = {a.lower() for a in aliases}
        deny_after = tuple(entry.get("deny_after", []))
        # Short-canonical guard (session 31): для 1-2 символьных canonicals
        # (C, R) author явно курирует aliases — НЕ auto-add canonical_lower
        # если его нет в aliases. Иначе bare `c` matches Cyrillic-confused
        # «с» preposition в RU text, «C-level» — все FPs.
        # Go (canonical 2 chars) безопасен: «go» уже в aliases явно.
        # R (session 32) теперь включает bare `r` обратно + `deny_after` для
        # `R&D` и SAP `R/3` — recovers ~680 real R mentions без noise.
        canonical_lower = canonical.lower()
        skip_canonical = (
            len(canonical_lower) <= 2 and canonical_lower not in aliases_lower
        )
        variants = aliases if skip_canonical else [canonical, *aliases]
        for variant in variants:
            v_lower = variant.lower()
            # Первое сопоставление выигрывает — детерминизм при коллизиях.
            variant_to_canonical.setdefault(v_lower, canonical)
            if deny_after:
                variant_deny_after.setdefault(v_lower, deny_after)

    boundaries: dict[str, tuple[bool, bool]] = {}
    automaton = ahocorasick.Automaton()
    for variant in variant_to_canonical:
        if not variant:
            continue
        boundaries[variant] = (_is_word_char(variant[0]), _is_word_char(variant[-1]))
        automaton.add_word(variant, variant)

    if variant_to_canonical:
        automaton.make_automaton()
    return automaton, variant_to_canonical, boundaries, variant_deny_after


def extract_skills(
    text: str | None,
    taxonomy_path: Path = DEFAULT_TAXONOMY_PATH,
) -> list[str]:
    if not text:
        return []
    automaton, mapping, boundaries, deny_after_map = _load_taxonomy(str(taxonomy_path))
    if not mapping:
        return []

    text_lower = _URL_RE.sub(" ", text.lower())
    text_len = len(text_lower)
    candidates: list[tuple[int, int, str]] = []  # (start, length, canonical)

    for end_idx, variant in automaton.iter(text_lower):
        v_len = len(variant)
        start_idx = end_idx - v_len + 1
        check_left, check_right = boundaries[variant]
        if check_left and start_idx > 0 and _is_word_char(text_lower[start_idx - 1]):
            continue
        right_pos = end_idx + 1
        if check_right and right_pos < text_len and _is_word_char(text_lower[right_pos]):
            continue
        if v_len == 1 and variant.isalpha():
            left_marker_pos = start_idx - 1
            while left_marker_pos >= 0 and text_lower[left_marker_pos] in "*_":
                left_marker_pos -= 1
            if (
                left_marker_pos != start_idx - 1
                and left_marker_pos >= 0
                and _is_word_char(text_lower[left_marker_pos])
            ):
                continue
            right_marker_pos = right_pos
            while right_marker_pos < text_len and text_lower[right_marker_pos] in "*_":
                right_marker_pos += 1
            if (
                right_marker_pos != right_pos
                and right_marker_pos < text_len
                and _is_word_char(text_lower[right_marker_pos])
            ):
                continue
        # Per-variant right-context deny patterns (session 32). Skip leading
        # whitespace в right-context перед сверкой — «R&D» и «R &D» обе denied.
        deny_patterns = deny_after_map.get(variant)
        if deny_patterns:
            right_str = text_lower[right_pos:right_pos + 16].lstrip()
            if any(right_str.startswith(p) for p in deny_patterns):
                continue
        candidates.append((start_idx, v_len, mapping[variant]))

    # Longest-match-at-position, mirroring re.finditer with sort-by-len-desc:
    # scan left-to-right, prefer longest variant at each position, skip past it.
    candidates.sort(key=lambda m: (m[0], -m[1]))
    found: set[str] = set()
    last_end = -1
    for start, length, canonical in candidates:
        if start < last_end:
            continue
        found.add(canonical)
        last_end = start + length
    return sorted(found)
