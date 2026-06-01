# Query encoder contract — v1

API hook для client-side query encoding в `/api/hybrid-search` и
будущих semantic endpoints. Реальный encoder (Transformers.js MiniLM
ONNX, Edge runtime, Python sidecar) — отдельный direction-required
выбор; этот контракт описывает **только wire format**, чтобы
encoder и server-side scoring могли разрабатываться независимо.

## `qv` query parameter

Опциональный параметр `?qv=<base64>` в `/api/hybrid-search`.

**Контракт:**
- `EMBEDDINGS_DIM = 768` (mpnet-base-v2). Любой client encoder
  должен возвращать вектор этой размерности — иначе нужен
  re-embedding корпуса (Phase 8 ML scope).
- Encoded value: `base64(Float32Array(EMBEDDINGS_DIM))`.
  Little-endian, 4 байта на float, total 3072 байта → ~4096 char base64.
- L2-нормализация делается на сервере (так что client может слать как
  есть).

## Поведение `/api/hybrid-search` с `qv`

| BM25 hits | qv передан | Behaviour |
|---|---|---|
| ≥1 | нет | pseudo-relevance feedback (текущий default) |
| ≥1 | есть | **qv заменяет seed centroid** → cosine rerank vs qv |
| 0 | нет | пустой результат (BM25-only zero hits, как раньше) |
| 0 | есть | **pure cosine search** над всем корпусом, BM25 = null в response |

Response добавляет `used_client_query_vector: true` для pure-cosine
fallback. Поле `seed_count: 0` когда qv использован.

## Encoder options (future direction)

1. **Transformers.js + Xenova/paraphrase-multilingual-mpnet-base-v2**
   - Плюс: тот же mpnet 768d, корпус НЕ нуждается в re-embedding.
   - Минус: ~480 MB ONNX (200 MB quantized) браузер download —
     первая загрузка медленная, но cache.
2. **Transformers.js + MiniLM-L12-v2 multilingual (384d)**
   - Плюс: ~60 MB quantized, fast.
   - Минус: re-embedding корпуса под 384d (Phase 8 ML compute).
3. **Vercel Edge Runtime + onnxruntime-web**
   - Server-side mpnet, browser shipping не нужен. Но Edge ≠ Node,
     DuckDB не работает на Edge → split: encode на Edge, score на
     Node. Сложнее.
4. **Python FastAPI sidecar (Render/Fly.io)**
   - Чисто mpnet. Cross-vendor, нужна card.
5. **External API (OpenAI text-embedding-3-small / Cohere)**
   - 0 maintenance. Cross-API fees.

## Текущий статус

- ✅ Server-side hook готов (commit b6ad0538 +): `qv` параметр
  принимается, validate dim+finite, normalize, использует как
  centroid OR pure-cosine fallback.
- ❌ Client encoder не подключён — для активации нужно либо
  выбрать option 1-5 + установить deps + написать encoding hook
  в `web/app/page.tsx` который populate `qv` перед fetch.

Без client encoder UI поведение не меняется (BM25-only когда
embeddings.parquet не опубликован, pseudo-relevance + BM25 когда
опубликован). qv — opt-in через external клиент или curl.
