"""Тесты на `cli._enrich_embeddings` без реальных torch/lance/HF.

Покрытие cli.py 523-601. Функция импортирует ML deps **внутри** body (а не
module-level), поэтому monkeypatch на `src.enrich.embeddings.*` ловится в
момент вызова `_enrich_embeddings`. `build_slim_active` также импортирован
внутри функции, аналогично перехватывается через
`src.transform.slim_export.build_slim_active`.

Никакие из тестов не загружают модель и не пишут Lance — все ML-вызовы
заменены лёгкими stub'ами.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.cli import _enrich_embeddings
from src.enrich.embeddings import EMBEDDING_DIM, EmbeddingRow


def _make_args(*, batch_size: int = 32, force: bool = False, limit: int | None = None) -> Namespace:
    """argparse Namespace соответствует enrich subparser в src/cli.py:118-123."""
    return Namespace(batch_size=batch_size, force=force, limit=limit)


def _patch_slim(monkeypatch, df: pl.DataFrame) -> dict[str, Any]:
    """Перехватывает build_slim_active. Возвращает dict с captured args."""
    captured: dict[str, Any] = {}

    def fake_build_slim_active(lake: Path, *, limit: int | None = None) -> pl.DataFrame:
        captured["lake"] = lake
        captured["limit"] = limit
        return df

    monkeypatch.setattr("src.transform.slim_export.build_slim_active", fake_build_slim_active)
    return captured


def _patch_embeddings(
    monkeypatch,
    *,
    existing: tuple[list[str], list[str], np.ndarray] | None = None,
    encode_returns: np.ndarray | None = None,
) -> dict[str, Any]:
    """Перехватывает read_existing_vectors / encode_texts / write_lance_arrays /
    needs_reencode. Возвращает dict с captured call args."""
    captured: dict[str, Any] = {"encode_calls": 0, "write_calls": 0}

    if existing is None:
        existing = ([], [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32))

    def fake_read(path: Path):
        captured["read_path"] = path
        return existing

    def fake_encode(texts: list[str], batch_size: int = 32) -> np.ndarray:
        captured["encode_calls"] += 1
        captured["encode_texts"] = list(texts)
        captured["encode_batch_size"] = batch_size
        if encode_returns is not None:
            return encode_returns
        # synthetic deterministic vectors (one per text), не модель.
        return np.ones((len(texts), EMBEDDING_DIM), dtype=np.float32)

    def fake_write(ids: list[str], hashes: list[str], vectors: np.ndarray, path: Path) -> int:
        captured["write_calls"] += 1
        captured["write_ids"] = list(ids)
        captured["write_hashes"] = list(hashes)
        captured["write_vectors_shape"] = vectors.shape
        captured["write_path"] = path
        return len(ids)

    def real_needs_reencode(rows: list[EmbeddingRow], existing_map: dict[str, str]) -> list[EmbeddingRow]:
        # Реальная логика тривиальна, можно reuse — но через monkeypatch
        # на копию, чтобы не зависеть от prod-import.
        return [r for r in rows if existing_map.get(r.vacancy_id) != r.text_hash()]

    monkeypatch.setattr("src.enrich.embeddings.read_existing_vectors", fake_read)
    monkeypatch.setattr("src.enrich.embeddings.encode_texts", fake_encode)
    monkeypatch.setattr("src.enrich.embeddings.write_lance_arrays", fake_write)
    monkeypatch.setattr("src.enrich.embeddings.needs_reencode", real_needs_reencode)
    return captured


def test_enrich_embeddings_empty_slim_returns_3(monkeypatch, capsys):
    """`build_slim_active` вернул пустой DF → exit 3 без encode."""
    _patch_slim(monkeypatch, pl.DataFrame())
    captured = _patch_embeddings(monkeypatch)

    assert _enrich_embeddings(_make_args()) == 3
    assert captured["encode_calls"] == 0
    assert captured["write_calls"] == 0
    assert "empty slim" in capsys.readouterr().err


def test_enrich_embeddings_no_text_rows_returns_3(monkeypatch, capsys):
    """slim непустой, но title и description_teaser пустые везде → exit 3."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1", "hh:2"],
            "title": [None, ""],
            "description_teaser": ["", None],
        }
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(monkeypatch)

    assert _enrich_embeddings(_make_args()) == 3
    assert captured["encode_calls"] == 0
    assert "no slim rows with non-empty text" in capsys.readouterr().err


