# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""clear_and_index_providermedicare_and_specialtymedicare
(EPIC-010-F-007-S-002-REQ-B-015) -- the annual full replace of the two
collections this run rebuilds, and the indexes they need.

THE TWO IT CLEARS ARE NAMED, AND THE FDA COLLECTION IS NOT ONE OF THEM.
fdaProductToDiagnosisResolution costs about $100 of Google inference to
build and is published by its own leg; it is deliberately preserved, which
is what lets a re-run skip the entire FDA pass. The step's previous name,
clear_published_collections, claimed every published collection -- including
that one. It cleared only these two; the name was the defect, not the code.

Serial and before the base fan-out, because a clear inside a state worker
would wipe its peers' records.

The indexes are created here, on the empty collections, before the first
record is written. They cannot be declared by the deployment: the collection
does not exist when the chain deploys (operator ruling 2026-10-24), so the
pipeline creates the indexes its own steps need.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


INDEXES = {
    "providermedicare": [
        # The enrichment and the transient-attribute drop both fan out on
        # source_state and page by _id within it. The compound index makes each
        # page a seek rather than a scan of the whole collection.
        {"keys": [("source_state", 1), ("_id", 1)],
         "name": "source_state_1__id_1"},
    ],
}


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    cleared = {}
    indexed = {}
    for source_name in ("providermedicare", "specialtymedicare"):
        entry = reg.by_source_name(source_name)
        coll = mongo[entry.public_data_db][
            reg.public_data_collection_name(source_name)]
        cleared[source_name] = coll.delete_many({}).deleted_count
        # The collection does not exist when the chain deploys, so no
        # deployment can declare its indexes: the pipeline creates the indexes
        # its own steps need, here, serially, on an empty collection and
        # before the first record is written.
        for spec in INDEXES.get(source_name, []):
            indexed[spec["name"]] = coll.create_index(
                spec["keys"], name=spec["name"])
    _log.LogPipeline("INFO", "clear_and_index_providermedicare_and_specialtymedicare: cleared=%s indexed=%s",
                     cleared, indexed)
    return {"step": "clear_and_index_providermedicare_and_specialtymedicare", "cleared": cleared,
            "indexed": sorted(indexed)}


def execute(ctx):
    return run_step(ctx)
