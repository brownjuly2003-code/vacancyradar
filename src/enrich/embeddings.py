"""Vacancy embeddings store на sentence-transformers + Lance.

Модель: `paraphrase-multilingual-mpnet-base-v2` (768-dim, многоязычная,
русский + английский). Кэш модели — стандартный HuggingFace
(`HF_HOME` или `~/.cache/huggingface/hub`).

Lance store layout:
  master/embeddings.lance/
    vacancy_id (utf8) | vector (fixed_size_list<float, 768>) | text_hash (utf8)

`text_hash` нужен чтобы пропустить re-encode когда title+teaser не
менялись между runs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

# Prime torch DLL load ДО pyarrow/lance. На Win + torch 2.10 порядок import
# критичен: `import lance` подгружает CRT DLL который потом блокирует
# `import torch` с WinError 1114. read_existing_vectors() и write_lance_arrays()
# импортируют lance внутри, поэтому prime обязан быть на module-level.
# try/except — чтобы модуль импортировался даже без ML deps (для unit tests
# через synthetic vectors без encode_texts).
try:
    import torch  # noqa: F401
except ImportError:
    pass


if TYPE_CHECKING:  # pragma: no cover
    from sentence_transformers import SentenceTransformer


DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
EMBEDDING_DIM = 768
DEFAULT_LANCE_PATH = Path("master/embeddings.lance")


@dataclass
class EmbeddingRow:
    vacancy_id: str
    text: str

    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=2)
def _load_model(name: str = DEFAULT_MODEL) -> "SentenceTransformer":
    """Lazy-load (≈1 GB resident, первый раз — download model файлов с HF)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def encode_texts(
    texts: list[str],
    *,
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 32,
) -> np.ndarray:
    """Texts → np.ndarray shape (N, EMBEDDING_DIM), float32."""
    if not texts:
        return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    model = _load_model(model_name)
    vectors = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return vectors.astype(np.float32)


def write_lance(
    rows: list[EmbeddingRow],
    vectors: np.ndarray,
    out_path: Path = DEFAULT_LANCE_PATH,
) -> int:
    """Write/overwrite Lance dataset. Returns rows written.

    `vectors` ожидается shape (len(rows), EMBEDDING_DIM).
    """
    if len(rows) != len(vectors):
        raise ValueError(f"rows ({len(rows)}) != vectors ({len(vectors)})")
    return write_lance_arrays(
        [r.vacancy_id for r in rows],
        [r.text_hash() for r in rows],
        vectors,
        out_path,
    )


def write_lance_arrays(
    vacancy_ids: list[str],
    text_hashes: list[str],
    vectors: np.ndarray,
    out_path: Path = DEFAULT_LANCE_PATH,
) -> int:
    """Write/overwrite Lance напрямую из массивов — нужен для merge-сценариев,
    где нельзя пересобирать text_hash из EmbeddingRow (хеш приходит из
    существующего store вместе с уже-encoded вектором).
    """
    import lance
    import pyarrow as pa

    n = len(vacancy_ids)
    if not (n == len(text_hashes) == len(vectors)):
        raise ValueError("ids/hashes/vectors length mismatch")
    if n > 0 and vectors.shape[1] != EMBEDDING_DIM:
        raise ValueError(f"vectors dim {vectors.shape[1]} != {EMBEDDING_DIM}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "vacancy_id": vacancy_ids,
            "vector": pa.FixedSizeListArray.from_arrays(
                pa.array(vectors.flatten(), type=pa.float32()),
                EMBEDDING_DIM,
            ),
            "text_hash": text_hashes,
        }
    )
    lance.write_dataset(table, out_path, mode="overwrite")
    return n


def read_existing_hashes(path: Path = DEFAULT_LANCE_PATH) -> dict[str, str]:
    """Map vacancy_id → text_hash из существующего Lance store. {} если нет."""
    import lance

    if not path.exists():
        return {}
    ds = lance.dataset(str(path))
    table = ds.to_table(columns=["vacancy_id", "text_hash"])
    return dict(zip(table["vacancy_id"].to_pylist(), table["text_hash"].to_pylist()))


def read_existing_vectors(
    path: Path = DEFAULT_LANCE_PATH,
) -> tuple[list[str], list[str], np.ndarray]:
    """Полное чтение store: (vacancy_ids, text_hashes, vectors). Пустые если нет."""
    import lance

    if not path.exists():
        return [], [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    ds = lance.dataset(str(path))
    table = ds.to_table(columns=["vacancy_id", "text_hash", "vector"])
    if table.num_rows == 0:
        return [], [], np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    ids = table["vacancy_id"].to_pylist()
    hashes = table["text_hash"].to_pylist()
    flat = np.asarray(table["vector"].to_pylist(), dtype=np.float32)
    if flat.ndim == 1:
        flat = flat.reshape(-1, EMBEDDING_DIM)
    return ids, hashes, flat


def needs_reencode(rows: list[EmbeddingRow], existing: dict[str, str]) -> list[EmbeddingRow]:
    """Filter rows которые отсутствуют в Lance или поменяли text_hash."""
    return [r for r in rows if existing.get(r.vacancy_id) != r.text_hash()]


def cosine_search(
    query_vector: np.ndarray,
    path: Path = DEFAULT_LANCE_PATH,
    k: int = 10,
) -> list[tuple[str, float]]:
    """Top-k vacancy_ids по cosine similarity. Vectors ожидаются normalized
    (encode_texts уже это делает), так что dot product = cosine.
    """
    import lance

    if not path.exists():
        return []
    ds = lance.dataset(str(path))
    table = ds.to_table(columns=["vacancy_id", "vector"])
    if table.num_rows == 0:
        return []

    ids = table["vacancy_id"].to_pylist()
    flat = np.asarray(table["vector"].to_pylist(), dtype=np.float32)
    if flat.ndim == 1:  # pyarrow возвращает list-of-list, страхуемся
        flat = flat.reshape(-1, EMBEDDING_DIM)
    scores = flat @ query_vector.astype(np.float32)
    top_k_idx = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in top_k_idx]
