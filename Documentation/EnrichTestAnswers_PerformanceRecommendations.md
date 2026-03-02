# EnrichTestAnswers Performance Analysis & Recommendations

## Bottleneck Analysis

| Bottleneck | Impact | Notes |
|------------|--------|-------|
| **`provider_collection.count_documents(query)`** | **HIGH** | Called **once per record** (~3,000 times). Each is a round-trip to MongoDB Atlas. Complex `$or`/`$regex` queries can be 50–500ms each. **Total: 2–25 minutes.** |
| **spaCy `en_core_web_lg`** | **MEDIUM** | Large model (~500MB+). CPU-only (no GPU). 3–5× slower than `en_core_web_sm`. Already using `nlp.pipe()` batch processing. |
| **Network latency** | **MEDIUM** | Atlas is remote. 3,000 sequential operations = 3,000 round-trips. |
| **Regex on MongoDB** | **MEDIUM** | `$regex` in provider queries may not use indexes well. |

## Optimization Strategies (Ordered by Impact)

### 1. Query Result Caching (Highest impact, low effort)
**Many questions share the same (geography, location, codes) → same MongoDB query.**
- Cache `count_documents()` results by a hashable query key.
- Expected reduction: 3,000 queries → ~100–500 unique queries.
- **Speedup: 5–30×** for the MongoDB phase.

### 2. Parallel MongoDB Counts (High impact, medium effort)
- Use `ThreadPoolExecutor` to run `count_documents()` calls in parallel (e.g. 10–20 workers).
- PyMongo is thread-safe for concurrent reads.
- **Speedup: 5–15×** for the MongoDB phase.

### 3. Lighter spaCy Model (Medium impact, trivial)
- Switch `en_core_web_lg` → `en_core_web_sm` (3–5× faster, slightly lower NER accuracy).
- Or `en_core_web_md` for a balance.

### 4. Increase spaCy Pipe Batch Size
- `nlp.pipe(questions, batch_size=50)` → try 100 or 200.
- Reduces Python overhead per batch.

### 5. GPU for spaCy (Conditional)
- **Standard models** (`en_core_web_sm/md/lg`): CPU-only, no GPU support.
- **Transformer model** (`en_core_web_trf`): Can use GPU via `spacy[transformers]`.
  - On CPU: Much slower than statistical models.
  - On GPU (CUDA): 2–5× faster than CPU for transformer.
- Install: `pip install "spacy[transformers]"` then `python -m spacy download en_core_web_trf`
- Requires: NVIDIA GPU + CUDA.

### 6. Pre-compute Provider Counts (High impact, high effort)
- Build a materialized collection: `(state, county, city, zip, taxonomy_codes) → count`.
- ETL job pre-computes counts; enrichment does lookup instead of `count_documents`.
- **Speedup: 100×+** for provider lookups, but requires pipeline changes.

### 7. MongoDB Indexes
- Add compound indexes on provider fields used in queries:
  - `Provider Business Practice Location Address State Name`
  - `Provider Business Practice Location Address City Name`
  - `Provider Business Practice Location Address Postal Code`
  - `Healthcare Provider Taxonomy Code_1`
- Helps both State and exact-match City queries; regex still limited.

---

## Deployment Options for GPU / Parallelization

| Platform | GPU | Use Case |
|----------|-----|----------|
| **Google Colab** | Free T4 | Quick runs, `en_core_web_trf` + GPU |
| **AWS EC2 g4dn.xlarge** | T4 GPU | Production, CUDA for spaCy transformers |
| **AWS SageMaker** | Configurable | Managed notebooks with GPU |
| **Azure ML / Vertex AI** | Configurable | Managed GPU notebooks |
| **Lambda Labs / Vast.ai** | Rent GPU | Cost-effective GPU rental |
| **Local machine** | Your GPU | `pip install spacy[transformers]`, use `en_core_web_trf` |

**Note:** For `EnrichTestAnswers`, the MongoDB counts dominate. GPU mainly helps spaCy; for maximum gain, prioritize **caching** and **parallel counts** first.

---

## Implemented (EnrichTestFile.ipynb)

- **Query caching** – Counts are cached by query key; repeated queries reuse cached result.
- **Parallel `count_documents`** – `ThreadPoolExecutor` with 16 workers; set `max_count_workers=0` for sequential.
- **Larger `nlp.pipe` batch** – Increased from 50 to 100.

## Recommended Next Steps

3. **Use `en_core_web_sm`** – Pass `spacy_model='en_core_web_sm'` for ~3× faster NER.
4. **MongoDB indexes** – Add indexes on provider address and taxonomy fields.
5. **GPU** – For `en_core_web_trf` only; requires CUDA.
