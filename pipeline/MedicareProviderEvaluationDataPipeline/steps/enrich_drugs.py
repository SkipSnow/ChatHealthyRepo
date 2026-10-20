# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs (EPIC-010-F-007, LLD v83 III.10) — attach the masters' FDA
facts.

Serial: one pass over the molecule masters, attaching each master's fda block
from its molecule-keyed FDA record by _id (the two collections share the key).
Resolves collections through the registry; the attach is the engine's. No
external calls.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_drugs_engine import (
    enrich_drugs_for_run)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    pm = reg.by_source_name("providermedicare")
    masters_coll = mongo[pm.public_data_db][reg.public_data_collection_name("providermedicare")]
    fda = reg.by_source_name("fda_drug")
    fda_drug_coll = mongo[fda.public_data_db][reg.public_data_collection_name("fda_drug")]

    result = enrich_drugs_for_run(
        run_id=ctx.run_id,
        masters_coll=masters_coll,
        fda_drug_coll=fda_drug_coll,
        record_discrepancy=rt.record_discrepancy,
    )
    _log.LogPipeline("INFO", "enrich_drugs: %s", result)
    return {"step": "enrich_drugs", **result}


def execute(ctx):
    return run_step(ctx)
