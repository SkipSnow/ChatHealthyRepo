# Code Audit: Code/DataPipelines/ vs agile_backlog.json

**Audit date:** 2026-04-10
**Auditor:** Claude Opus 4.6 (1M context)
**Branch:** dev (commit aa9f340)
**Scope:** Every source file in Code/DataPipelines/ (excluding tests/) audited against EPIC-006 requirements in agile_backlog.json.

---

## Legend

| Symbol | Meaning |
|--------|---------|
| MAPPED | Code implements one or more existing requirements |
| NEEDS-REQ | Code has a clear purpose but no matching requirement exists |
| DEAD | Code appears unused/deprecated/unreferenced |

---

## Section 1: Active Pipeline Files — Requirement Mappings

### 1.1 function_app.py (Router, Orchestrators, Activities)

**What it does:** Azure Functions entry point. Defines HTTP Router endpoint (`DevPipelineManagementService`), GPTReader endpoint, OTP exchange endpoint. Routes sync tasks (LoadSpecialtyData, PromoteData, etc.), async tasks (FindCarePipeline, LoadProviderData, CopyToFrontEnd, etc.), and ops tasks (WakeCluster, ClusterStatus, Release, ForceRelease). Includes pipeline step registry with precondition checks.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `dev_pipeline_management` (Router) | PIPE-BP-004-REQ-006 (workloads on Azure Functions), PIPE-LC-003-REQ-001 (incremental param), FC-PIPE-RENAME-001-REQ-001 (FindCarePipeline name) |
| `ASYNC_TASK_ORCHESTRATORS["FindCarePipeline"]` | FC-PIPE-RENAME-001-REQ-001 |
| `PIPELINE_STEP_REGISTRY` | PIPE-BP-004-REQ-007 (5-stage pipeline) |
| `gpt_reader_route` | NEEDS-REQ |
| `exchange_otp_route` | NEEDS-REQ |
| `OPS_TASK_HANDLERS` | PIPE-LIFECYCLE feature (cluster ops) |

### 1.2 provider_load_manager.py

**What it does:** Implements the FindCarePipeline and LoadProviderData orchestrators plus all activity functions: download NPPES zip, extract CSV, partition, fan-out load workers, reconcile, embed, create vector indexes. Contains NppesFetcher (DataFetcherBase subclass).

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `provider_load_orchestrator_fn` | PIPE-LC-005-REQ-001 (try/finally reservation release) |
| `findcare_pipeline_orchestrator_fn` | PIPE-LC-004-REQ-001 (try/finally reservation release), PIPE-LC-001-REQ-001 (state filter), PIPE-BP-004-REQ-007 (5-stage pipeline) |
| `NppesFetcher` | PIPE-BP-004-REQ-002 (DataFetcherBase), PIPE-BP-001-REQ-004 (SHA-256 validation) |
| `drain_staging_fn` | PIPE-LC-003-REQ-002 (drop unless incremental) |
| `embed_worker_fn` | PIPE-DQ-004-REQ-011 (embedding includes can_prescribe) |
| `create_vector_index_fn` | IDX-001-REQ-001, IDX-001-REQ-002 |
| `register_reservation_fn` / `release_reservation_fn` | PIPE-LC-015-REQ-001, PIPE-LC-016-REQ-001 |
| `warm_instances_fn` (pre-warm import) | NEEDS-REQ |

### 1.3 copy_to_frontend.py

**What it does:** Copies collections from pipeline cluster to frontend cluster. Implements parity verification, vector index creation (provider + specialty), snapshot, partitioned copy, migrate environment orchestrator.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `_copy_collection` + parity check | PIPE-CP-001-REQ-001, PIPE-CP-001-REQ-002 |
| `_create_frontend_vector_index` | IDX-001-REQ-001, FC-PIPE-COPY-001-REQ-002 |
| `_create_specialty_vector_index` | IDX-001-REQ-002, FC-PIPE-COPY-001-REQ-002 |
| `copy_to_frontend_orchestrator` | PIPE-LC-006-REQ-001 (try/finally reservation), FC-PIPE-COPY-001-REQ-001 |
| `snapshot_collection_fn` | PIPE-LC-008-REQ-001 (no reservation management) |
| `migrate_environment_orchestrator` | PIPE-LC-007-REQ-001 (try/finally reservation) |
| `verify_frontend_indexes` | IDX-003-REQ-001 |

