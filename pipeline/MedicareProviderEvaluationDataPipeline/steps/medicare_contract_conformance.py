# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_contract_conformance (EPIC-010-F-007) — gate on record contracts.

Serial, after the collections are built. Validates every published
ProviderMedicare, SpecialtyMedicare and Normalized Indication Map record against
its JSON Schema; any nonconformance stops the run. Resolves collections through
the registry. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_conformance_engine import check_conformance

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    collections = {}
    for source_name in ("providermedicare", "specialtymedicare", "indication_map"):
        entry = reg.by_source_name(source_name)
        collections[source_name] = mongo[entry.public_data_db][
            reg.public_data_collection_name(source_name)]

    result = check_conformance(run_id=ctx.run_id, collections=collections)
    _log.LogPipeline("INFO", "medicare_contract_conformance: %s",
                     {k: v["checked"] for k, v in result["report"].items()})
    return result


def execute(ctx):
    return run_step(ctx)
