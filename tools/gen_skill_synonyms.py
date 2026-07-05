"""Generate web/lib/skill-synonyms.json from data/skills_taxonomy.yaml.

Output map: alias_lower -> [expansion_terms_lower_unique_sorted].
- alias_lower = each canonical name + each alias, lowercased.
- expansion_terms = the canonical AND every alias from the same skill, lowercased
  and deduplicated. So both "питон" and "python" expand to the same set.
- Aliases shorter than MIN_ALIAS_LEN are skipped (py/js/go/c — too greedy as
  ILIKE substring, would match "polyglot", "deploy", "deploys", "google").

Usage:
    D:/Python/Python312/python.exe -m tools.gen_skill_synonyms

The JSON is checked in alongside the source yaml. /api/search loads it at
runtime to expand single-word Russian queries (e.g. "питон" -> Python jobs).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


TAXONOMY_PATH = Path("data/skills_taxonomy.yaml")
OUTPUT_PATH = Path("web/lib/skill-synonyms.json")
MIN_ALIAS_LEN = 3


def build_synonym_map(taxonomy: list[dict]) -> dict[str, list[str]]:
    by_alias: dict[str, list[str]] = {}
    for entry in taxonomy:
        canonical = entry["canonical"]
        terms = {canonical.lower(), *(a.lower() for a in entry.get("aliases", []))}
        terms = {t for t in terms if len(t) >= MIN_ALIAS_LEN}
        if not terms:
            continue
        expansion = sorted(terms)
        for alias in terms:
            existing = by_alias.get(alias)
            if existing is None:
                by_alias[alias] = expansion
            else:
                merged = sorted(set(existing) | set(expansion))
                by_alias[alias] = merged
    return dict(sorted(by_alias.items()))


def main() -> None:
    raw = yaml.safe_load(TAXONOMY_PATH.read_text(encoding="utf-8")) or []
    synonyms = build_synonym_map(raw)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(synonyms, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"wrote {OUTPUT_PATH} — {len(synonyms)} aliases, {sum(len(v) for v in synonyms.values())} expansion entries")


if __name__ == "__main__":
    main()