### 1.4 county_enrichment_job.py

**What it does:** Six-pass county enrichment: Pass 1 (ZIP crosswalk), Pass 2 (Census geocoder), Pass 3 (billing address), Pass 4 (Google Maps), Pass 5 (Maps billing), Pass 6 (NPPES lookup). Sub-orchestrators for each pass.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `county_enrichment_orchestrator_fn` | PIPE-LC-009-REQ-001 (no reservation management) |
| `county_enrichment_pass1_orchestrator_fn` | PIPE-LC-010-REQ-001 |
| `county_enrichment_pass2_orchestrator_fn` | PIPE-LC-011-REQ-001 |
| `county_enrichment_pass3_orchestrator_fn` | PIPE-LC-012-REQ-001 |
| `county_enrichment_pass4_orchestrator_fn` | PIPE-LC-013-REQ-001 |
| `county_enrichment_pass6_nppes_orchestrator_fn` | PIPE-LC-014-REQ-001 |
| `mark_out_of_scope_fn` | PIPE-DQ-002-REQ-001 (out_of_scope flag) |
| `mark_zip_state_mismatch_fn` | PIPE-DQ-003-REQ-001 through REQ-005 (zip/state mismatch repair) |
| `enrich_by_address_batch_fn` | PIPE-DM-001-REQ-001 (in-place enrichment) |
| States filter in all passes | PIPE-LC-001-REQ-001, PIPE-LC-001-REQ-002 |

### 1.5 crosswalk_builder.py

**What it does:** Builds molecule-to-indication-to-ICD10 crosswalk from RxNorm/DrugCentral/UMLS. Caches lookups in drug_crosswalk_cache. Computes 5% bands for generic ratios.

**Status:** MAPPED (partial)

| Function/Block | Requirement(s) |
|---|---|
| Drug-to-indication mapping | PIPE-DQ-004-REQ-009 (dual-write drug data), PIPE-DQ-004-REQ-010 (drug array schema) |
| `_compute_band` | PIPE-DQ-004-REQ-014 (generic_ratio_band), PIPE-DQ-004-REQ-013 (brand/generic counts) |

### 1.6 load_specialty_data.py

**What it does:** Fetches NUCC provider taxonomy CSV from nucc.org, stores in Azure Blob, loads into MongoDB SpecialtyMetaData collection. Enriches with embeddings, can_prescribe, and homeopathic flags.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `ChatHealthyLoadSpecialtyData` | FC-PIPE-SPEC-001-REQ-001 (load SpecialtyMetaData) |
| can_prescribe/homeopathic classification | FC-PIPE-SPEC-002-REQ-001, FC-PIPE-SPEC-002-REQ-002 |

### 1.7 provider_embedding.py

**What it does:** Canonical embedding projection for provider records. Defines trust tiers from county source, should_embed filter, project function, render function.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `should_embed` | PIPE-DQ-001-REQ-003 (downstream exclusion by reason) |
| `project` / `render` | PIPE-DQ-004-REQ-011 (embedding includes can_prescribe), PIPE-DQ-004-REQ-016 (bands in embedding) |
| Trust tiers | PIPE-DM-001-REQ-001 (in-place enrichment result consumed) |

### 1.8 embedding_worker.py

**What it does:** Generates OpenAI text-embedding-3-large embeddings for provider documents. Idempotent. Rate-limit handling with exponential backoff.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| EmbeddingWorker | PIPE-BP-004-REQ-007 (Embed stage), PIPE-DQ-004-REQ-011 |

### 1.9 provider_worker.py

