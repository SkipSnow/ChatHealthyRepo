# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_providermedicare_base (EPIC-010-F-007) — provider x molecule record.

Process-pool fanned by provider_state: each worker builds the flat
providerMedicarePartDQuantityRollUps documents (one per provider x molecule) for
its state from Part D staging, grouping each provider's rows by molecule and
joining the Provider spine (entity_type, NUCC) by NPI. Part B is out of this
collection's scope (best-effort, separate steps). Resolves every collection
through the registry and delegates the assembly to the engine.

NOTE: the Provider spine collection is named by the run's providerCollection arg
(cluster.db.collection); the built collection versions independently of the
Provider source. No external calls.
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
    state = partition.get("provider_state") if isinstance(partition, dict) else None
    entity_type = partition.get("entity_type") if isinstance(partition, dict) else None
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
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("providermedicare")]

    result = build_provider_molecule(
        state=state,
        run_id=ctx.run_id,
        data_version=int(ctx.args.data_version),
        provider_spine_coll=provider_spine_coll,
        partd_coll=partd_coll,
        target_coll=target_coll,
        entity_type=int(entity_type) if entity_type is not None else None,
    )
    _log.LogPipeline("INFO", "build_providermedicare_base[%s/type=%s]: %s",
                     state, entity_type, {k: v for k, v in result.items() if k != "not_found_npis"})
    # A billed/prescribing NPI absent from the Provider collection is a
    # non-fatal finding, never an abend -- recorded so the miss is visible on
    # the discrepancy report. De-duped: the engine joins by NPI and returns the
    # DISTINCT set of absent NPIs, so one NPI spanning many drug/procedure rows
    # is recorded exactly once, with the NPI itself as the record key.
    not_found = result.get("not_found_npis") or []
    for npi in not_found:
        rt.record_discrepancy(
            reason="FDA_NPI_not_found_In_Providers",
            step="build_providermedicare_base", artifact="provider",
            record_key=npi,
            detail={"provider_source": str(ctx.args.provider_collection)})
    return {k: v for k, v in result.items() if k != "not_found_npis"} | {
        "not_found_count": len(not_found)}


def execute(ctx):
    return run_step(ctx)
