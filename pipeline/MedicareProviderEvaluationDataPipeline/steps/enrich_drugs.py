# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs (EPIC-010-F-007) — place drugs under indications, roll day supply.

Fanned by provider_state: each worker reads the published Normalized Indication
Map and, for every ProviderMedicare record in its state, stamps each drug's
parent indication(s) and rolls the provider's own day supply up per indication
(count-under-each). Resolves collections through the registry; the aggregation
is the engine's. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_drugs_engine import enrich_drugs

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("provider_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message="enrich_drugs: expected ctx.config['partition']['provider_state']",
            component="enrich_drugs",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    pm = reg.by_source_name("providermedicare")
    providermedicare_coll = mongo[pm.public_data_db][reg.public_data_collection_name("providermedicare")]
    im = reg.by_source_name("indication_map")
    indication_map_coll = mongo[im.public_data_db][reg.public_data_collection_name("indication_map")]

    result = enrich_drugs(
        state=state,
        run_id=ctx.run_id,
        providermedicare_coll=providermedicare_coll,
        indication_map_coll=indication_map_coll,
    )
    _log.LogPipeline("INFO", "enrich_drugs[%s]: %s", state, result)
    return result


def execute(ctx):
    return run_step(ctx)