**What it does:** Reads byte-range slices of NPI CSV from Azure Blob, parses rows, normalizes multi-valued fields, batch-upserts to MongoDB staging. Idempotent on (load_id, record_id).

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| Full load vs incremental | PIPE-LC-003-REQ-002 (drop unless incremental), PIPE-INC-001-REQ-001 (replaceOne) |
| Idempotent upsert | PIPE-BP-001-REQ-002 (checkpoint/restart) |

### 1.10 data_fetcher_base.py

**What it does:** Base class for all data source downloaders. ETag/Last-Modified guard, SHA-256 checksums, blob storage upload, DataSourceRegistry tracking.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `DataFetcherBase` | PIPE-CO-001-REQ-001 (no raw requests.get), PIPE-BP-004-REQ-002 (all downloads use DataFetcherBase), PIPE-BP-001-REQ-004 (SHA-256 validation) |

### 1.11 blob_client.py

**What it does:** Shared BlobServiceClient singleton. Container name helpers.

**Status:** NEEDS-REQ (infrastructure utility, no explicit backlog entry)

### 1.12 pipeline_db.py

**What it does:** Shared MongoDB access singleton. ENV_PREFIX routing with CV-010 validation.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `get_db` with ENV_PREFIX | PIPE-BP-004-REQ-004 (all database names use ENV_PREFIX) |

### 1.13 pipeline_worker_base.py

**What it does:** Gang of Four Template Method base class for all pipeline row-processing workers. Owns processing loop, exception handling, row_errors accumulator, output_exists_and_valid for idempotent resume.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `pipeline_execute` | PIPE-BP-001-REQ-003 (step reports success/failure) |
| `output_exists_and_valid` | PIPE-ID-001-REQ-001, PIPE-ID-001-REQ-002, PIPE-ID-001-REQ-003 |

### 1.14 pipeline_health.py

**What it does:** MongoDB health check, SparkPost admin email notifications, Pushover push notifications.

**Status:** MAPPED (partial)

| Function/Block | Requirement(s) |
|---|---|
| `check_mongo_health` | PIPE-BP-001-REQ-001 (reliability) |
| `send_pushover` | PIPE-BP-002-REQ-001 (fatal alerting channel) |

### 1.15 quality_gate.py

**What it does:** Inter-stage data quality validation. Checks row count minimums and null fraction for required fields. Raises QualityGateFailure on failure.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `QualityGate.enforce` | PIPE-QG-001-REQ-001 through REQ-004 |
| QualityGate class | PIPE-CO-001-REQ-004 (pipeline must use QualityGate) |

### 1.16 schema_drift_detector.py

**What it does:** Detects schema changes between pipeline runs via SHA-256 fingerprinting. Stores fingerprints in admin.SchemaFingerprints. Raises SchemaDriftError on mismatch.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `SchemaDriftDetector` | PIPE-SD-001-REQ-001 through REQ-004 |

### 1.17 fatal_alert_bridge.py

**What it does:** Fatal error alerting. PowerShell bell loop every 5 seconds (local), Pushover webhook (cloud). Logs to admin.BellEvents.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `FatalAlertBridge` | PIPE-FA-001-REQ-001 through REQ-003, PIPE-BP-002-REQ-001 through REQ-004 |
| `send_alert` / `stop_bell` | PIPE-CO-001-REQ-005 (pipeline must use FatalAlertBridge) |

### 1.18 cluster_lifecycle_manager.py

**What it does:** Infrastructure operations manager. Manages MongoDB Atlas cluster lifecycle via reservation pattern. Reserve/release/force_release. Auto-pause on zero reservations. Overdue detection.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `reserve` | PIPE-LC-016-REQ-001 (no time.sleep blocking) |
| `release` | PIPE-LC-015-REQ-001 (auto-pause on zero reservations) |
| `force_release_all` | PIPE-LC-015-REQ-002 (only manager may pause) |

### 1.19 atlas_cluster_manager.py

**What it does:** Scale Atlas clusters up/down via Atlas API. CLI tool for scale-up (resume + resize to M30/M200) and scale-down (pause). Used by idle_monitor and migration scripts.

**Status:** NEEDS-REQ (infrastructure utility, no explicit backlog entry)

