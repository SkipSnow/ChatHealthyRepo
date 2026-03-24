# Embedding Design — 2026-03-24

## Context

ChatHealthy FindCare — healthcare provider search index.
~8.4M provider records from the NPPES NPI Registry (CMS), enriched with county FIPS codes
by the FindCare DataPipelines county enrichment job.

Target store: MongoDB Atlas Vector Search.

Primary users: network managers and healthcare administrators.

## Implementation

- `Code/DataPipelines/provider_embedding.py` — `should_embed()`, `project()`, `render()`
- `Code/DataPipelines/embedding_worker.py` — `EmbeddingWorker`, retry/backoff, version stamping
- `Code/DataPipelines/tests/test_provider_embedding.py` — 41 unit tests, all passing
- `Code/Schemas/ChatHealthy.Providers.json` — source of truth for the provider record schema

---

## Record Filter — What to Embed

Embed all records **except**:

| Exclusion criterion | Field | Reason |
|---|---|---|
| No address at all | `county.reason = 'no_address'` | Nothing to anchor a location query |
| Inconsistent address | `county.reason = 'zip_state_mismatch'` | ZIP and state disagree — address is untrustworthy |

All other records embed regardless of county enrichment status.
County is one attribute among many — not a gate for inclusion.

**Included record types:**

| county.source / reason | Notes |
|---|---|
| `crosswalk_pass1` | Full geo, highest confidence |
| `geocoder_pass2_batch` | Full geo, Census Geocoder |
| `geocoder_pass3_billing` | Full geo, billing address fallback |
| `geocoder_pass4_maps` | Full geo, Google Maps |
| `geocoder_pass5_maps_billing` | Full geo, Google Maps billing fallback |
| `geocoder_failed` | Valid US address — county just couldn't be resolved |
| `out_of_scope / deactivated` | Valid address — NPI is inactive but record is searchable |
| `out_of_scope / foreign_provider` | Valid address — non-US provider |
| `out_of_scope / legacy` | Valid address — reason predates reason field |

---

## Field Projection

Embed **all content fields** except four system/pipeline internals:

| Excluded field | Reason |
|---|---|
| `_id` | MongoDB ObjectId — system |
| `load_id` | Pipeline run identifier — system |
| `record_id` | Worker sequence number — system |
| `worker_id` | Worker index — system |

---

## Derived Fields

Computed by `project()` before rendering. Not stored in the provider record.

| Field | Values | Description |
|---|---|---|
| `display_name` | string | Individual: `[prefix] first [middle] last [suffix], credential`. Org: legal business name. |
| `entity_type_label` | `individual` / `organization` | Human label for `entity_type_code`. |
| `primary_taxonomy_code` | string | Taxonomy code where `primary: true`. |
| `all_taxonomy_codes` | space-joined string | All taxonomy codes on the record. |
| `location_display` (practice) | string | `line1[, line2], city, state zip` |
| `mailing_location_display` | string / omitted | Same format. Omitted if identical to practice address. |
| `county_resolution_status` | `resolved` / `unresolved` / `excluded` | Whether `county.fips` was set. |
| `county_trust_tier` | `high` / `medium` / `low` / `excluded` | crosswalk = high; geocoder/maps = medium; geocoder_failed = low; out_of_scope = excluded. |
| `county_source_summary` | string | Human-readable enrichment method (e.g. "ZIP crosswalk (res_ratio: 0.99)"). |
| `npi_status` | `active` / `inactive` | Inactive when `npi_deactivation_date` set and `npi_reactivation_date` absent. |
| `foreign_provider_flag` | `yes` / `no` | `yes` when `practice_address.country` is not `US`. |
| `sole_proprietor_flag` | `yes` / `no` | Mapped from `is_sole_proprietor` Y/N. |
| `organization_subpart_flag` | `yes` / `no` | Mapped from `is_organization_subpart` Y/N. |
| `authorized_official` | string | Assembled: `first [middle] last[, credential] [— title]`. |
| `licenses` | string | Comma-joined: `GA #G12345, FL #F67890`. |
| `other_identifiers` | string | Semicolon-joined with type/state/issuer context. |

---

## Embedding Text Templates

Stable field order. Absent fields omitted entirely. No blank lines. No raw JSON.
Placeholder values (`<UNAVAIL>`) stripped before rendering.

### Individual (entity_type_code = '1')

