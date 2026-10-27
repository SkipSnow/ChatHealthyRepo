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

AND THE CONTRACT IS INSTALLED HERE TOO (LLD I.3.12). Conformance is enforced
by the store, not by the callers: each collection is recreated carrying its
published JSON Schema as the collection's own MongoDB validator, at
validationAction "error" and validationLevel "strict", so the database refuses
a write that does not conform. This is the only place either collection is
created, which is what makes it the place the contract is installed.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    PUBLISHED_CONTRACTS, validator_for)
from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


# Every index each published collection needs, with the query that needs it.
# A collection of 26.4M records has no safe unindexed predicate: a query
# without one is a full scan, and a measurement taken from the first N
# documents in natural order is not a measurement of the collection.
INDEXES = {
    "providermedicare": [
        # The enrichment and the transient-attribute drop both fan out on
        # source_state and page by _id within it. The compound index makes each
        # page a seek rather than a scan of the whole collection.
        {"keys": [("source_state", 1), ("_id", 1)],
         "name": "source_state_1__id_1"},
        # The molecule: the specialty rollup's fan-out population, the FDA
        # lookup during enrichment, and every by-molecule question asked of
        # the collection afterwards.
        {"keys": [("molecule_id", 1), ("_id", 1)],
         "name": "molecule_id_1__id_1"},
        # The external identity the children join on. rxcui_in reaches CMS;
        # unii is the immutable one.
        {"keys": [("rxcui_in", 1)], "name": "rxcui_in_1"},
        {"keys": [("unii", 1)], "name": "unii_1"},
        # One provider's records: the detail panel and every per-NPI question.
        {"keys": [("npi", 1)], "name": "npi_1"},
        # Specialty filtering. Multikey: a provider carries several codes.
        {"keys": [("nucc_codes", 1)], "name": "nucc_codes_1"},
        # The product question -- given an indication, which providers treat
        # it. Multikey on the diagnosis codes the indications carry.
        {"keys": [("indications.icd10.level3", 1)],
         "name": "indications_icd10_level3_1"},
        {"keys": [("indications.icd10.level4", 1)],
         "name": "indications_icd10_level4_1"},
        # The provider's one billing state: the scope the drop settles on and
        # the geography every downstream question filters by.
        {"keys": [("billing_state", 1)], "name": "billing_state_1"},
    ],
    "specialtymedicare": [
        # 717 documents keyed by NUCC code, so _id answers the lookup. The
        # molecule entries are a map, and a map's keys cannot be indexed
        # generically; nothing queries inside them.
    ],
}


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    cleared = {}
    indexed = {}
    enforced = []
    for source_name in ("providermedicare", "specialtymedicare"):
        entry = reg.by_source_name(source_name)
        db = mongo[entry.public_data_db]
        coll_name = reg.public_data_collection_name(source_name)
        coll = db[coll_name]
        # Read the contract BEFORE the drop. A contract this translation
        # cannot restate faithfully stops the step with the collection still
        # standing, rather than after the drop has already destroyed it.
        validator = validator_for(source_name)
        # DROP, not delete_many({}). An annual full replace keeps no record,
        # so there is nothing to delete one document at a time: the first
        # version of this step deleted row by row and took 24 minutes to clear
        # 4.3M of 26.4M records, blowing the step's 1800s timeout and failing
        # the run of 2026-10-24 20:35Z. A drop is one metadata operation.
        cleared[source_name] = coll.estimated_document_count()
        coll.drop()
        # Recreate the namespace EXPLICITLY, carrying the contract, rather
        # than letting the first index or the first insert create it bare: a
        # validator added after records exist governs nothing already written.
        db.create_collection(
            coll_name,
            validator=validator,
            validationAction="error",
            validationLevel="strict",
        )
        enforced.append(f"{source_name}={PUBLISHED_CONTRACTS[source_name]}")
        # The collection does not exist when the chain deploys, so no
        # deployment can declare its indexes: the pipeline creates the indexes
        # its own steps need, here, serially, on a collection the drop just
        # emptied and before the first record is written.
        for spec in INDEXES.get(source_name, []):
            indexed[spec["name"]] = coll.create_index(
                spec["keys"], name=spec["name"])
    _log.LogPipeline("INFO", "clear_and_index_providermedicare_and_specialtymedicare: cleared=%s indexed=%s contracts_installed=%s",
                     cleared, indexed, enforced)
    return {"step": "clear_and_index_providermedicare_and_specialtymedicare", "cleared": cleared,
            "indexed": sorted(indexed), "contracts_installed": sorted(enforced)}


def execute(ctx):
    return run_step(ctx)