### 1.20 auth.py

**What it does:** Token-based authentication for Azure Functions. Validates Bearer tokens from API_TOKEN_MAP environment variable.

**Status:** NEEDS-REQ (security infrastructure, no pipeline-specific backlog entry)

### 1.21 gpt_reader.py

**What it does:** Read-only broker service for GPT. Two actions: ReadArtifact (project files) and Query (MongoDB). Authenticated, capped, audited. Requirements R1-R32 referenced in file header but not in agile_backlog.json.

**Status:** NEEDS-REQ (GPT integration service, no EPIC-006 backlog entry)

### 1.22 otp_manager.py

**What it does:** One-time password key exchange for Brain API. Human generates OTP, agent calls ExchangeOTP to get permanent Bearer key. Stored in MachineBrain.otp_tokens.

**Status:** NEEDS-REQ (agent auth infrastructure, no EPIC-006 backlog entry)

### 1.23 idle_monitor.py

**What it does:** Auto-pauses ChatHealthyDataPipelines when idle too long. Checks cluster state, queries last pipeline report, pauses after threshold hours. Sends SparkPost notification. Currently disabled (commented out import in function_app.py).

**Status:** NEEDS-REQ (cost management, no backlog entry) -- NOTE: disabled in function_app.py (line 41 comment: `# from idle_monitor import check_and_pause  # disabled`)

### 1.24 instance_warmer.py

**What it does:** Pre-warms Azure Flex Consumption instances before fan-out via Azure Management API (MSI auth). Eliminates cold-start stacking/OOM. Writes metrics to admin.WarmUpMetrics.

**Status:** NEEDS-REQ (performance optimization, no backlog entry)

### 1.25 ChatHealthyMongoUtilities.py

**What it does:** MongoDB connection manager for GUI/ConversationalUX layer. Ping-on-access health check. NOT for use in DataPipelines (file header says so explicitly).

**Status:** MAPPED (SEC-MONGO-001 in EPIC-4, ChatHealthyMongoUtilities uses SecretManager)

**NOTE:** This file explicitly says "DO NOT USE in DataPipelines." It is placed in DataPipelines directory but is a shared utility for the ConversationalUX layer. Consider moving to Code/Shared/.

### 1.26 validate_provider_load.py

**What it does:** Automated spot-check against NPPES NPI Registry API. Samples providers, compares name/address/taxonomy against public registry. Writes report to admin.ValidationReport. CLI tool.

**Status:** NEEDS-REQ (QA utility, no backlog entry)

### 1.27 zip_county_crosswalk_loader.py

**What it does:** Loads US Census ZCTA-to-county relationship data into PublicHealthData.ZipCountyCrosswalk. Uses DataFetcherBase.

**Status:** NEEDS-REQ (reference data loader, implicitly required by county_enrichment_job Pass 1 but no explicit backlog entry)

### 1.28 discrepancy_reporter.py

**What it does:** Writes pipeline run results to admin.PipelineDiscrepancyReports, sends SparkPost email notification on completion.

**Status:** NEEDS-REQ (operational reporting, no explicit backlog entry)

### 1.29 promote_data_fn.py

**What it does:** Promotes data between environments on the frontend cluster. Copies collections from {from_env} to {to_env}. Verifies frontend indexes after promotion.

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| `run_promote_data` + `verify_frontend_indexes` | IDX-003-REQ-001 (verify indexes after promotion) |

### 1.30 sync_gateway_agent.py

**What it does:** Agent-gated data promotion from pipeline to frontend. CopyToFrontEnd v2: streaming copy (500-doc batches), atomic swap, per-state split, quality gates, schema drift detection, parity verification.

**Status:** NEEDS-REQ (v2 of CopyToFrontEnd, supersedes parts of PIPE-COPY but no dedicated backlog entry)

### 1.31 ops_manager/*.py

