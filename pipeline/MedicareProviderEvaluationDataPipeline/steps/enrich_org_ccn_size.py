# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_org_ccn_size (EPIC-010-F-007) — stamp organizational CCN.

Fanned by provider_state: each worker stamps the CCN (mined from the provider
spine's Medicare OSCAR / certification identifier) onto its state's
organizational ProviderMedicare records. Resolves collections through the
registry; the mine is the engine's. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_org_engine import enrich_org_ccn_size

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("provider_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message="enrich_org_ccn_size: expected ctx.config['partition']['provider_state']",
            component="enrich_org_ccn_size",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    pm = reg.by_source_name("providermedicare")
    providermedicare_coll = mongo[pm.public_data_db][reg.public_data_collection_name("providermedicare")]
    prov = reg.by_source_name("provider")
    provider_spine_coll = mongo[prov.public_data_db][reg.public_data_collection_name("provider")]
    pos = reg.by_source_name("medicare_pos")
    pos_coll = mongo[pos.staging_db][reg.staging_collection_name("medicare_pos")]

    result = enrich_org_ccn_size(
        state=state,
        run_id=ctx.run_id,
        providermedicare_coll=providermedicare_coll,
        provider_spine_coll=provider_spine_coll,
        pos_coll=pos_coll,
    )
    _log.LogPipeline("INFO", "enrich_org_ccn_size[%s]: %s", state, result)
    return result


def execute(ctx):
    return run_step(ctx)
