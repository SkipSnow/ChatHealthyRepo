# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 §3.2 — ProviderPipelineOrchestrator.

Declares PIPELINE_NAME = "provider" and the canonical STEPS list from
LLD v23 §3.2. Prerequisite edges follow the sequence in §3.2 and the
step-by-step semantics of §4. Parallelism attribute follows §2.3:
per-partition fan-out steps run under process_pool; single-worker
stages run serial; the freshness gate, catalog load, and infrastructure
lifecycle steps run serial.
"""

from __future__ import annotations

from base_pipeline_orchestrator import BasePipelineOrchestrator
from step_spec import StepSpec


class ProviderPipelineOrchestrator(BasePipelineOrchestrator):
    PIPELINE_NAME = "provider"

    STEPS: list[StepSpec] = [
        StepSpec(
            name="prepare_infrastructure",
            prerequisites=[],
            parallelism="serial",
            aca_job_name="prov-prepare-infrastructure",
        ),
        StepSpec(
            name="source_freshness_gate",
            prerequisites=["prepare_infrastructure"],
            parallelism="serial",
        ),
        StepSpec(
            name="fetch_all_sources",
            prerequisites=["source_freshness_gate"],
            parallelism="process_pool",
            aca_job_name="prov-fetch-sources",
            partition_key="source_name_base",
        ),
        StepSpec(
            name="source_archival",
            prerequisites=["fetch_all_sources"],
            parallelism="serial",
        ),
        StepSpec(
            name="load_staging_parallel",
            prerequisites=["source_archival"],
            parallelism="process_pool",
            aca_job_name="prov-load-staging",
            partition_key="source_name",
        ),
        StepSpec(
            name="load_f006_catalog",
            prerequisites=["load_staging_parallel"],
            parallelism="serial",
        ),
        StepSpec(
            # Enrich raw NUCC staging with F-105 proprietary data
            # (can_prescribe / is_homeopathic / is_disqualified) and insert
            # F-105 supplement codes not present in NUCC as new rows with
            # is_supplemented=true. v42 §5.2.8a removed the inline SMD
            # publish; that lives in publish_smd_and_embed now.
            name="normalize_nucc",
            prerequisites=["load_f006_catalog"],
            parallelism="serial",
            aca_job_name="prov-normalize-nucc",
        ),
        StepSpec(
            # v42 §5.2.8a: copy StagingNucc -> frontend SMD_staging,
            # generate text-embedding-3-large per row, atomic swap to
            # live SpecialtyMetaData via renameCollection(dropTarget=True).
            # Downstream Provider normalize + FindCare $vectorSearch both
            # depend on this having landed. MUST appear before
            # normalize_npi_per_state_fanout in this list -- the orchestrator
            # walks STEPS in array order and enforces prereqs at first
            # visit; positional order is the topological gate.
            name="publish_smd_and_embed",
            prerequisites=["normalize_nucc"],
            parallelism="serial",
            aca_job_name="prov-publish-smd-embed",
        ),
        StepSpec(
            # v42 §5.2.9 Pass B: reads the fully-published SpecialtyMetaData
            # (with F-105 supplements) on the frontend cluster to stamp
            # taxonomies[i].code_label. Prerequisite chain enforces that:
            # normalize_nucc supplements StagingNucc; publish_smd_and_embed
            # copies + embeds + atomic-renames into live SMD; only then
            # does normalize run. Fixes the code_label race on F-105-only
            # primary codes surfaced 2026-08-02.
            name="normalize_npi_per_state_fanout",
            prerequisites=["load_f006_catalog", "publish_smd_and_embed"],
            parallelism="process_pool",
            aca_job_name="prov-normalize-state-fanout",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="add_secondary_practices",
            prerequisites=["normalize_npi_per_state_fanout"],
            parallelism="process_pool",
            aca_job_name="prov-add-secondary-practices",
            partition_key="business_address_state",
        ),
        StepSpec(
            # F-105 flag stamping (can_prescribe / is_homeopathic /
            # is_disqualified) driven off the provider's primary taxonomy
            # code via the specialty_classification_catalog. Independent
            # of county enrichment -- runs as soon as the normalized
            # provider record exists.
            name="provider_flags_enrichment",
            prerequisites=["add_secondary_practices"],
            parallelism="process_pool",
            aca_job_name="prov-provider-flags",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="harvest_other_identifier_phrases",
            prerequisites=["add_secondary_practices"],
            parallelism="serial",
            aca_job_name="prov-harvest-oi-phrases",
        ),
        StepSpec(
            name="classify_other_identifier_phrases",
            prerequisites=["harvest_other_identifier_phrases"],
            parallelism="process_pool",
            aca_job_name="prov-classify-oi-phrases",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="apply_other_identifier_classifications",
            prerequisites=["classify_other_identifier_phrases"],
            parallelism="process_pool",
            aca_job_name="prov-apply-oi-classifications",
            partition_key="business_address_state",
        ),
        StepSpec(
            # LLD v45 sec 5.2.11: "fan-out to state x entity-type begins
            # here". Type 1 and Type 2 are disjoint -- an NPI carries one
            # Entity Type Code -- so they never write the same document and
            # neither waits for the other. As two sequential steps they
            # doubled the wall-clock of this phase; as one step partitioned
            # by (state, entity_type) they run together.
            name="entity_first_branch",
            prerequisites=["apply_other_identifier_classifications"],
            parallelism="process_pool",
            aca_job_name="prov-entity-first-branch",
            partition_key="state_entity",
        ),
        StepSpec(
            name="license_address_repair",
            prerequisites=["entity_first_branch"],
            parallelism="process_pool",
            aca_job_name="prov-license-address-repair",
            partition_key="business_address_state",
        ),
        StepSpec(
            # County enrichment stamps practice_addresses[i].county.rucc
            # (int 1-9 USDA RUCC) alongside fips/name/source. The urban
            # marker derived from it is its own step below, so its work,
            # its timing and its failure are visible on the run rather than
            # hidden inside this one.
            name="county_enrichment",
            prerequisites=["license_address_repair"],
            parallelism="process_pool",
            aca_job_name="prov-county-enrichment",
            partition_key="county_partition",
        ),
        StepSpec(
            # LLD 5.2.15. Derives county.urban per address and the
            # provider-level urban marker from the RUCC county_enrichment
            # resolved, so it must follow it.
            name="urban_flag",
            prerequisites=["county_enrichment"],
            parallelism="process_pool",
            aca_job_name="prov-urban-flag",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="entity_second_branch",
            prerequisites=["urban_flag"],
            parallelism="process_pool",
            aca_job_name="prov-entity-second-branch",
            partition_key="state_entity",
        ),
        StepSpec(
            name="post_load_reconciliation",
            prerequisites=["entity_second_branch"],
            parallelism="serial",
            aca_job_name="prov-post-load-reconciliation",
        ),
        StepSpec(
            # Operator directive 2026-08-02: consumers never observe
            # partially-enriched Provider records mid-pipeline. Every
            # upstream write step targets PipelinePublicHealthData.
            # Provider_staging_v_{data_version}; publish_provider does
            # the atomic renameCollection swap into Provider_v_{
            # data_version} and calls mark_loaded so consumers transition
            # from prior-fire-complete to this-fire-complete in one
            # operation. Runs late, after reconciliation, so only a
            # verified-good build ever lands as loaded.
            name="publish_provider",
            prerequisites=["post_load_reconciliation"],
            parallelism="serial",
            aca_job_name="prov-publish-provider",
        ),
        StepSpec(
            name="discrepancy_and_notifications",
            prerequisites=["publish_provider"],
            parallelism="serial",
            aca_job_name="prov-discrepancy-notify",
        ),
        StepSpec(
            name="quiesce_infrastructure",
            prerequisites=[],
            parallelism="serial",
            invocation_phase="finally_block",
        ),
    ]