**What it does:** Pipeline Operations Manager agent package. Contains:
- `ops_agent.py` — BaseAgent subclass, manages infrastructure lifecycle
- `cluster_tool.py` — wraps ClusterLifecycleManager as BaseTool
- `alert_tool.py` — Pushover + SparkPost with cooldown windows
- `triage_tool.py` — rule-based + GPT-4.1-mini error classification
- `evidence_package.py` — structured JSON handoff between Ops and Dev managers
- `audit_trail.py` — append-only logging to ops_audit collection

**Status:** NEEDS-REQ (operations infrastructure agent, no backlog entry)

### 1.32 load_provider_data.py

**What it does:** Placeholder file. Returns a stub response. File header says "retained as a placeholder -- not called by TASK_HANDLERS."

**Status:** DEAD -- Placeholder only. LoadProviderData is handled by provider_load_orchestrator via Durable Functions. This file is not imported by any other file.

### 1.33 icd10_loader.py

**What it does:** Downloads CMS ICD-10-CM code descriptions, upserts into MongoDB ICD10Codes collection. Uses DataFetcherBase.

**Status:** MAPPED (active -- imported in function_app.py as sync task "LoadICD10")

| Function/Block | Requirement(s) |
|---|---|
| ICD-10 data needed for prescriber drug enrichment | PIPE-DQ-004-REQ-010 (icd10_codes per drug) |

### 1.34 county_economic_enrichment.py

**What it does:** Enriches provider county data with economic indicators (GDP, income, rural-urban codes) from BEA, Census ACS, and USDA sources.

**Status:** NEEDS-REQ -- Not imported by function_app.py or any other pipeline file. Appears to be a standalone utility awaiting integration. Has a clear purpose for EvaluateCare scoring.

### 1.35 prescriber_*.py (5 files)

All prescriber files are active -- imported by function_app.py for the `PrescriberEvaluateCarePipeline` async task.

- **prescriber_data_fetcher.py:** Downloads CMS Part D, OIG LEIE, SAM.gov files
- **prescriber_load_worker.py:** Builds provider_quality from providers + CMS Part D
- **prescriber_enrichment_job.py:** Drug-indication mapping, exclusion flags, location
- **prescriber_evaluate_care_pipeline.py:** Overnight pipeline orchestration
- **prescriber_pipeline_manager.py:** Full pipeline orchestration (fetch/load/enrich/embed)

**Status:** MAPPED

| Function/Block | Requirement(s) |
|---|---|
| prescriber_data_fetcher.py | PIPE-DQ-004-REQ-009 (CMS Part D dual-write) |
| prescriber_load_worker.py | PIPE-DQ-004-REQ-010 (drug array schema), PIPE-DQ-004-REQ-013 (brand/generic counts) |
| prescriber_enrichment_job.py | PIPE-DQ-004-REQ-009, PIPE-DQ-004-REQ-010 |
| prescriber_pipeline_manager.py | PIPE-DQ-004 story (prescriber classification pipeline) |
| prescriber_evaluate_care_pipeline.py | PIPE-DQ-004 story |

---

## Section 2: Dead Code Candidates

### 2.1 load_provider_data.py -- DEAD

**Evidence:** File header explicitly says "retained as a placeholder -- not called by TASK_HANDLERS." Not imported by any file. The `run_load_provider_data` function returns a stub. All real load logic is in `provider_load_manager.py`.

**Recommendation:** Mark EXTRANEOUS or delete.

### 2.2 copy_va_incremental.py -- DEAD

**Evidence:** Not imported by function_app.py or any other file. One-time script for incrementally copying VA providers to frontend. Superseded by copy_to_frontend.py's state-filtered copy capabilities.

**Recommendation:** Mark EXTRANEOUS (one-time migration script, completed).

### 2.3 embed_va_backfill.py -- DEAD

**Evidence:** Not imported by any file. One-time backfill script for VA provider embeddings. Superseded by embedding_worker.py's standard pipeline flow.

**Recommendation:** Mark EXTRANEOUS (one-time backfill script, completed).

### 2.4 move_to_frontend.py -- DEAD

**Evidence:** Not imported by any file. One-time script to copy databases from DataPipelines cluster to FrontEnd cluster. Superseded by copy_to_frontend.py and promote_data_fn.py.