def test_enrich_embeddings_force_skips_existing_read(monkeypatch, capsys):
    """force=True → read_existing_vectors НЕ вызывается, encode идёт по всем rows."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1", "hh:2"],
            "title": ["Python Dev", "Data Engineer"],
            "description_teaser": ["FastAPI", "Airflow + Kafka"],
        }
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(monkeypatch)

    assert _enrich_embeddings(_make_args(force=True)) == 0
    assert "read_path" not in captured  # force bypass — line 562-563
    assert captured["encode_calls"] == 1
    assert captured["encode_texts"] == ["Python Dev FastAPI", "Data Engineer Airflow + Kafka"]
    assert captured["write_calls"] == 1
    assert captured["write_ids"] == ["hh:1", "hh:2"]
    assert captured["write_vectors_shape"] == (2, EMBEDDING_DIM)
    out = capsys.readouterr().out
    assert "force=True" in out


def test_enrich_embeddings_nothing_to_encode_exits_early(monkeypatch, capsys):
    """force=False + все hashes match existing → todo пуст → return 0 без encode."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1"],
            "title": ["Python"],
            "description_teaser": ["FastAPI"],
        }
    )
    # Pre-compute matching hash чтобы needs_reencode = [].
    row = EmbeddingRow(vacancy_id="hh:1", text="Python FastAPI")
    existing = (
        ["hh:1"],
        [row.text_hash()],
        np.zeros((1, EMBEDDING_DIM), dtype=np.float32),
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(monkeypatch, existing=existing)

    assert _enrich_embeddings(_make_args(force=False)) == 0
    assert captured["encode_calls"] == 0
    assert captured["write_calls"] == 0
    assert "nothing to encode" in capsys.readouterr().out


def test_enrich_embeddings_incremental_preserves_kept_existing(monkeypatch, capsys):
    """Happy path: todo=2 changed, existing=3 (1 kept, 2 stale-but-still-in-todo
    через mismatched hash). Combined order = todo first, kept after.
    Vectors конкатенируются: new (2) сверху, kept (1) снизу."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1", "hh:2", "hh:3"],
            "title": ["Title 1", "Title 2", "Title 3"],
            "description_teaser": ["Body 1", "Body 2", "Body 3"],
        }
    )
    # existing: hh:3 matches (kept), hh:1 mismatches, hh:99 (not в slim → not kept, исчезает).
    matched_row = EmbeddingRow(vacancy_id="hh:3", text="Title 3 Body 3")
    existing_ids = ["hh:1", "hh:3", "hh:99"]
    existing_hashes = ["STALE_HASH_FOR_HH1", matched_row.text_hash(), "WHATEVER_HH99"]
    existing_vecs = np.array(
        [
            [0.1] * EMBEDDING_DIM,
            [0.3] * EMBEDDING_DIM,  # kept
            [0.99] * EMBEDDING_DIM,  # not in slim — должна оставаться kept (in existing → keep)
        ],
        dtype=np.float32,
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(
        monkeypatch,
        existing=(existing_ids, existing_hashes, existing_vecs),
        encode_returns=np.array(
            [
                [0.7] * EMBEDDING_DIM,  # for hh:1
                [0.8] * EMBEDDING_DIM,  # for hh:2
            ],
            dtype=np.float32,
        ),
    )

    assert _enrich_embeddings(_make_args(force=False)) == 0
    # todo по факту: hh:1 (hash mismatch) + hh:2 (отсутствует в existing).
    # hh:3 matches → keep. hh:99 нет в slim, но он в existing → keep_idx обходит todo_ids,
    # поэтому hh:99 тоже остаётся в combined.
    assert captured["encode_calls"] == 1
    assert captured["encode_texts"] == ["Title 1 Body 1", "Title 2 Body 2"]
    # combined order: новые todo сначала, потом существующие не из todo_ids.
    assert captured["write_ids"] == ["hh:1", "hh:2", "hh:3", "hh:99"]
    # Vectors: new (2) || kept (2) = shape (4, EMBEDDING_DIM).
    assert captured["write_vectors_shape"] == (4, EMBEDDING_DIM)
    out = capsys.readouterr().out
    assert "slim=3" in out
    assert "existing_lance=3" in out
    assert "to_encode=2" in out


def test_enrich_embeddings_limit_truncates_rows(monkeypatch):
    """args.limit=2 обрезает rows ДО encode (не encode'ит всё, потом отбрасывает)."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1", "hh:2", "hh:3"],
            "title": ["A", "B", "C"],
            "description_teaser": ["x", "y", "z"],
        }
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(monkeypatch)

    assert _enrich_embeddings(_make_args(force=True, limit=2)) == 0
    assert captured["encode_texts"] == ["A x", "B y"]
    assert captured["write_ids"] == ["hh:1", "hh:2"]


@pytest.mark.parametrize("batch_size", [16, 64])
def test_enrich_embeddings_passes_batch_size_to_encoder(monkeypatch, batch_size):
    """--batch-size прокидывается в encode_texts (line 578)."""
    df = pl.DataFrame(
        {
            "vacancy_id": ["hh:1"],
            "title": ["Python"],
            "description_teaser": ["FastAPI"],
        }
    )
    _patch_slim(monkeypatch, df)
    captured = _patch_embeddings(monkeypatch)

    assert _enrich_embeddings(_make_args(force=True, batch_size=batch_size)) == 0
    assert captured["encode_batch_size"] == batch_size
