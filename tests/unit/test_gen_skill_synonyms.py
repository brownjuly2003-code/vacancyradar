from __future__ import annotations

from pathlib import Path

from tools import gen_skill_synonyms


def test_main_writes_lf_line_endings(tmp_path: Path, monkeypatch):
    taxonomy_path = tmp_path / "skills_taxonomy.yaml"
    output_path = tmp_path / "skill-synonyms.json"
    taxonomy_path.write_text(
        "- {canonical: Computer Vision, aliases: [\"ml/cv\", \"cv engineer\"]}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(gen_skill_synonyms, "TAXONOMY_PATH", taxonomy_path)
    monkeypatch.setattr(gen_skill_synonyms, "OUTPUT_PATH", output_path)

    gen_skill_synonyms.main()

    payload = output_path.read_bytes()
    assert b"\r\n" not in payload
    assert b"\n" in payload
