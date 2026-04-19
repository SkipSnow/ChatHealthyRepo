# Pipeline Architecture v4 -- Bulletproof ETL

**Design Document**

| Field | Value |
|-------|-------|
| Version | 4 |
| Date | 2026-04-08 |
| Status | Designed |
| Created By | GPT-4.1 (Architect) + GPT-4.1 (Reviewer), 11 iterations |
| Audit Source | EXT-AUDIT-003 (Perplexity Pipeline v4 Design Audit) |
| Epic | PIPE-BP-001 |
| Priority | Reliability > Speed > Cost |
| Target | 95% successful end-to-end runs when source files exist |

---

## Table of Contents

1. [human Requirements](#human-requirements)
2. [v3 Decisions Retained](#v3-decisions-retained)
3. [Provider Pipeline](#provider-pipeline)
4. [Prescriber Pipeline](#prescriber-pipeline)
5. [New Components](#new-components)
6. [v4 Remediations](#v4-remediations)
7. [Execution Rules](#execution-rules)
8. [Vector Search Architecture](#vector-search-architecture)
9. [Storage Architecture](#storage-architecture)
10. [Implementation Order](#implementation-order)

---

## Human Requirements

- **Target:** Bulletproof ETL -- 95% successful end-to-end runs when source files exist
- **Fatal Alert:** Ring bell continuously every 5 seconds on human's computer for fatal errors
- **Priority:** Reliability > Speed > Cost

---

## v3 Decisions Retained

These architectural decisions from v3 carry forward unchanged:

1. **Parquet canonical intermediate format** -- ADLS Gen2, pyarrow, zstd compression
2. **Dual logging** -- ADLS Gen2 append blobs + MongoDB checkpoints
3. **Azure Durable Functions orchestration** -- single `/api/Router` dispatch
4. **DataFetcherBase** -- ETag/SHA-256 skip guard, 8MB stream chunks, blob caching
5. **PipelineWorkerBase** -- Gang of Four Template Method pattern
6. **Staged collection pattern** for Ship (CopyToFrontEnd)
7. **Circuit breakers, dead-letter queues, rate limiters**
8. **ClusterLifecycleManager** -- pause/resume pipeline cluster for cost savings
9. **Chunk-level state machine** -- pending -> in_progress -> complete -> verified | failed
10. **Environment isolation** -- dev_ / qa_ / prod_ prefix routing

---

## Provider Pipeline

**Data Source:** NPPES (CMS) -- 10.6GB ZIP, all US healthcare providers

### Stage 1: Assemble

| Step | Name | Class | Notes |
|------|------|-------|-------|
| 0 | Reserve cluster | `ClusterLifecycleManager` | `reserve()` wakes pipeline DB |
| 1 | Download NPI ZIP | `NppesFetcher` (extends `DataFetcherBase`) | Auto-discovers URL from CMS page. ETag/SHA-256 skip guard. 8MB stream chunks to Azure Blob. |
| 2 | Extract CSV to blob | -- | Skip guard if CSV already exists in blob |
| 3 | Compute byte-aligned partitions | -- | Newline-aligned boundaries from blob byte ranges, O(1) memory (DR-008) |

### Stage 2: Process

| Step | Name | Class | Notes |
|------|------|-------|-------|
| 4 | Drain staging collection | -- | Drop unless `incremental=true` (BUG-PIPE-003) |
| 5 | Pre-load indexes | -- | Incremental mode only |
| 6 | Write worker metadata | -- | One record per worker in `admin.DataLoadMetadata` |
| 6.5 | Pre-warm Azure Flex instances | -- | Set `always_ready = num_workers` |
| 7 | Fan-out load | `ProviderWorker` (extends `PipelineWorkerBase`) | 32 workers load partitions simultaneously via `task_all` |
| 7.5 | Reset always_ready | -- | Set `always_ready = 0` |
| 8-9 | Build indexes + reconcile counts | -- | Parallel: post-load indexes + source vs loaded count verification |

### Stage 3: Enrich

**County Enrichment** -- 6-pass cascading resolution via `CountyEnrichmentJob`

| Pass | Method | Notes |
|------|--------|-------|
| 1 | ZIP bulk crosswalk | `updateMany` per ZIP where `res_ratio >= 0.98`. ~220x fewer ops than per-provider. |
| 2 | Census Geocoder (practice address) | Batch POST, 500 per batch |
| 3 | Census Geocoder (billing address) | For `geocoder_failed` from pass 2 |
| 4 | Google Maps (practice address) | Paid API, optional |
| 5 | Google Maps (billing address) | Paid API, optional |
| 6 | NPPES public registry lookup | Free, 5 req/s rate limit |

### Stage 4: Embed

| Field | Value |
|-------|-------|
| Class | `EmbeddingWorker` (extends `PipelineWorkerBase`) |
| Model | `text-embedding-3-large` (3072 dimensions) |
| Batch size | 100 |
| Rate limiting | Exponential backoff with jitter, MAX_RETRIES = 5, respects Retry-After header |
| Pre-filter | Excludes `no_address`, `zip_state_mismatch`, `bad_data.flagged`, `out_of_scope.flagged` |
| Rule | DR-022: Embed on pipeline cluster only, never frontend |

### Stage 5: Ship

**CopyToFrontEnd** via `CopyToFrontEndManager`

| Step | Name | Notes |
|------|------|-------|
| 1 | Copy static collections | `SpecialtyMetaData`, `provider_quality`, `ICD10Codes`, `ZipCountyCrosswalk`, `drug_crosswalk_cache` |
| 2 | Copy providers (filtered by states) | Partition via `$bucketAuto` (DR-008). Copy by `_id` range in 10K batches. `insert_many(ordered=False)`. |
| 3 | Verify parity | Source count == destination count per collection and per state |
| 4 | Create vector indexes | `provider_vector_index` (3072d cosine) + `specialty_vector_index`. DR-016: verified on every promotion. |
| 5 | Verify frontend indexes | Confirm index health and query latency |

**Post-pipeline:**

| Step | Name |
|------|------|
| 10 | Write discrepancy report + email |
| 11 | Release cluster reservation (`ClusterLifecycleManager.release()`) |

---

## Prescriber Pipeline

**Data Sources:**
- CMS Part D Prescriber PUF (3.7GB CSV)
- OIG LEIE (excluded providers)
- SAM.gov (excluded entities)
- RxNorm API (drug-molecule-indication mapping)

### Stages

| Stage | Name | Class | Notes |
|-------|------|-------|-------|
| 0 | Validate | -- | Check provider counts exist |
| 1 | Fetch | `PrescriberDataFetcher` (extends `DataFetcherBase`) | Downloads CMS Part D + OIG + SAM via `fetch_all()` |
| 2 | Load | `PrescriberLoadWorker` (extends `PipelineWorkerBase`) | Parse CSV, filter by state, build `provider_quality` collection |
| 3 | Crosswalk enrichment | `CrosswalkBuilder` | `enrich_providers_with_crosswalk()` -- RxNorm -> molecule -> indication -> ICD-10 |
| 4 | Exclusion flags | `PrescriberEnrichmentJob` | `enrich_all()` -- OIG/SAM exclusion flag enrichment |
| 5 | Specialty normalization | `CrosswalkBuilder` | `compute_specialty_baselines()` -- peer benchmarks, 5% deviation bands |
| 6 | Embed | -- | Drug/molecule vector search embeddings |

---

## New Components

### QualityGate

**File:** `Code/DataPipelines/pipeline/quality_gate.py`

Data validation inserted between every pipeline stage. Fail fast with clear error. Config-driven per stage. Override via documented flag.

```
class QualityGate:
    validate_schema()       # Column names/types match expected fingerprint
    check_nulls()           # Required fields have values
    check_ranges()          # Numeric fields within expected bounds
    enforce_on_stage(name)  # Run all checks, abort pipeline on failure
```

### SchemaDriftDetector

**File:** `Code/DataPipelines/pipeline/schema_drift_detector.py`

Detects schema changes between pipeline runs using SHA-256 fingerprints of normalized column names + types, stored in `admin.SchemaFingerprints`. Aborts pipeline on mismatch. Override for hotfixes only.

```
class SchemaDriftDetector:
    detect_and_alert()  # Compare incoming schema to stored fingerprint
```

### FatalAlertBridge

**File:** `Code/DataPipelines/pipeline/fatal_alert_bridge.py`

Fatal error notification per v4-008. Bell rings every 5 seconds until acknowledged.

```
class FatalAlertBridge:
    send_alert(error, context)
    # Local:  PowerShell beep loop ([console]::beep(800,600))
    # Cloud:  Pushover webhook notification
    # Logged: admin.BellEvents collection
```

### IdempotencyManager (enhancement to PipelineWorkerBase)

**Enhanced in:** `Code/DataPipelines/pipeline_worker_base.py`

New method `output_exists_and_valid()` checks output file/collection existence + hash before executing. Deterministic upsert keys. Conditional blob upload. Enables resume from last successful checkpoint without re-running completed steps.

### TraceabilityMatrixManager

**File:** `Code/DataPipelines/scripts/update_traceability_matrix.py`

Addresses the audit's most important missed requirement (v4-007/v4-023).

```
class TraceabilityMatrixManager:
    update_on_change()          # Detects requirement/story/test splits, merges, deprecations
    sync_status_with_tests()    # Propagates pytest/Playwright results to requirement status
    resolve_test_mapping()      # UUID-based mapping survives file renames/refactors
    audit_trail()               # Logs requirement, test, result, release decision, user, timestamp
```

**Enforcement:** CI on PR + nightly via `traceability-enforce.yml`
**Storage:** `brain/machine_artifacts/content/traceability_matrix.json`

### CiCdConflictDetector

**File:** `Code/DataPipelines/scripts/ci_cd_conflict_detector.py`

Detects enforcement logic drift between GitHub Actions and Azure DevOps. Blocks deploy on conflict. Notifies legacy Azure DevOps users.

```
class CiCdConflictDetector:
    compare_enforcement_logic()  # Diff enforcement rules across both systems
    notify_legacy_users()        # Slack/email on enforcement change
    detect_drift()               # Block deploy if conflict detected
```

### ApiTestUpdater

**File:** `Code/DataPipelines/scripts/update_api_tests.py`

Auto-updates `api-tests.http` after successful deploy (v4-002). Only human status APIs via OpenAPI tags. Instance ID from Azure Functions environment. CI blocks PRs with non-status APIs.

```
class ApiTestUpdater:
    filter_status_apis()      # OpenAPI tag allowlist
    capture_instance_id()     # From Azure Functions env
    update_api_tests_file()   # Post-deploy only
```

### ManifestManager

**File:** `Code/DataPipelines/scripts/update_manifest.py`

Includes MongoDB collections in project manifest (DR-021). Auto-detects collections and adds `mongodb_collections` section.

```
class ManifestManager:
    add_mongodb_collections()  # Scan and add all active collections
    validate_manifest()        # Verify completeness
```

---

## v4 Remediations

### overnight_pipeline.py

| Issue | Fix |
|-------|-----|
| Raw `requests.get` (v4-001D violation) | Replace with `DataFetcherBase` subclass calls |
| `.readall()` in 3 files (v4-001D violation) | Replace with streaming `download_blob().chunks()`, O(1) memory |
| Hardcoded URLs in `provider_load_manager.py` | Move to env vars / config with discovery fallback |
| Runs on local machine (v4-001C violation) | Becomes Durable Functions orchestrator. CI gate rejects `if __name__`. Health check verifies `WEBSITE_SITE_NAME`. |

### CopyToFrontEnd (BUG-LOAD-001)

| Issue | Fix |
|-------|-----|
| Delaware: 2,843 of 25,591 providers copied | Atomic staged copy with metadata sidecar |
| No rollback on partial failure | Count+hash parity verification before swap |
| Silent partial copy | Atomic swap only after full parity. Rollback to previous on failure. |

---

## Execution Rules

| Rule | Description |
|------|-------------|
| **v4-001A** | `deploy-pipelines.yml` only triggers on `Code/DataPipelines/**` changes |
| **v4-001B** | Never commit/deploy DataPipelines while Azure job running |
| **v4-001C** | All workloads on Azure Functions, never local machine |
| **v4-001D** | All downloads through `DataFetcherBase`. Never raw HTTP. Stream and chunk. Never load full file in memory. |
| **v4-001E** | Sequential stages: Assemble -> Process -> Enrich -> Embed -> Ship. Each completes before next begins. Parallel sub-steps within stages allowed. |

---

## Vector Search Architecture

| Index | Collection | Dimensions | Similarity | Filter Fields |
|-------|-----------|------------|------------|---------------|
| `provider_vector_index` | `providers` | 3072 | cosine | `practice_address.state`, `.city`, `.zip`, `county.name`, `county.fips` |
| `specialty_vector_index` | `SpecialtyMetaData` | 3072 | cosine | -- |

**Placement:** Vector indexes ONLY on frontend cluster. Pipeline cluster holds raw vectors, no indexes. (v3 iteration 1 decision)

**Scaling guardrails:**
- Max 2M providers at 3072d vectors
- RAM threshold: 80% triggers architecture review for external vector DB
- State-based sharding plan if provider count exceeds 2M

---

## Storage Architecture

| Layer | Technology | Role |
|-------|-----------|------|
| **Parquet** | ADLS Gen2 (`adls2://pipeline-data/parquet/`) | Immutable audit source. pyarrow, zstd, 128MB row groups. Partitioned by state/year. Cold-start recovery to MongoDB. |
| **Azure Blob** | Azure Blob Storage | Intermediate file storage (downloaded ZIPs, extracted CSVs) |
| **MongoDB Pipeline** | Atlas `ChatHealthyDataPipelines` cluster | Operational mutable state during execution. DR-020: ZERO admin state. Paused when idle. |
| **MongoDB Frontend** | Atlas `ChatHealthyFrontEnd` cluster | Production serving. All admin state. All vector indexes. 24/7 uptime. |

---

## Implementation Order

| Phase | Name | Issues Addressed | Description |
|-------|------|------------------|-------------|
| 1 | Orchestrator Cloud Wiring | 1, 11 | Wire prescriber orchestrator + overnight pipeline to Azure Functions |
| 2 | Data Fetcher and Streaming | 5, 6 | Replace raw HTTP and `.readall()` with `DataFetcherBase` streaming |
| 3 | Configurable URLs | 7 | Move hardcoded URLs to config/env |
| 4 | Quality Gates and Idempotency | 2, 3 | Add `QualityGate` between stages, idempotency in `PipelineWorkerBase` |
| 5 | Schema Drift Detection | 9 | `SchemaDriftDetector` with SHA-256 fingerprints |
| 6 | CopyToFrontEnd Parity | 4 | Fix BUG-LOAD-001 with atomic staged copy and parity verification |
| 7 | Compliance Test Fixes | 8 | Auto-resolves when phases 2-3 and 6 complete |
| 8 | Fatal Bell Bridge | 10 | `FatalAlertBridge` with PowerShell beep and Pushover webhook |
| 9 | 95% Success Metric | -- | Rolling success-rate computation and dashboard |
| 10 | Traceability and CI/CD | -- | `TraceabilityMatrixManager`, `CiCdConflictDetector`, `ApiTestUpdater`, `ManifestManager` |

---

*Pipeline Architecture v4. Designed 2026-04-08 by GPT-4.1 (Architect) x GPT-4.1 (Reviewer). 11 iterations, $2.18 total cost. Audit: EXT-AUDIT-003.*
