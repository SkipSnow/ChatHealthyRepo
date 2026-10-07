# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_providermedicare_base (EPIC-010-F-007) — per-NPI ProviderMedicare doc.

Process-pool fanned by provider_state: each worker builds the ProviderMedicare
documents for its state from the Provider spine (entity_type, specialty) plus
Part B (procedures) and Part D (drugs, with day supply) staging. Resolves every
collection through the registry and delegates the assembly to the engine.

NOTE: the Provider spine collection is resolved at this run's data_version; if
the Provider pipeline was built at a different version, surface a
--provider-data-version arg (tracked). No external calls.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.run_lifecycle.steps._partitions import business_state_filter
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    build_providermedicare)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("provider_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message="build_providermedicare_base: expected ctx.config['partition']['provider_state']",
            component="build_providermedicare_base",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    prov = reg.by_source_name("provider")
    provider_spine_coll = mongo[prov.public_data_db][reg.public_data_collection_name("provider")]
    partb = reg.by_source_name("medicare_partb")
    partb_coll = mongo[partb.staging_db][reg.staging_collection_name("medicare_partb")]
    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    tgt = reg.by_source_name("providermedicare")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("providermedicare")]

    result = build_providermedicare(
        state=state,
        run_id=ctx.run_id,
        data_version=int(ctx.args.data_version),
        mongo=mongo,
        provider_spine_coll=provider_spine_coll,
        partb_coll=partb_coll,
        partd_coll=partd_coll,
        target_coll=target_coll,
        spine_state_filter=business_state_filter(state),
    )
    _log.LogPipeline("INFO", "build_providermedicare_base[%s]: %s", state, result)
    return result


def execute(ctx):
    return run_step(ctx)
