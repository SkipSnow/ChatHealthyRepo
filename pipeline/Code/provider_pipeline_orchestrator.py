# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderPipelineOrchestrator — LLD v22 §3.2 STEPS declaration."""

from __future__ import annotations

from base_pipeline_orchestrator import BasePipelineOrchestrator
from blob_client import get_blob_service
from pipeline_config import load_pipeline_config
from pipeline_db import get_frontend_mongo, get_mongo
from step_context import PipelineArgs, StepContext
from step_spec import StepSpec


class ProviderPipelineOrchestrator(BasePipelineOrchestrator):
    PIPELINE_NAME = "provider"
    STEPS = [
        StepSpec("prepare_infrastructure"),
        StepSpec("source_freshness_gate", prerequisites=("prepare_infrastructure",)),
        StepSpec("fetch_all_sources", prerequisites=("source_freshness_gate",)),
        StepSpec("archive_sources", prerequisites=("fetch_all_sources",)),
        StepSpec("load_staging_parallel", prerequisites=("fetch_all_sources",)),
        StepSpec(
            "load_nucc_classification_catalog",
            prerequisites=("prepare_infrastructure",),
            parallelism="gather",
        ),
        StepSpec("normalize_npi_serial_bulk_load", prerequisites=("load_staging_parallel",)),
        StepSpec(
            "normalize_npi_per_state_fanout",
            prerequisites=("normalize_npi_serial_bulk_load",),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-normalize-state-fanout",
        ),
        StepSpec("attach_practice_addresses", prerequisites=("normalize_npi_per_state_fanout",)),
        StepSpec(
            "type1_first_branch",
            prerequisites=("attach_practice_addresses",),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-type1-first-branch",
        ),
        StepSpec(
            "type2_first_branch",
            prerequisites=("attach_practice_addresses",),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-type2-first-branch",
        ),
        StepSpec(
            "license_address_repair",
            prerequisites=("type1_first_branch", "type2_first_branch"),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-license-address-repair",
        ),
        StepSpec(
            "county_enrichment_cascade",
            prerequisites=("license_address_repair",),
            parallelism="process_pool",
            partition_key="business_address_state+kind",
            aca_job_name="prov-county-enrichment",
        ),
        StepSpec(
            "urban_flag",
            prerequisites=("county_enrichment_cascade",),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-urban-flag",
        ),
        StepSpec(
            "type1_second_branch",
            prerequisites=("urban_flag", "load_nucc_classification_catalog"),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-type1-second-branch",
        ),
        StepSpec(
            "type2_second_branch",
            prerequisites=("urban_flag", "load_nucc_classification_catalog"),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-type2-second-branch",
        ),
        StepSpec(
            "provider_flags_enrichment",
            prerequisites=("type1_second_branch", "type2_second_branch"),
        ),
        StepSpec(
            "post_load_reconciliation",
            prerequisites=("provider_flags_enrichment",),
            parallelism="process_pool",
            partition_key="business_address_state",
            aca_job_name="prov-post-load-reconciliation",
        ),
        StepSpec(
            "discrepancy_reports_and_notifications",
            invocation_phase="finally_block",
            aca_job_name="prov-discrepancy-and-notifications",
        ),
        StepSpec(
            "quiesce_infrastructure",
            invocation_phase="finally_block",
        ),
    ]

    def build_step_context(self, args: PipelineArgs) -> StepContext:
        mongo = get_mongo()
        cfg = load_pipeline_config(get_frontend_mongo(), args.env_prefix)
        cfg.update({
            "pipeline_cluster": "ChatHealthyDataPipelines",
            "pool_size": args.pool_size,
            "source_freshness": cfg.get("source_freshness"),
            "staging_container": "pipeline-staging",
            "inner_workers": 16,
        })
        return StepContext(
            args=args,
            manifest=None,  # type: ignore[arg-type]
            config=cfg,
            mongo_client=mongo,
            blob_client=get_blob_service(),
        )
