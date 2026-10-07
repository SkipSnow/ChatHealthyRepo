# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_procedures (EPIC-010-F-007 / S-005) — procedure coverage justification.

Fanned by provider_state: each worker joins the Medicare Coverage Database HCPCS
and ICD-10 staging tables into an HCPCS -> covered-ICD-10 map and stamps each
billed procedure on its state's ProviderMedicare records with the diagnoses it is
covered for and the coded source. Resolves collections through the registry; the
join is the engine's. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_procedures_engine import enrich_procedures

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("provider_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message="enrich_procedures: expected ctx.config['partition']['provider_state']",
            component="enrich_procedures",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    pm = reg.by_source_name("providermedicare")
    providermedicare_coll = mongo[pm.public_data_db][reg.public_data_collection_name("providermedicare")]
    hcpc = reg.by_source_name("medicare_coverage_hcpc")
    coverage_hcpc_coll = mongo[hcpc.staging_db][reg.staging_collection_name("medicare_coverage_hcpc")]
    icd = reg.by_source_name("medicare_coverage_icd10")
    coverage_icd10_coll = mongo[icd.staging_db][reg.staging_collection_name("medicare_coverage_icd10")]

    result = enrich_procedures(
        state=state,
        run_id=ctx.run_id,
        providermedicare_coll=providermedicare_coll,
        coverage_hcpc_coll=coverage_hcpc_coll,
        coverage_icd10_coll=coverage_icd10_coll,
    )
    _log.LogPipeline("INFO", "enrich_procedures[%s]: %s", state, result)
    return result


def execute(ctx):
    return run_step(ctx)
