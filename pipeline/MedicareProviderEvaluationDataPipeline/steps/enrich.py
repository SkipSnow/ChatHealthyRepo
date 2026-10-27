# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich (EPIC-010-F-007-S-002) -- one enrichment pass per source-state
partition.

Reads the base collection and attaches everything that comes from outside the
CMS file: the Provider collection's billing state, entity type, NUCC codes,
and an organization's CCN and size tier; the FDA molecule collection's block
and indications. One sweep of the partition, not one per fact.

Fans out on source_state, the key the base task wrote; the final
DropTransientAttributes task removes that key once enrichment is done.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_engine import (
    enrich_partition)
from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (
    parse_provider_source)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("source_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message="enrich: expected ctx.config['partition']['source_state']",
            component="enrich",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    tgt = reg.by_source_name("providermedicare")
    target_coll = mongo[tgt.public_data_db][
        reg.public_data_collection_name("providermedicare")]
    fda = reg.by_source_name("fda_drug")
    fda_coll = mongo[fda.public_data_db][
        reg.public_data_collection_name("fda_drug")]
    pos = reg.by_source_name("medicare_pos")
    pos_coll = mongo[pos.staging_db][reg.staging_collection_name("medicare_pos")]
    prov_db, prov_coll_name = parse_provider_source(ctx.args.provider_collection)
    provider_coll = mongo[prov_db][prov_coll_name]

    result = enrich_partition(
        state=str(state),
        target_coll=target_coll,
        provider_coll=provider_coll,
        fda_coll=fda_coll,
        pos_coll=pos_coll,
        record_discrepancy=rt.record_discrepancy,
    )
    _log.LogPipeline("INFO", "enrich[%s]: %s", state, result)
    return result


def execute(ctx):
    return run_step(ctx)
