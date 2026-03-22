# ChatHealthy.ai Roadmap

## Milestone: Tuesday 2026-03-24 — Data Loading Complete

- Load ICD-10 diagnosis codes — `LoadICD10` task already in pipeline router
- Load procedure codes (CPT/HCPCS) — new pipeline needed
- Load CMS Medicare Part D prescription data — new pipeline needed
- Pipeline fixes already deployed: MongoDB health check, embeddings + Atlas Vector Search,
  `start_step` resume parameter, license field normalization, ZIP truncation

---

## Milestone: Alpha Launch — April 30, 2026

A working end-to-end ChatHealthy.ai product: users can describe a health need in natural language
and receive relevant provider results with geographic and specialty context.

**Included:**
- All data loaded: providers (embedded), ICD-10, procedures, prescriptions, specialties, crosswalk
- Vector search wired into ConversationalUX — natural language provider queries
- ICD-10 → specialty → provider linking agent (RESEARCH-002)
- Provider cards, maps, and inline components in the chat UI
- Session transcript export

---

## Research Agents

### RESEARCH-001: Multi-location provider data (payer directory agent)

**Status:** Open
**Priority:** Post-Alpha

NPPES NPI data captures only one practice address per individual provider (Type 1 NPI). A provider practicing at multiple locations has a single NPI record pointing to their primary address — the other locations are invisible.

**The gap:** Users searching by location may miss providers who practice nearby but list a different primary address in NPPES.

**Proposed solution:** A research agent that supplements NPPES with payer network directory data. Payer directories are location-level (providers must register each billing location to get paid) and are kept more current than NPI.

**Data sources to investigate:**
- Payer network directories — per-payer, not a single public feed; would need to pull from major payers (BCBS, Aetna, UHC, Cigna, etc.)
- CMS Medicare Provider Enrollment data — shows facilities where a provider has billed Medicare
- CMS Open Payments — facility-level billing activity
- CAQH — provider self-reported, credentialing-focused, not public

**Approach:** Agent queries payer directories by NPI, collects all reported practice locations, stores as a `locations` array on the provider document supplementing the single NPPES practice address.

**Why an agent:** Each payer directory has a different API/format; an agent can handle the per-payer variation, retry logic, and reconciliation across sources.

---

### RESEARCH-002: ICD-10 → specialty → provider linking agent

**Status:** Open
**Priority:** Alpha milestone (April 30, 2026)

Given a patient's diagnosis (ICD-10 code), FindCare needs to identify which provider specialties treat that condition and surface relevant providers along with their prescribing patterns.

**The problem:** ICD-10 codes and provider taxonomy (specialty) codes are separate classification systems with no built-in mapping between them. A user saying "I have Type 2 diabetes" should find endocrinologists and primary care providers — that linkage doesn't exist in the raw data.

**Proposed approach:**
1. Map ICD-10 codes → relevant taxonomy codes (specialty crosswalk) — likely LLM-assisted or via CMS/AMA published mappings
2. Use taxonomy codes to filter providers from the NPI dataset
3. Join with CMS Medicare Part D prescriber data to show prescribing patterns for that condition

**Data sources:**
- ICD-10-CM codes — `LoadICD10` already exists in the pipeline router
- Provider taxonomy codes — already loaded via `LoadSpecialtyData`
- CMS Medicare Part D prescriber data — new pipeline needed

**Why an agent:** The ICD-10 → specialty mapping requires reasoning (one condition may involve multiple specialties; some mappings are nuanced). An LLM agent is better suited than a static lookup table.

---

## Infrastructure

### INFRA-001: CopyToRuntime + Blue-Green Collection Swap

**Status:** Open
**Priority:** Post-Alpha

Fan-out copy of `providers_staging` → `providers_new` on ChatHealthyFrontEnd cluster, then atomic three-step rename swap. Until implemented, runtime reads are served from the DataPipelines cluster.

### INFRA-002: Cluster manager agent

**Status:** Open
**Priority:** Post-Alpha

Dedicated agent/service to start and stop the ChatHealthyDataPipelines cluster on a schedule, fully decoupled from the pipeline. Current approach: manual. Required before idle monitor (BUG-001) can be safely re-enabled.

### INFRA-003: Migrate ConversationalUX off HuggingFace → Azure Static Web Apps

**Status:** Open
**Priority:** Post-Alpha