**Recommendation:** Mark EXTRANEOUS (one-time migration script, completed).

### 2.5 migrate_discrepancy_reports.py -- DEAD

**Evidence:** Not imported by any file. One-time migration from admin.PipelineDiscrepancyReport (singular) to admin.PipelineDiscrepancyReports (plural).

**Recommendation:** Mark EXTRANEOUS (one-time migration script, completed).

### 2.6 rename_to_dev.py -- DEAD

**Evidence:** Not imported by any file. One-time script to add dev_ prefix to database names on FrontEnd cluster.

**Recommendation:** Mark EXTRANEOUS (one-time migration script, completed).

### 2.7 refactor_db.py -- DEAD

**Evidence:** Not imported by any file. One-time script to copy PublicHealthData to dev_PublicHealthData. Uses `winsound` (Windows-only). Imports from 2025 copyright.

**Recommendation:** Mark EXTRANEOUS (one-time migration script, completed).

### 2.8 qa_provider_load.py -- ACTIVE (CLI tool)

**Evidence:** Not imported by function_app.py but is a standalone CLI QA tool. Has clear operational purpose (pre-deployment validation). Not dead code but lacks a requirement.

**Recommendation:** NEEDS-REQ (QA gate for provider loads).

### 2.9 county_economic_enrichment.py -- NOT DEAD but UNINTEGRATED

**Evidence:** Not imported by any pipeline code. Has a clear future purpose (EvaluateCare scoring data). Actively developed but not yet wired into the orchestrator.

**Recommendation:** NEEDS-REQ (economic enrichment for EvaluateCare).

### 2.10 idle_monitor.py -- DISABLED but NOT DEAD

**Evidence:** Import is commented out in function_app.py (line 41). Code is complete and functional. Superseded by ClusterLifecycleManager's reservation-based auto-pause.

**Recommendation:** NEEDS-REQ or mark EXTRANEOUS if ClusterLifecycleManager fully replaces it.

---

## Section 3: Requirements That Need to Be Created

The following files have clear purposes but no matching requirement in agile_backlog.json. Requirements should be created under EPIC-006.

### 3.1 GPT Reader Service (gpt_reader.py + function_app.py GPTReader route)

**Proposed story:** FC-PIPE-GPTREADER-001
**Title:** Read-only GPT broker service for pipeline data
**Description:** GPT agents can query MongoDB and read project files via authenticated, capped, audited API. Separate auth from Router.
**Requirements:**
- R1: Bearer token auth separate from Router
- R2: Only allowlisted databases accessible
- R3: Response capped at 500KB
- R4: All access read-only and audited
**pytest_ids:** `test_gpt_reader.py::test_auth_required`, `test_gpt_reader.py::test_read_only`

### 3.2 OTP Key Exchange (otp_manager.py + function_app.py ExchangeOTP route)

**Proposed story:** FC-PIPE-OTP-001
**Title:** One-time password key exchange for agent API access
**Description:** human generates OTP, agent exchanges for permanent Bearer key. OTP expires after 30 minutes, consumed on first use.
**Requirements:**
- REQ-001: Generate OTP with 30-minute TTL
- REQ-002: Exchange OTP for permanent Bearer key (single use)
- REQ-003: Expired/used OTP returns 401
**pytest_ids:** `test_otp_exchange.py::test_valid_otp_exchange`, `test_otp_exchange.py::test_expired_otp_rejected`

### 3.3 Instance Pre-warming (instance_warmer.py)

**Proposed story:** FC-PIPE-WARM-001
**Title:** Pre-warm Azure Flex Consumption instances before fan-out
**Description:** Set always_ready = num_workers via Azure Management API before fan-out dispatch. Eliminates cold-start stacking and OOM kills. Write metrics to admin.WarmUpMetrics.
**Requirements:**
- REQ-001: Pre-warm instances before fan-out (num_workers instances)
- REQ-002: Wait floor of 60 seconds for propagation
- REQ-003: Cool down (reset always_ready = 0) after fan-out completes
- REQ-004: Write timing metrics to admin.WarmUpMetrics
**pytest_ids:** `test_instance_warmer.py::test_warm_and_cool_cycle`

