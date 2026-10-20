# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_providermedicare_base (EPIC-010-F-007, LLD v83 III.10) — the master
apply fan-out.

Process-pool fanned by partition = (billing/business state x entity type,
LLD v83 III.3); the pre-pass (prepare_molecule_masters) stamped every staged
Part D row with its partition, so each worker's read is an indexed
find({run_id, partition_state, partition_entity}). The worker groups its rows
per molecule and applies ONE ATOMIC UPDATE per (molecule, partition) — raw
children or the state-aggregate child plus the $inc of the master's totals —
gated by the applied_partitions marker: a contribution already applied is
disregarded silently. Merge-only; never a replace, never a delete.

NPIs absent from the Provider spine were stamped to the catch-all partition
and recorded by the pre-pass; this step records nothing new about them.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    build_provider_molecule)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("partition_state") if isinstance(partition, dict) else None
    entity_type = partition.get("entity_type") if isinstance(partition, dict) else None
    if not state or entity_type is None:
        raise ChatHealthyException(
            mode="value_error",
            message=("build_providermedicare_base: expected ctx.config['partition'] "
                     "to carry partition_state and entity_type"),
            component="build_providermedicare_base",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    # The Provider spine is named explicitly by the run's providerCollection arg
    # (cluster.db.collection), not derived from this run's data_version -- the
    # built collections version independently of the Provider source.
    from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (
        parse_provider_source)
    _prov_db, _prov_coll = parse_provider_source(ctx.args.provider_collection)
    provider_spine_coll = mongo[_prov_db][_prov_coll]
    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    tgt = reg.by_source_name("providermedicare")
    master_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("providermedicare")]

    result = build_provider_molecule(
        state=str(state),
        entity_type=int(entity_type),
        run_id=ctx.run_id,
        provider_spine_coll=provider_spine_coll,
        partd_coll=partd_coll,
        master_coll=master_coll,
    )
    _log.LogPipeline("INFO", "build_providermedicare_base[%s]: %s",
                     result.get("partition"), result)
    return result


def execute(ctx):
    return run_step(ctx)
