# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""rollup_specialtymedicare (EPIC-010-F-007, LLD v83 III.10) — the NUCC rollup.

Single partition (fan_out: single), staging-sourced and serial: the engine
streams StagingMedicarePartD, accumulates per-(provider, molecule) totals,
rolls to the NUCC grain with the R1 statistics, and writes one document per
NUCC. Resolves collections through the registry. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rollup_engine import (
    rollup_specialtymedicare)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (
        parse_provider_source)
    prov_db, prov_name = parse_provider_source(ctx.args.provider_collection)
    provider_spine_coll = mongo[prov_db][prov_name]

    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    spec = reg.by_source_name("specialtymedicare")
    target_coll = mongo[spec.public_data_db][reg.public_data_collection_name("specialtymedicare")]

    result = rollup_specialtymedicare(
        run_id=ctx.run_id,
        partd_coll=partd_coll,
        provider_spine_coll=provider_spine_coll,
        target_coll=target_coll,
    )
    _log.LogPipeline("INFO", "rollup_specialtymedicare: %s", result)
    return {"step": "rollup_specialtymedicare", **result}


def execute(ctx):
    return run_step(ctx)
