# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""clear_published_collections (EPIC-010-F-007-S-002-REQ-B-015) -- the annual
full replace.

Each year's construction fully replaces the published collections: no record
from a prior year is retained. Serial and before the base fan-out, because a
clear inside a state worker would wipe its peers' records.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    cleared = {}
    for source_name in ("providermedicare", "specialtymedicare"):
        entry = reg.by_source_name(source_name)
        coll = mongo[entry.public_data_db][
            reg.public_data_collection_name(source_name)]
        cleared[source_name] = coll.delete_many({}).deleted_count
    _log.LogPipeline("INFO", "clear_published_collections: %s", cleared)
    return {"step": "clear_published_collections", "cleared": cleared}


def execute(ctx):
    return run_step(ctx)
