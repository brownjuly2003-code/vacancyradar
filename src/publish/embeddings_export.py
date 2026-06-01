"""Export Lance embeddings store → single Parquet file.

Web reads embeddings via DuckDB httpfs (same channel as slim_active.parquet)
because Lance has no Node bindings. Output schema:

    vacancy_id: VARCHAR
    vector:     fixed_size_list<float, 768>

Quirk: DuckDB surfaces Arrow `fixed_size_list<float, N>` as variable-length
`FLOAT[]`, not `FLOAT[N]`. `array_cosine_similarity` requires the fixed-size
form, so the Web endpoint must `CAST(vector AS FLOAT[768])` after reading
this file. See `tests/unit/test_embeddings_export.py` —
`test_export_to_parquet_supports_duckdb_array_cosine_similarity` locks that
contract.

Cosine == dot product because `embeddings.encode_texts` calls
`normalize_embeddings=True`, so all stored vectors have L2 norm == 1.
"""
from __future__ import annotations

from pathlib import Path

from src.enrich.embeddings import EMBEDDING_DIM, DEFAULT_LANCE_PATH


def export_to_parquet(
    out_path: Path,
    lance_path: Path = DEFAULT_LANCE_PATH,
) -> int:
    """Write Lance store to a single Parquet file. Returns rows written.

    Raises FileNotFoundError if the Lance store does not exist (caller decides
    whether that's a hard failure or "skip publish").
    """
    import lance
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not lance_path.exists():
        raise FileNotFoundError(lance_path)

    ds = lance.dataset(str(lance_path))
    table = ds.to_table(columns=["vacancy_id", "vector"])
    if table.num_rows == 0:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(_empty_table(), out_path, compression="zstd")
        return 0

    vector_field = table.schema.field("vector")
    if not pa.types.is_fixed_size_list(vector_field.type):
        raise ValueError(
            f"Lance vector column has unexpected type {vector_field.type}; "
            "expected fixed_size_list<float, N>"
        )
    if vector_field.type.list_size != EMBEDDING_DIM:
        raise ValueError(
            f"Lance vector dim {vector_field.type.list_size} != {EMBEDDING_DIM}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd")
    return table.num_rows


def _empty_table():
    """Empty table with the same schema as a non-empty export — keeps DuckDB
    `read_parquet` happy when there are no rows yet."""
    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("vacancy_id", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), EMBEDDING_DIM)),
        ]
    )
    return pa.table(
        {
            "vacancy_id": pa.array([], type=pa.string()),
            "vector": pa.FixedSizeListArray.from_arrays(
                pa.array([], type=pa.float32()),
                EMBEDDING_DIM,
            ),
        },
        schema=schema,
    )
