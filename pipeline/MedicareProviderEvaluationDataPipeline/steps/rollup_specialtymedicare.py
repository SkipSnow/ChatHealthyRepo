# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""rollup_specialtymedicare (EPIC-010-F-007) — build SpecialtyMedicare per NUCC.

Fanned by nucc_code: each worker rolls up the ProviderMedicare records for the
NUCC code(s) in its partition into SpecialtyMedicare, carrying the day-supply
benchmark (mean / median / quintile cut-points per drug and per indication) and
provider_count read from the provider base. Resolves collections through the
registry and delegates the aggregation to the engine. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rollup_engine import (
    rollup_specialtymedicare as _rollup)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    nucc = partition.get("nucc_code") if isinstance(partition, dict) else None
    # A real nucc_code value scopes this worker to one NUCC; the single-partition
    # placeholder (BUG-014) carries none, so the worker rolls up every NUCC.
    nucc_codes = {nucc} if nucc else None

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    pm = reg.by_source_name("providermedicare")
    providermedicare_coll = mongo[pm.public_data_db][reg.public_data_collection_name("providermedicare")]
    prov = reg.by_source_name("provider")
    provider_spine_coll = mongo[prov.public_data_db][reg.public_data_collection_name("provider")]
    sm = reg.by_source_name("specialtymedicare")
    target_coll = mongo[sm.public_data_db][reg.public_data_collection_name("specialtymedicare")]
    # The specialty embedding is copied from the SMD collection the provider
    # pipeline already built (same vector FindCare's vector search uses).
    smd_coll = None
    try:
        smd = reg.by_source_name("smd")
        smd_coll = mongo[smd.public_data_db][reg.public_data_collection_name("smd")]
    except Exception:  # noqa: BLE001 - SMD absent means no embedding to copy
        smd_coll = None

    result = _rollup(
        run_id=ctx.run_id,
        data_version=int(ctx.args.data_version),
        providermedicare_coll=providermedicare_coll,
        provider_spine_coll=provider_spine_coll,
        target_coll=target_coll,
        smd_coll=smd_coll,
        nucc_codes=nucc_codes,
    )
    _log.LogPipeline("INFO", "rollup_specialtymedicare: %s", result)
    return result


def execute(ctx):
    return run_step(ctx)
