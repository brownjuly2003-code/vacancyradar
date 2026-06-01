from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from src.enrich.embeddings import EMBEDDING_DIM, write_lance_arrays
from src.publish.embeddings_export import export_to_parquet


def _fake_vectors(n: int, *, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    raw = rng.standard_normal((n, EMBEDDING_DIM)).astype(np.float32)
    norm = np.linalg.norm(raw, axis=1, keepdims=True)
    return (raw / norm).astype(np.float32)


def test_export_to_parquet_round_trip(tmp_path: Path):
    lance_path = tmp_path / "embeddings.lance"
    out_path = tmp_path / "embeddings.parquet"
    ids = ["hh:1", "hh:2", "tg:foo:42"]
    hashes = ["h1", "h2", "h3"]
    vectors = _fake_vectors(len(ids))

    write_lance_arrays(ids, hashes, vectors, lance_path)
    rows = export_to_parquet(out_path, lance_path)

    assert rows == 3
    assert out_path.exists()

    import duckdb

    con = duckdb.connect()
    result = con.execute(
        f"SELECT vacancy_id, vector FROM read_parquet('{out_path.as_posix()}') ORDER BY vacancy_id"
    ).fetchall()
    assert [r[0] for r in result] == sorted(ids)
    for vacancy_id, vec in result:
        assert len(vec) == EMBEDDING_DIM
        original = vectors[ids.index(vacancy_id)]
        np.testing.assert_allclose(np.asarray(vec, dtype=np.float32), original, atol=1e-5)


def test_export_to_parquet_supports_duckdb_array_cosine_similarity(tmp_path: Path):
    """End-to-end check: the Web `semantic-similar` endpoint relies on
    `array_cosine_similarity(vector, query_vector)` over this Parquet.

    DuckDB reads Arrow `fixed_size_list<float, N>` as variable-length `FLOAT[]`,
    but `array_cosine_similarity` requires `FLOAT[ANY]` fixed-size. The Web
    endpoint must `CAST(vector AS FLOAT[768])` — locking that contract here so
    a future schema/dim change breaks the test loudly.
    """
    from src.enrich.embeddings import EMBEDDING_DIM

    lance_path = tmp_path / "embeddings.lance"
    out_path = tmp_path / "embeddings.parquet"
    ids = ["hh:a", "hh:b", "hh:c"]
    vectors = _fake_vectors(len(ids), seed=7)
    write_lance_arrays(ids, ["x"] * 3, vectors, lance_path)
    export_to_parquet(out_path, lance_path)

    import duckdb

    con = duckdb.connect()
    rows = con.execute(
        f"""
        WITH src AS (
            SELECT vacancy_id,
                   CAST(vector AS FLOAT[{EMBEDDING_DIM}]) AS vector
            FROM read_parquet('{out_path.as_posix()}')
        ),
        q AS (SELECT vector FROM src WHERE vacancy_id = 'hh:a')
        SELECT vacancy_id,
               array_cosine_similarity(vector, (SELECT vector FROM q)) AS sim
        FROM src
        ORDER BY sim DESC
        """
    ).fetchall()
    # Self-similarity wins, dot product == 1.0 ± fp noise on normalized vectors.
    assert rows[0][0] == "hh:a"
    assert rows[0][1] == pytest.approx(1.0, abs=1e-5)


def test_export_to_parquet_missing_store_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        export_to_parquet(tmp_path / "out.parquet", tmp_path / "missing.lance")


def test_export_to_parquet_empty_lance_writes_empty_parquet(tmp_path: Path):
    """Lance store with zero rows → write empty parquet with the same schema.

    Locks the `if table.num_rows == 0` branch and the `_empty_table()` helper
    used to keep DuckDB `read_parquet` happy on a freshly-rebuilt corpus.
    """
    lance_path = tmp_path / "embeddings.lance"
    out_path = tmp_path / "embeddings.parquet"
    empty_vectors = np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
    write_lance_arrays([], [], empty_vectors, lance_path)

    rows = export_to_parquet(out_path, lance_path)
    assert rows == 0
    assert out_path.exists()

    import duckdb

    con = duckdb.connect()
    result = con.execute(
        f"SELECT vacancy_id, vector FROM read_parquet('{out_path.as_posix()}')"
    ).fetchall()
    assert result == []
    schema = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{out_path.as_posix()}')"
    ).fetchall()
    cols = {r[0]: r[1] for r in schema}
    assert cols["vacancy_id"] == "VARCHAR"
    assert cols["vector"].startswith("FLOAT[")  # DuckDB widens to variable list


def _stub_lance_dataset_returning(table_factory):
    """Build a fake `lance.dataset` callable that returns a stub with
    `to_table(columns=...)` yielding the provided Arrow table."""

    class _StubDataset:
        def to_table(self, columns):  # noqa: ARG002 - signature parity
            return table_factory()

    return lambda _: _StubDataset()


