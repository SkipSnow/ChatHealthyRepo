# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CMS_NPI_Prescriber_DropTransientAttributes (EPIC-010-F-007-S-002) -- removes
the attributes that existed only to partition the work.

source_state is the CMS state the base task read a record's rows from, and
contributed_by is the set of units whose contributions the record carries.
Both are scaffolding: the first is the fan-out key for the base and the
enrichment steps, the second is what makes a re-driven unit a no-op. Neither is
a fact about the provider or the molecule, and no requirement asks the served
collection to carry them.

This task runs LAST, after enrichment and after the audits: dropping the key
earlier would take away the criterion every later step fans out on, and the
audit still needs to recompute a sampled record against the rows it was built
from.

It fans out on source_state like the steps before it -- the key is removed by
the same worker that reads it, within one partition.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()

TRANSIENT_ATTRIBUTES = ("source_state", "contributed_by")


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("source_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message=("CMS_NPI_Prescriber_DropTransientAttributes: expected "
                     "ctx.config['partition']['source_state']"),
            component="drop_transient_attributes",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    tgt = reg.by_source_name("providermedicare")
    target_coll = mongo[tgt.public_data_db][
        reg.public_data_collection_name("providermedicare")]

    res = target_coll.update_many(
        {"source_state": str(state)},
        {"$unset": {attr: "" for attr in TRANSIENT_ATTRIBUTES}})
    result = {"partition": str(state), "records_stripped": res.modified_count}
    _log.LogPipeline(
        "INFO",
        "CMS_NPI_Prescriber_DropTransientAttributes[%s]: stripped=%d",
        state, res.modified_count)
    return result


def execute(ctx):
    return run_step(ctx)
