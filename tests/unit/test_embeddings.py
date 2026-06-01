"""Unit tests для embeddings store. Реальную sentence-transformers модель
НЕ вызываем (~1 GB resident, slow). Тестируем storage layer на synthetic
vectors. Encoder smoke-тест помечен `slow` и опционален.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import numpy as np
import pytest

import src.enrich.embeddings as embeddings_mod
from src.enrich.embeddings import (
    EMBEDDING_DIM,
    EmbeddingRow,
    cosine_search,
    encode_texts,
    needs_reencode,
    read_existing_hashes,
    read_existing_vectors,
    write_lance,
    write_lance_arrays,
)


def _synthetic_vectors(n: int, *, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal((n, EMBEDDING_DIM)).astype(np.float32)
    # normalise (как делает encode_texts → cosine = dot product)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    return v / norms


def test_text_hash_deterministic():
    r1 = EmbeddingRow("hh:1", "Senior Python Developer")
    r2 = EmbeddingRow("hh:1", "Senior Python Developer")
    r3 = EmbeddingRow("hh:1", "Senior Python Developer ")  # trailing space
    assert r1.text_hash() == r2.text_hash()
    assert r1.text_hash() != r3.text_hash()


def test_encode_texts_empty_list_returns_zero_array():
    out = encode_texts([])
    assert out.shape == (0, EMBEDDING_DIM)
    assert out.dtype == np.float32


def test_module_import_tolerates_missing_torch(monkeypatch):
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "torch", None)
        reloaded = importlib.reload(embeddings_mod)

    importlib.reload(embeddings_mod)

    assert reloaded.EMBEDDING_DIM == EMBEDDING_DIM


def test_load_model_uses_sentence_transformers_class(monkeypatch):
    created = []

    class FakeSentenceTransformer:
        def __init__(self, name: str) -> None:
            created.append(name)

    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    embeddings_mod._load_model.cache_clear()

    try:
        model = embeddings_mod._load_model("fake-model")
    finally:
        embeddings_mod._load_model.cache_clear()

    assert isinstance(model, FakeSentenceTransformer)
    assert created == ["fake-model"]


def test_encode_texts_non_empty_uses_model_and_float32(monkeypatch):
    calls = {}

    class FakeModel:
        def encode(self, texts, **kwargs):
            calls["texts"] = texts
            calls["kwargs"] = kwargs
            return np.ones((len(texts), EMBEDDING_DIM), dtype=np.float64)

    monkeypatch.setattr("src.enrich.embeddings._load_model", lambda _name: FakeModel())

    out = encode_texts(["alpha", "beta"], model_name="fake-model", batch_size=7)

    assert out.shape == (2, EMBEDDING_DIM)
    assert out.dtype == np.float32
    assert calls["texts"] == ["alpha", "beta"]
    assert calls["kwargs"]["batch_size"] == 7
    assert calls["kwargs"]["normalize_embeddings"] is True
    assert calls["kwargs"]["show_progress_bar"] is False
    assert calls["kwargs"]["convert_to_numpy"] is True


def test_write_lance_roundtrip(tmp_path: Path):
    rows = [EmbeddingRow(f"hh:{i}", f"text {i}") for i in range(3)]
    vectors = _synthetic_vectors(3)
    out = tmp_path / "emb.lance"

    n_written = write_lance(rows, vectors, out)
    assert n_written == 3
    assert out.exists()

    hashes = read_existing_hashes(out)
    assert set(hashes.keys()) == {"hh:0", "hh:1", "hh:2"}
    assert hashes["hh:1"] == EmbeddingRow("hh:1", "text 1").text_hash()


def test_write_lance_dim_mismatch_raises(tmp_path: Path):
    rows = [EmbeddingRow("hh:1", "x")]
    bad_vectors = np.zeros((1, EMBEDDING_DIM - 1), dtype=np.float32)
    with pytest.raises(ValueError, match="dim"):
        write_lance(rows, bad_vectors, tmp_path / "bad.lance")


def test_write_lance_count_mismatch_raises(tmp_path: Path):
    rows = [EmbeddingRow("hh:1", "x")]
    too_many = _synthetic_vectors(2)
    with pytest.raises(ValueError, match="rows"):
        write_lance(rows, too_many, tmp_path / "bad.lance")


def test_write_lance_arrays_count_mismatch_raises(tmp_path: Path):
    vectors = _synthetic_vectors(1)

    with pytest.raises(ValueError, match="length mismatch"):
        write_lance_arrays(["hh:1"], [], vectors, tmp_path / "bad.lance")


def test_read_existing_hashes_empty_when_no_dataset(tmp_path: Path):
    assert read_existing_hashes(tmp_path / "missing.lance") == {}


def test_needs_reencode_picks_new_and_changed():
    rows = [
        EmbeddingRow("hh:1", "old text"),
        EmbeddingRow("hh:2", "unchanged"),
        EmbeddingRow("hh:3", "brand new"),
    ]
    existing = {
        "hh:1": EmbeddingRow("hh:1", "DIFFERENT old text").text_hash(),  # changed
        "hh:2": EmbeddingRow("hh:2", "unchanged").text_hash(),            # same
        # hh:3 not present at all
    }
    todo = needs_reencode(rows, existing)
    assert {r.vacancy_id for r in todo} == {"hh:1", "hh:3"}


def test_cosine_search_returns_top_k_in_order(tmp_path: Path):
    """Embed orthogonal-ish vectors, query the first; ranking should put it first."""
    rows = [EmbeddingRow(f"hh:{i}", f"t{i}") for i in range(5)]
    vectors = _synthetic_vectors(5, seed=42)
    out = tmp_path / "emb.lance"
    write_lance(rows, vectors, out)

    query = vectors[2]  # ищем тот же вектор что у hh:2 → первое место
    top = cosine_search(query, path=out, k=3)
    assert len(top) == 3
    assert top[0][0] == "hh:2"
    assert top[0][1] == pytest.approx(1.0, abs=1e-5)
    # scores monotonically decreasing
    assert top[0][1] >= top[1][1] >= top[2][1]


def test_cosine_search_empty_when_no_dataset(tmp_path: Path):
    query = _synthetic_vectors(1)[0]
    assert cosine_search(query, path=tmp_path / "missing.lance", k=5) == []


def test_cosine_search_empty_when_dataset_has_no_rows(tmp_path: Path):
    out = tmp_path / "empty.lance"
    write_lance_arrays([], [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32), out)

    query = _synthetic_vectors(1)[0]

    assert cosine_search(query, path=out, k=5) == []


def test_read_existing_vectors_returns_full_arrays(tmp_path: Path):
    rows = [EmbeddingRow(f"hh:{i}", f"text {i}") for i in range(3)]
    vectors = _synthetic_vectors(3, seed=11)
    out = tmp_path / "emb.lance"
    write_lance(rows, vectors, out)

    ids, hashes, vecs = read_existing_vectors(out)
    assert ids == ["hh:0", "hh:1", "hh:2"]
    assert hashes[1] == EmbeddingRow("hh:1", "text 1").text_hash()
    assert vecs.shape == (3, EMBEDDING_DIM)
    # roundtrip близок к идентичному (float32 quantization вне зоны видимости)
    assert vecs[0] == pytest.approx(vectors[0], abs=1e-6)


def test_read_existing_vectors_empty_when_missing(tmp_path: Path):
    ids, hashes, vecs = read_existing_vectors(tmp_path / "missing.lance")
    assert ids == []
    assert hashes == []
    assert vecs.shape == (0, EMBEDDING_DIM)


def test_read_existing_vectors_empty_when_dataset_has_no_rows(tmp_path: Path):
    out = tmp_path / "empty.lance"
    write_lance_arrays([], [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32), out)

    ids, hashes, vecs = read_existing_vectors(out)

    assert ids == []
    assert hashes == []
    assert vecs.shape == (0, EMBEDDING_DIM)


def test_read_vectors_and_search_accept_flat_vector_lists(tmp_path: Path, monkeypatch):
    path = tmp_path / "fake.lance"
    path.mkdir()
    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)

    class FakeColumn:
        def __init__(self, values):
            self.values = values

        def to_pylist(self):
            return self.values

    class FakeTable:
        num_rows = 1

        def __getitem__(self, key: str):
            values = {
                "vacancy_id": ["hh:1"],
                "text_hash": ["hash1"],
                "vector": vector,
            }
            return FakeColumn(values[key])

    class FakeDataset:
        def to_table(self, *, columns):
            return FakeTable()

    monkeypatch.setitem(
        sys.modules,
        "lance",
        types.SimpleNamespace(dataset=lambda _path: FakeDataset()),
    )

    ids, hashes, vecs = read_existing_vectors(path)
    top = cosine_search(np.asarray(vector, dtype=np.float32), path=path, k=1)

    assert ids == ["hh:1"]
    assert hashes == ["hash1"]
    assert vecs.shape == (1, EMBEDDING_DIM)
    assert top == [("hh:1", pytest.approx(1.0))]


def test_write_lance_arrays_supports_merge_scenario(tmp_path: Path):
    """P2 fix CX: инкрементальный re-encode должен сохранять unchanged
    embeddings из старого store, не только todo. Проверяем чистый
    write_lance_arrays на combined ids/hashes/vectors.
    """
    out = tmp_path / "emb.lance"
    initial_rows = [EmbeddingRow(f"hh:{i}", f"v{i}") for i in range(3)]
    initial_vecs = _synthetic_vectors(3, seed=1)
    write_lance(initial_rows, initial_vecs, out)

    # Имитация инкремента: hh:1 изменился (новый text), hh:0 и hh:2 — нет
    new_row = EmbeddingRow("hh:1", "v1-NEW")
    new_vec = _synthetic_vectors(1, seed=99)

    existing_ids, existing_hashes, existing_vecs = read_existing_vectors(out)
    keep_idx = [i for i, vid in enumerate(existing_ids) if vid != "hh:1"]
    combined_ids = [new_row.vacancy_id] + [existing_ids[i] for i in keep_idx]
    combined_hashes = [new_row.text_hash()] + [existing_hashes[i] for i in keep_idx]
    combined_vecs = np.concatenate([new_vec, existing_vecs[keep_idx]])

    n = write_lance_arrays(combined_ids, combined_hashes, combined_vecs, out)
    assert n == 3

    final = dict(zip(*read_existing_vectors(out)[:2]))
    assert set(final.keys()) == {"hh:0", "hh:1", "hh:2"}
    assert final["hh:1"] == new_row.text_hash()
    assert final["hh:0"] == EmbeddingRow("hh:0", "v0").text_hash()  # сохранён