```
record_type: provider
entity_type: individual
display_name: Dr. Jane A. Smith, M.D.
npi: 1144247073
npi_status: active
credential: M.D.
sex: F
sole_proprietor: no
primary_taxonomy_code: 207R00000X
all_taxonomy_codes: 207R00000X 207RC0000X
practice_address: 123 Peachtree St, Suite 400, Atlanta, GA 30301
mailing_address: PO Box 999, Atlanta, GA 30302
county_name: Fulton County
county_fips: 13121
county_resolution_status: resolved
county_trust_tier: high
county_source: ZIP crosswalk (res_ratio: 0.99)
foreign_provider: no
licenses: GA #G12345, FL #F67890
other_identifiers: G123 (04, GA); M456 (05, GA)
enumeration_date: 07/16/2006
last_update_date: 01/15/2024
certification_date: 01/15/2024
```

### Organization (entity_type_code = '2')

```
record_type: provider
entity_type: organization
display_name: MAYO CLINIC
organization_name: MAYO CLINIC
other_organization_name: Mayo Foundation
other_organization_name_type: 5
npi: 1215954557
npi_status: active
ein: 41-6011702
is_organization_subpart: yes
parent_organization_name: Mayo Foundation for Medical Education and Research
parent_organization_tin: 41-6011702
authorized_official: Robert T. Johnson, M.D. — CEO
primary_taxonomy_code: 282N00000X
all_taxonomy_codes: 282N00000X
practice_address: 200 First St SW, Rochester, MN 55905
county_name: Olmsted County
county_fips: 27109
county_resolution_status: resolved
county_trust_tier: medium
county_source: Census geocoder — practice address
foreign_provider: no
licenses: MN #MN001
enumeration_date: 04/01/2005
last_update_date: 03/01/2024
```

### Unresolved county (geocoder_failed)

```
record_type: provider
entity_type: individual
display_name: Carlos Rivera, D.O.
npi: 1234567890
npi_status: active
credential: D.O.
primary_taxonomy_code: 207P00000X
all_taxonomy_codes: 207P00000X
practice_address: 456 Rural Rd, Smalltown, TX 79901
county_resolution_status: unresolved
county_trust_tier: low
county_source: unresolved — all passes exhausted
foreign_provider: no
```

### Foreign provider (out_of_scope / foreign_provider — still embeds)

```
record_type: provider
entity_type: individual
display_name: Pierre Dubois
npi: 1987654321
npi_status: active
primary_taxonomy_code: 207R00000X
all_taxonomy_codes: 207R00000X
practice_address: 12 Rue de Paris, Paris, FR 75001
county_resolution_status: excluded
county_trust_tier: excluded
county_source: excluded (foreign_provider)
county_reason: foreign_provider
foreign_provider: yes
```

---

## Versioning

Each embedded document is stamped with:

| Field | Example | Description |
|---|---|---|
| `embedding_version` | `"0.1"` | Projection + model version. Bump when either changes. |
| `embedding_model` | `"text-embedding-3-large"` | Exact model used to generate the embedding. |

**Idempotency rule:** a document is skipped if `embedding_version` already matches the requested version. Re-embedding requires either a version bump or an explicit version override in the API payload.

**Backfill:** the `StampEmbeddingVersion` Router task backfills `embedding_version` and `embedding_model` onto records embedded before version stamping was introduced. Idempotent — skips already-stamped records.

**Version history:**

| Version | Model | Date | Notes |
|---|---|---|---|
| `0.1` | `text-embedding-3-large` | 2026-03-24 | Initial alpha — DE+MS test run |

---

## Concurrency and Rate Limiting

### First run post-mortem (2026-03-24)

The first embedding run (DE+MS, instance `ebc366fa`) stalled at ~32% due to OpenAI TPM exhaustion.

**Root cause:** 32 workers fanned out simultaneously, each sending 500-doc batches (~60–80K tokens each). This exhausted the 1M TPM allowance almost instantly. Workers that fired first embedded ~1,500 records each; workers that fired last embedded near zero.

**Aggravating factor:** `_pipeline_resume()` was called on any batch exception including 429, silently skipping rate-limited batches instead of retrying them.

### Fixes implemented

| Fix | Implementation |
|---|---|
| Retry on 429 | `_pipeline_process` catches `openai.RateLimitError`, retries up to 5× with exponential backoff. Honors `Retry-After` header when present. |
| Cursor never advances on 429 | Retry loop wraps only the API call. `_pipeline_resume()` (skip) is only reached after retry exhaustion. |
| Single `num_workers` parameter | Load fan-out and embedding fan-out both use `num_workers`. Rate control is via batch size and startup jitter — not worker count. |
| Smaller batch size | `embed_batch_size` default reduced from 500 → 100 docs/batch. |
| Startup jitter | Each worker sleeps a random `[0, embed_initial_jitter]` seconds before first API call (default 5s). Prevents synchronized burst at t=0. |

