# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_indication_map (EPIC-010-F-007) — mine the Normalized Indication Map.

The map is MINED, not inferred: the disease_class -> parent_indication ->
ICD-10 leaf hierarchy is adopted from the AHRQ CCSR staging, and molecule ->
indication associations are mined from the FDA-label prose of the drugs actually
prescribed under Part D, resolved through the shared facade (the thin residual).
Only an indication with no coded placement falls to that residual.

Gate: BUILD_INDICATION_MAP (set by control_runner from --build-indication-map,
default off). A normal run consumes the current published map and this step is a
no-op; a rebuild run mines a candidate at this run's data_version, released to
the serving store through the operator-gated crossing (F-003) -- never
auto-replacing the live map. Run serially, once per rebuild.
"""

from __future__ import annotations

import os

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_map_engine import (
    build_indication_map as _build)

_log = ChatHealthyLoggingService()


def _rebuild_requested() -> bool:
    return os.environ.get("BUILD_INDICATION_MAP", "").strip().lower() in ("1", "true", "yes")


def run_step(ctx) -> dict:
    rebuild = _rebuild_requested()
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    if not rebuild:
        _log.LogPipeline(
            "INFO",
            "build_indication_map: consume mode (BUILD_INDICATION_MAP off) run_id=%s; "
            "per-provider steps read the current published map",
            ctx.run_id,
        )
        return {"step": "build_indication_map", "rebuild": False, "consumed": True}

    ccsr = reg.by_source_name("ccsr")
    ccsr_coll = mongo[ccsr.staging_db][reg.staging_collection_name("ccsr")]
    icd = reg.by_source_name("icd10_cm")
    icd10_coll = mongo[icd.staging_db][reg.staging_collection_name("icd10_cm")]
    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    ofda = reg.by_source_name("openfda_labels")
    openfda_coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
    tgt = reg.by_source_name("indication_map")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("indication_map")]

    result = _build(
        run_id=ctx.run_id,
        data_version=int(ctx.args.data_version),
        ccsr_coll=ccsr_coll,
        icd10_coll=icd10_coll,
        partd_coll=partd_coll,
        openfda_coll=openfda_coll,
        target_coll=target_coll,
        rebuild=True,
    )
    _log.LogPipeline("INFO", "build_indication_map: %s", result)
    return result


def execute(ctx):
    return run_step(ctx)