def test_export_to_parquet_wrong_vector_type_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Lance store with non-fixed-size-list `vector` (e.g. variable list) →
    surface a clear ValueError instead of writing a parquet the Web endpoint
    can't `array_cosine_similarity` over."""
    import pyarrow as pa

    lance_path = tmp_path / "embeddings.lance"
    lance_path.mkdir()  # exists() check passes

    def make_table():
        return pa.table(
            {
                "vacancy_id": pa.array(["hh:1"], type=pa.string()),
                "vector": pa.array([[0.1, 0.2]], type=pa.list_(pa.float32())),
            }
        )

    import lance

    monkeypatch.setattr(lance, "dataset", _stub_lance_dataset_returning(make_table))

    with pytest.raises(ValueError, match="expected fixed_size_list"):
        export_to_parquet(tmp_path / "out.parquet", lance_path)


def test_export_to_parquet_wrong_dim_raises_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Lance store with `fixed_size_list<float, N>` where N != EMBEDDING_DIM →
    ValueError so a future dim bump doesn't silently break semantic search."""
    import pyarrow as pa

    lance_path = tmp_path / "embeddings.lance"
    lance_path.mkdir()

    wrong_dim = EMBEDDING_DIM + 1

    def make_table():
        return pa.table(
            {
                "vacancy_id": pa.array(["hh:1"], type=pa.string()),
                "vector": pa.FixedSizeListArray.from_arrays(
                    pa.array([0.0] * wrong_dim, type=pa.float32()),
                    wrong_dim,
                ),
            }
        )

    import lance

    monkeypatch.setattr(lance, "dataset", _stub_lance_dataset_returning(make_table))

    with pytest.raises(ValueError, match=f"!= {EMBEDDING_DIM}"):
        export_to_parquet(tmp_path / "out.parquet", lance_path)


def test_semantic_similar_excludes_stale_embeddings(tmp_path: Path):
    """Replays the Web /api/semantic-similar SQL pattern against pure DuckDB.

    embeddings.lance can outlive slim_active.parquet — vacancies dropped by
    `publish slim --dedup` or already-closed listings still have stored
    vectors. The endpoint must filter against active *before* applying LIMIT,
    otherwise stale IDs occupy top-K slots and crowd out real matches.

    Setup:
      - 4 embeddings: hh:q (the query), hh:near (almost identical), hh:far,
        hh:stale (almost identical to query, but NOT in slim_active)
      - 3 active rows: hh:q, hh:near, hh:far
      - Limit 1 → must return hh:near, NOT hh:stale.
    """
    from src.enrich.embeddings import EMBEDDING_DIM

    lance_path = tmp_path / "embeddings.lance"
    embeddings_parquet = tmp_path / "embeddings.parquet"
    slim_parquet = tmp_path / "slim_active.parquet"

    base = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    base[0] = 1.0
    near = base.copy()
    near[1] = 0.05
    near /= np.linalg.norm(near)
    stale = base.copy()
    stale[1] = 0.04
    stale /= np.linalg.norm(stale)
    far = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    far[-1] = 1.0

    ids = ["hh:q", "hh:near", "hh:far", "hh:stale"]
    vectors = np.stack([base, near, far, stale]).astype(np.float32)
    write_lance_arrays(ids, ["x"] * 4, vectors, lance_path)
    export_to_parquet(embeddings_parquet, lance_path)

    import duckdb

    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE slim_active AS
        SELECT 'hh:q' AS vacancy_id, 'q' AS title
        UNION ALL SELECT 'hh:near', 'near'
        UNION ALL SELECT 'hh:far', 'far';
        COPY slim_active TO '{slim_parquet.as_posix()}' (FORMAT PARQUET);
        """
    )
    con.execute(
        f"""
        CREATE TABLE vacancy_embeddings AS
        SELECT vacancy_id, CAST(vector AS FLOAT[{EMBEDDING_DIM}]) AS vector
        FROM read_parquet('{embeddings_parquet.as_posix()}');
        """
    )

    rows = con.execute(
        f"""
        WITH active AS (
            SELECT vacancy_id, title FROM read_parquet('{slim_parquet.as_posix()}')
        ),
        ranked AS (
            SELECT e.vacancy_id,
                   array_cosine_similarity(
                     e.vector,
                     (SELECT vector FROM vacancy_embeddings WHERE vacancy_id = 'hh:q')
                   ) AS similarity
            FROM vacancy_embeddings e
            WHERE e.vacancy_id <> 'hh:q'
              AND e.vacancy_id IN (SELECT vacancy_id FROM active)
            ORDER BY similarity DESC
            LIMIT 1
        )
        SELECT r.vacancy_id, s.title, r.similarity
        FROM ranked r JOIN active s ON s.vacancy_id = r.vacancy_id
        ORDER BY r.similarity DESC
        """
    ).fetchall()

    assert len(rows) == 1
    vacancy_id, title, _similarity = rows[0]
    assert vacancy_id == "hh:near"
    assert title == "near"