HuggingFace has a 60-second request timeout (too short for multi-agent workflows), spaces sleep on inactivity, and free-tier compute is undersized for production. Azure Static Web Apps: React frontend on Azure CDN + FastAPI backend as an Azure Function — same infrastructure as DataPipelines, single vendor, single deploy pipeline.

### INFRA-004: Enterprise security hardening

**Status:** Open
**Priority:** Pre-sales blocker

Pre-sales requirements: fine-grained GitHub PAT or SSH key with passphrase; Azure Key Vault for secrets; GitHub Actions OIDC federation replacing long-lived secrets; GPG-signed commits; separation of deploy and dev credentials.

### INFRA-005: Production MongoDB cluster

**Status:** Open
**Priority:** Post-Alpha

Currently dev-only. Production cluster requires sizing, backup policy, IP allowlist locked to VNet only, and separation of PipelineUser and FrontEndUser credentials.

### INFRA-006: Refactor DataPipelines into packages

**Status:** Open
**Priority:** Pre-Alpha — before ICD-10 and prescriptions pipelines are written

Flat file structure (19 files) lacks architectural clarity. Restructure into packages so the folder layout reflects the system design:

- `providers/` — NPI load, worker, embedding, validator
- `enrichment/` — county enrichment, crosswalk loader
- `loaders/` — ICD-10, specialty, copy-to-frontend, future data sources
- `infrastructure/` — mongo, blob, auth, health, cluster manager, reporter

**Timing:** Do before writing new loaders so ICD-10 and prescriptions land in `loaders/` from the start. All imports in `function_app.py` and across the codebase will need updating.

---

### INFRA-007: Durable Functions orchestrator versioning

**Status:** Open
**Priority:** Pre-Alpha — April 25, 2026

Deploying new orchestrator code while a pipeline run is in progress causes a non-deterministic replay failure (confirmed March 22, 2026). The deploy guard in `deploy-pipelines.yml` now blocks deploys during active runs, but does not protect against `Pending` or `ContinuedAsNew` states, and relies on the developer not triggering a manual deploy.

Full solution: side-by-side orchestrator versioning so old instances replay against the version they started with, and new deployments route to the new version. Each orchestrator is suffixed with a version number; old versions remain in the codebase until all in-flight instances complete.

**Decision needed before Alpha:** confirm whether the fixed guard is sufficient for Alpha scale, or whether full versioning is required.

---

## Future

### FUTURE-001: User authentication and cross-session persistence

**Status:** Open
**Priority:** Post-Alpha

Users can currently save a session transcript but have no persistent identity across sessions. Authentication would enable saved searches, provider favourites, and longitudinal session history.

### FUTURE-002: Data deletion agent

**Status:** Open
**Priority:** Post-Alpha

Azure Function triggered by email verification: deletes user record from MongoDB, sends confirmation email. Required for GDPR/CCPA compliance before any EU or California user data is collected.

### FUTURE-003: FHIR / Epic EMR integration

**Status:** Deferred
**Priority:** Future phase

FHIR API integration with Epic and other EMR systems to pull patient context directly into the session (conditions, medications, care team). Plugs into Layer 2 (DataPipelines). Deferred until core provider search is live and validated.

---

## Potential Requirements

### POTENTIAL-001: Purge deactivated providers + rename collection

**Status:** Under consideration
**Priority:** Post-Alpha

After the provider load, delete records where `npi_deactivation_date` is present and `npi_reactivation_date` is absent (305,510 records as of March 2026). Reactivated providers (16,245) would be retained as they are currently active.

If purge is implemented, rename `providers_staging` → `active_providers` to make the collection intent explicit.

**Decision needed:** Purge vs. retain-but-skip (current approach). Purge reduces collection size and simplifies queries; retain keeps the full NPI universe available for future use cases.

---

## Bugs

### BUG-001: Idle monitor can pause cluster while user is in Atlas GUI

**Status:** Open (idle monitor currently disabled)
**Severity:** Low (dev inconvenience)

The idle monitor fired every 30 minutes and paused `ChatHealthyDataPipelines` if no active load workers were detected and the last completed pipeline report was older than `IDLE_MONITOR_THRESHOLD_HOURS` (default 2h). It had no awareness of a human browsing the Atlas console — if the threshold was exceeded, it would pause the cluster mid-session.

**Current state:** Timer trigger is commented out in `function_app.py`. Cluster is managed manually.

**Fix options:** Re-enable only after adding a pipeline lock mechanism that the idle monitor respects. Alternatively, implement the cluster manager as a separate agent (INFRA-002) fully decoupled from the pipeline.
