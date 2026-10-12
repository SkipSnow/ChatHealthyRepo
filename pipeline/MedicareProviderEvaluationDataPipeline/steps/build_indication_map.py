# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_indication_map (EPIC-010-F-007) — mine the Normalized Indication Map.

This step is a named PREREQUISITE of the map-dependent steps (enrich_drugs and
the rollup). It PRODUCES the Normalized Indication Map candidate from THIS run's
own staged data: the disease_class -> parent_indication -> ICD-10 leaf hierarchy
is adopted from the AHRQ CCSR staging, and molecule -> indication associations
are mined across the whole FDA-label universe, each FDA-label indication prose
resolved to ICD-10-CM codes through a pydantic-ai Agent run directly.

The step never consumes a released map: the pipeline proposes a candidate here
and the data-release migrator (EPIC-010-F-003) publishes it. The build is
idempotent at the run level -- if this run already built the map (a resume or a
worker retry), it is not mined again. Run serially, once per run.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_map_engine import (
    build_indication_map as _build)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    tgt = reg.by_source_name("indication_map")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("indication_map")]

    # Idempotent prerequisite: if this run already produced the candidate (a
    # resume or a worker retry re-entering the step), do not mine it again.
    already = target_coll.count_documents({"run_id": ctx.run_id})
    if already:
        _log.LogPipeline(
            "INFO",
            "build_indication_map: candidate already built this run run_id=%s "
            "indications=%d; skipping rebuild",
            ctx.run_id, already,
        )
        return {"step": "build_indication_map", "rebuild": False,
                "indications_written": 0, "already_built": already}

    ccsr = reg.by_source_name("ccsr")
    ccsr_coll = mongo[ccsr.staging_db][reg.staging_collection_name("ccsr")]
    icd = reg.by_source_name("icd10_cm")
    icd10_coll = mongo[icd.staging_db][reg.staging_collection_name("icd10_cm")]
    ofda = reg.by_source_name("openfda_labels")
    openfda_coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]

    result = _build(
        run_id=ctx.run_id,
        data_version=int(ctx.args.data_version),
        ccsr_coll=ccsr_coll,
        icd10_coll=icd10_coll,
        openfda_coll=openfda_coll,
        target_coll=target_coll,
        rebuild=True,
    )
    _log.LogPipeline("INFO", "build_indication_map: %s", result)
    return result


def execute(ctx):
    return run_step(ctx)