### 3.4 Blob Storage Client (blob_client.py)

**Proposed story:** FC-PIPE-INFRA-001
**Title:** Shared Azure Blob Storage singleton
**Description:** All pipeline blob operations use the shared BlobServiceClient singleton. Container naming convention enforced.
**Requirements:**
- REQ-001: Single BlobServiceClient singleton per function app instance
- REQ-002: Container names follow naming convention (admin, {env}-brain, etc.)
**pytest_ids:** `test_blob_client.py::test_singleton_reuse`

### 3.5 Pipeline Database Access (pipeline_db.py)

**Proposed story:** FC-PIPE-INFRA-002
**Title:** Shared MongoDB access with ENV_PREFIX routing
**Description:** All pipeline MongoDB access uses pipeline_db.get_db() with CV-010 validated environment prefix.
**Note:** Partially covered by PIPE-BP-004-REQ-004 but deserves its own infrastructure requirement.
**pytest_ids:** `test_pipeline_db.py::test_env_prefix_routing`, `test_pipeline_db.py::test_invalid_env_rejected`

### 3.6 Auth Infrastructure (auth.py)

**Proposed story:** FC-PIPE-AUTH-001
**Title:** Bearer token authentication for pipeline Router
**Description:** All pipeline API calls require valid Bearer token. Tokens configured via API_TOKEN_MAP.
**Requirements:**
- REQ-001: Missing or invalid token returns 401
- REQ-002: Valid token returns user identity
**pytest_ids:** `test_auth.py::test_valid_token`, `test_auth.py::test_missing_token_401`

### 3.7 Provider Load Validation (validate_provider_load.py)

**Proposed story:** FC-PIPE-QA-001
**Title:** Automated spot-check validation against NPPES registry
**Description:** Samples providers from latest load, validates against public NPPES API. Writes report to admin.ValidationReport.
**Requirements:**
- REQ-001: Sample N providers per worker from most recent load
- REQ-002: Compare name, address, taxonomy against NPPES registry
- REQ-003: Verify county enrichment is present
**pytest_ids:** `test_validate_provider_load.py::test_validation_report_generated`

### 3.8 ZIP-County Crosswalk Loader (zip_county_crosswalk_loader.py)

**Proposed story:** FC-PIPE-CROSSWALK-001
**Title:** Load US Census ZCTA-to-county crosswalk reference data
**Description:** Download Census ZCTA-to-county relationship file, load into ZipCountyCrosswalk collection. Required by county_enrichment_job Pass 1.
**Requirements:**
- REQ-001: Load all ZIP-to-county mappings with res_ratio and split flag
- REQ-002: Uses DataFetcherBase for download (v4-001D compliance)
**pytest_ids:** `test_crosswalk_loader.py::test_crosswalk_loaded`

### 3.9 Discrepancy Reporter (discrepancy_reporter.py)

**Proposed story:** FC-PIPE-REPORT-001
**Title:** Pipeline run result reporting and email notification
**Description:** Write run results to admin.PipelineDiscrepancyReports. Send SparkPost email on completion with summary of workers, failures, reconciliation.
**Requirements:**
- REQ-001: Write structured report to MongoDB after every pipeline run
- REQ-002: Send email notification via SparkPost on completion
**pytest_ids:** `test_discrepancy_reporter.py::test_report_written`

### 3.10 Sync Gateway Agent (sync_gateway_agent.py)

**Proposed story:** FC-PIPE-SYNC-001
**Title:** Agent-gated promotion from pipeline to frontend (CopyToFrontEnd v2)
**Description:** Streaming copy with quality gates, schema drift detection, parity verification, atomic swap for large states, per-state partitioning.
**Requirements:**
- REQ-001: Quality gate check before promotion starts
- REQ-002: Schema drift detection before copy
- REQ-003: Parity verification after copy
- REQ-004: Streaming copy in 500-doc batches (no OOM)
**pytest_ids:** `test_sync_gateway.py::test_promotion_with_quality_gate`

