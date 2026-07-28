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
            partition_key="source_name",
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
            name="normalize_npi_serial_bulk_load",
            prerequisites=["load_f006_catalog"],
            parallelism="serial",
            aca_job_name="prov-normalize",
        ),
        StepSpec(
            name="normalize_npi_per_state_fanout",
            prerequisites=["normalize_npi_serial_bulk_load"],
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
            name="type1_first_branch",
            prerequisites=["add_secondary_practices"],
            parallelism="process_pool",
            aca_job_name="prov-type1-first-branch",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="type2_first_branch",
            prerequisites=["add_secondary_practices"],
            parallelism="process_pool",
            aca_job_name="prov-type2-first-branch",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="license_address_repair",
            prerequisites=["type1_first_branch", "type2_first_branch"],
            parallelism="process_pool",
            aca_job_name="prov-license-address-repair",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="county_enrichment",
            prerequisites=["license_address_repair"],
            parallelism="process_pool",
            aca_job_name="prov-county-enrichment",
            partition_key="county_partition",
        ),
        StepSpec(
            name="urban_flag",
            prerequisites=["county_enrichment"],
            parallelism="process_pool",
            aca_job_name="prov-urban-flag",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="type1_second_branch",
            prerequisites=["urban_flag"],
            parallelism="process_pool",
            aca_job_name="prov-type1-second-branch",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="type2_second_branch",
            prerequisites=["urban_flag"],
            parallelism="process_pool",
            aca_job_name="prov-type2-second-branch",
            partition_key="business_address_state",
        ),
        StepSpec(
            name="post_load_reconciliation",
            prerequisites=["type1_second_branch", "type2_second_branch"],
            parallelism="serial",
            aca_job_name="prov-post-load-reconciliation",
        ),
        StepSpec(
            name="discrepancy_and_notifications",
            prerequisites=["post_load_reconciliation"],
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
