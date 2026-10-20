# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""finalize_fda_molecules (EPIC-010-F-007, LLD v83 III.10) -- the serial
molecule finalize, the second of the two formal FDA build steps (operator
ruling 2026-10-20).

Groups the per-label resolution results (the transient collection the fan-out
step wrote) by molecule_id and writes fdaProductToDiagnosisResolution at the
MOLECULE grain: one document per molecule, _id = molecule_id, carrying the
folded labels' set_ids (the contributing record keys) and the unioned FDA
facts (LLD v83 III.4.1). Single-writer and exactly-once by construction: the
group-by IS the fold, and the write is a deterministic full-document upsert.

Full annual replace (decision #1): this data_version's published collection is
cleared before writing, so no residue from prior or aborted runs survives.

A label whose molecule cannot be minted (no clean name recovered) cannot fold
into any molecule: its transient row is stamped orphan: true with the reason
and a finding is recorded -- NON-FATAL per hard-fail ruling H2; the
conformance step reports the arithmetic.
"""

from __future__ import annotations

import datetime

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    fda_molecule_union, molecule_id)
from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (
    TRANSIENT_COLLECTION)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    data_version = int(ctx.args.data_version)

    tgt = reg.by_source_name("fda_drug")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("fda_drug")]

    building = bool(ctx.args.build_fda_drug)
    if not building:
        published = target_coll.count_documents({})
        if published == 0:
            raise ChatHealthyException(
                mode="runtime_error",
                component="finalize_fda_molecules",
                message=("finalize_fda_molecules: the run did not ask to build the "
                         "fdaProductToDiagnosisResolution collection and none is published "
                         "to consume; fire with --build-fda-drug to build it"))
        _log.LogPipeline(
            "INFO", "finalize_fda_molecules: consume mode; published molecules=%d",
            published)
        return {"step": "finalize_fda_molecules", "rebuild": False, "consumed": published}

    ofda = reg.by_source_name("openfda_labels")
    transient_coll = mongo[ofda.staging_db][TRANSIENT_COLLECTION]

    # Group the run's label results by molecule_id. Labels whose molecule
    # cannot be minted are orphans: stamped in the transient (operationally
    # queryable, H2) and recorded as findings -- never a run kill.
    by_molecule: dict[str, list[dict]] = {}
    orphans = 0
    labels = 0
    for lr in transient_coll.find({"run_id": ctx.run_id}):
        labels += 1
        mid = molecule_id(lr.get("molecule"))
        if not mid:
            orphans += 1
            transient_coll.update_one(
                {"_id": lr["_id"]},
                {"$set": {"orphan": True,
                          "orphan_reason": "no_molecule_id: no clean molecule "
                                           "name was recovered for this label"}})
            rt.record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="finalize_fda_molecules", artifact="fda",
                record_key=str(lr.get("set_id") or lr.get("_id") or ""),
                detail={"kind": "label_orphaned_no_molecule"})
            continue
        by_molecule.setdefault(mid, []).append(lr)

    # Full annual replace (decision #1): clear this data_version's collection.
    removed = target_coll.delete_many({}).deleted_count

    built_at = datetime.datetime.utcnow().isoformat()
    written = 0
    for mid, group in by_molecule.items():
        doc = fda_molecule_union(group)
        doc["_id"] = mid
        doc["molecule_id"] = mid
        doc["run_id"] = ctx.run_id
        doc["data_version"] = data_version
        doc["provenance"] = {
            "source_run_id": ctx.run_id,
            "source_collection": "StagingOpenFdaLabels_v_1",
            "built_at": built_at,
        }
        # Deterministic full-document upsert: the finalize is serial, so every
        # published write is single-writer; a re-run re-derives byte-identical
        # documents.
        target_coll.replace_one({"_id": mid}, doc, upsert=True)
        written += 1

    folded = labels - orphans
    _log.LogPipeline(
        "INFO",
        "finalize_fda_molecules: labels=%d folded=%d orphans=%d molecules=%d "
        "cleared=%d run_id=%s",
        labels, folded, orphans, written, removed, ctx.run_id)
    return {"step": "finalize_fda_molecules",
            "labels": labels, "labels_folded": folded, "labels_orphaned": orphans,
            "molecules_written": written, "cleared_prior_docs": removed}


def execute(ctx):
    return run_step(ctx)