### 3.11 Ops Manager Agent (ops_manager/*.py)

**Proposed story:** FC-PIPE-OPS-001
**Title:** Pipeline Operations Manager agent
**Description:** Autonomous agent managing infrastructure lifecycle. Uses tools (ClusterTool, AlertTool, TriageTool). Produces EvidencePackage for Dev Manager. Append-only audit trail.
**Requirements:**
- REQ-001: Agent manages cluster lifecycle via tools (status, wake, release)
- REQ-002: Error triage classifies as INFRASTRUCTURE, PIPELINE, or UNKNOWN
- REQ-003: All actions logged to immutable audit trail
- REQ-004: Alerts enforce cooldown windows per event type
- REQ-005: EvidencePackage is the only handoff to Dev Manager
**pytest_ids:** `test_ops_agent.py::test_agent_registers_tools`

### 3.12 Atlas Cluster Manager (atlas_cluster_manager.py)

**Proposed story:** FC-PIPE-ATLAS-001
**Title:** Atlas API cluster scaling utility
**Description:** Scale Atlas clusters up (resume + resize) and down (pause) via Atlas Admin API. Used by ClusterLifecycleManager and idle_monitor.
**Requirements:**
- REQ-001: Scale-up resumes paused cluster and resizes to job tier
- REQ-002: Scale-down pauses cluster (zero compute cost)
- REQ-003: Waits for IDLE state before returning
**pytest_ids:** `test_atlas_cluster_manager.py::test_scale_up_resumes`

### 3.13 County Economic Enrichment (county_economic_enrichment.py)

**Proposed story:** FC-PIPE-ECON-001
**Title:** County economic indicator enrichment
**Description:** Enrich provider county data with GDP, income, rural-urban codes from BEA, Census ACS, USDA. Required for EvaluateCare scoring.
**Requirements:**
- REQ-001: Enrich county with GDP from BEA CAGDP2
- REQ-002: Enrich county with mean/median household income from Census ACS
- REQ-003: Enrich county with USDA Rural-Urban Continuum Code
**pytest_ids:** `test_county_economic_enrichment.py::test_enrichment_adds_gdp`

### 3.14 Idle Monitor (idle_monitor.py)

**Proposed story:** FC-PIPE-IDLE-001
**Title:** Auto-pause pipeline cluster after idle threshold
**Description:** Timer-based check of pipeline cluster activity. Pauses and notifies when idle exceeds threshold hours. Currently disabled in favor of ClusterLifecycleManager reservation-based approach.
**Status note:** May be DEPRECATED by ClusterLifecycleManager (PIPE-LC-015). Confirm with human whether to keep or remove.
**pytest_ids:** `test_idle_monitor.py::test_idle_pause_triggered`

---

## Section 4: Misplaced Files

### 4.1 ChatHealthyMongoUtilities.py

**Issue:** File header explicitly says "DO NOT USE in DataPipelines." This is a ConversationalUX/GUI utility placed in the wrong directory.
**Recommendation:** Move to `Code/Shared/` or `Code/ConversationalUX/`.

---

## Section 5: Summary

| Category | Count |
|---|---|
| Files audited (active) | 35 |
| Files with existing requirement mappings | 22 |
| Files needing new requirements | 14 |
| Dead code files | 6 |
| Disabled but not dead | 1 (idle_monitor.py) |
| Misplaced files | 1 (ChatHealthyMongoUtilities.py) |
| New requirements to create | 14 stories |

### Dead Code Files (mark EXTRANEOUS or delete)
1. `load_provider_data.py` -- stub placeholder
2. `copy_va_incremental.py` -- one-time VA migration
3. `embed_va_backfill.py` -- one-time VA backfill
4. `move_to_frontend.py` -- one-time cluster migration
5. `migrate_discrepancy_reports.py` -- one-time collection rename
6. `rename_to_dev.py` -- one-time env prefix migration
7. `refactor_db.py` -- one-time DB restructure
