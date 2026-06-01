# Embeddings export contract — v1

Path on Vercel Blob: `agg/embeddings.parquet`

Produced by: `python -m src.cli publish embeddings` (reads
`master/embeddings.lance`, calls `src/publish/embeddings_export.export_to_parquet`,
uploads via `src/publish/blob_push.upload_file`).

Consumed by: `web/app/api/semantic-similar/route.ts` via DuckDB `httpfs`,
materialised lazily into the in-memory table `vacancy_embeddings`
(see `web/lib/duckdb.ts::ensureEmbeddingsTable`, TTL 24h).

## Schema

| column | type | semantics |
|---|---|---|
| `vacancy_id` | `VARCHAR` (NOT NULL) | matches `slim_active.vacancy_id`; format `<source>:<id>` |
| `vector` | `fixed_size_list<float, 768>` (NOT NULL) | L2-normalized mpnet embedding |

Compression: `zstd`. One row per vacancy embedded by `vradar enrich embeddings`.
Vectors are normalized at encode time (`SentenceTransformer.encode(...,
normalize_embeddings=True)`), so `dot(a, b) == cosine(a, b)` and
`array_cosine_similarity` returns the dot product directly.

## DuckDB read quirk

DuckDB surfaces Arrow `fixed_size_list<float, 768>` as variable-length
`FLOAT[]` after `read_parquet`. `array_cosine_similarity` requires the
fixed-size form, so consumers must `CAST(vector AS FLOAT[768])` (the Web
endpoint does this once when materialising `vacancy_embeddings`). Locked by
`tests/unit/test_embeddings_export.py::test_export_to_parquet_supports_duckdb_array_cosine_similarity`.

## Update cadence

Recompute is incremental (`text_hash` skip) — `vradar enrich embeddings`
re-encodes only vacancies with a new title+description hash. After encode,
`vradar publish embeddings` overwrites the Blob file in place.

The Web TTL is 24h, so a fresh upload is picked up within one TTL cycle
without a redeploy.