### API parameters (embedding step)

| Parameter | Default | Description |
|---|---|---|
| `num_workers` | `32` | Number of concurrent workers (shared with load step) |
| `embed_batch_size` | `100` | Documents per OpenAI batch |
| `embed_model` | `"text-embedding-3-large"` | Must be in `SUPPORTED_EMBED_MODELS` — abends on unsupported value |
| `embed_initial_jitter` | `5.0` | Max startup delay in seconds per worker |

### Model validation

`SUPPORTED_EMBED_MODELS` in `embedding_worker.py` maps model name → vector dimensions. Passing an unsupported model raises `ValueError` immediately in `__init__` — the activity fails before touching OpenAI or MongoDB. New models must be added to the whitelist explicitly after end-to-end validation.

### Token usage tracking

Each worker accumulates `response.usage.total_tokens` per batch and returns `total_tokens` in its result. The orchestrator aggregates across all workers and includes `total_tokens` in the pipeline result. Useful for cost tracking and model comparison.

---

## Non-Goals (Alpha/MVP)

- No multiple embeddings (one projection, all users)
- No consumer vs. enterprise split
- No raw JSON embedding
- `authorized_official_telephone_number` excluded — contact info, not a search signal
- No TPM-aware governor (future: estimate tokens/batch, throttle dynamically)

---

## Embedding Model Decision — How We Got There

### GPT's initial proposal

GPT recommended `text-embedding-3-large` (OpenAI).

- 3072 dimensions
- $0.13/1M tokens
- Expected cost: ~$130–200 one-time
- Reasoning: well-documented, predictable under load, known OpenAI + Atlas path,
  no meaningful performance penalty vs smaller models for this use case.
- GPT noted that embedding latency is governed by batching and parallelization,
  not model size — so large vs small is a cost/quality tradeoff, not a speed one.

### Claude's counter-proposal

Claude proposed `voyage-3-large` (Voyage AI) as an alternative.

- 1024 dimensions
- $0.06/1M tokens
- Expected cost: ~$55–85 one-time
- Reasoning:
  - Purpose-built for retrieval — Voyage was founded specifically for RAG and search.
  - Anthropic uses Voyage internally for Claude's own RAG infrastructure.
  - 3× smaller index (~34 GB vs ~103 GB in Atlas Vector Search) — faster queries,
    lower Atlas tier cost, easier to rebuild on re-embed.
  - Consistently benchmarks above OpenAI on BEIR retrieval tasks.
  - 54% cheaper for a model that retrieval benchmarks favor.

### The decision

**`text-embedding-3-large`. Locked for alpha.**

Skip reviewed both proposals and decided in favor of GPT's recommendation.
Rationale:

1. **Shipping is the primary risk, not optimization.** The goal is a working
   end-to-end system with stable retrieval. Marginal benchmark gains are not
   the priority at this stage.

2. **Voyage introduces a new vendor.** New API surface, new authentication,
   new rate limiting behavior, new retry/error handling paths. Every new
   dependency multiplies debugging complexity in alpha.

3. **OpenAI + Atlas is a known, documented path.** This combination is widely
   used and predictable under load. Voyage + Atlas is viable but not the default.

4. **BEIR benchmarks do not reflect our workload.** Our queries are mixed:
   exact NPI lookup, name lookup, taxonomy lookup, geo + specialty queries,
   structured entity retrieval. Benchmark rankings on general retrieval corpora
   don't directly translate.

5. **Voyage is a valid v2 conversation.** Once a working baseline exists,
   re-embedding with Voyage can be evaluated against real query performance.
   The projection and pipeline are model-agnostic — swapping the model later
   is a one-line change plus a re-embed run.

### Implementation

- `EMBED_MODEL = "text-embedding-3-large"`, `EMBED_VERSION = "0.1"` in `embedding_worker.py`
- `SUPPORTED_EMBED_MODELS = {"text-embedding-3-large": 3072}` — whitelist, abend on unsupported
- `numDimensions = 3072` in `provider_load_manager.py`
- First run: DE+MS test, instance `ebc366fa`, 2026-03-24. Partial (~32%) due to TPM exhaustion — see post-mortem above.
