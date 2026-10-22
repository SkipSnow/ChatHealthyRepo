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

Molecule identity is enriched by a LOCAL join against the staged RxNorm
vocabulary (LLD v83 III.9/III.10): the run's staged RXNCONSO ingredient rows
(TTY IN/PIN) are folded by the same molecule_id normalization and their RXCUIs
unioned into molecule.rxcui. No external terminology API is called at run time
-- the former RxNav usage was an off-design bug, removed (operator ruling
2026-10-20).
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

# RXNCONSO term types that name an ingredient: IN = ingredient, PIN = precise
# ingredient. Only these rows carry molecule-grain identity; every other TTY
# (brand, clinical drug, pack...) is a different grain and is not joined.
_RXNORM_INGREDIENT_TTYS = ("IN", "PIN")


def _rxnorm_rxcui_by_molecule(ctx, reg, mongo) -> dict[str, list[str]]:
    """The staged RxNorm vocabulary folded to the pipeline's molecule grain:
    molecule_id(STR) -> first-seen-ordered RXCUI list, from the staged
    RXNCONSO ingredient rows (TTY IN/PIN). A LOCAL join -- the vocabulary is a
    declared, fetched, staged source like every other (LLD v83 III.9). An
    empty staging in build mode is a missing source: hard fail (H6)."""
    rx = reg.by_source_name("rxnorm")
    coll = mongo[rx.staging_db][reg.staging_collection_name("rxnorm")]
    by_mid: dict[str, list[str]] = {}
    rows = 0
    # No run_id filter: the drained-and-replaced staging is the one truth
    # (Start At, operator design 2026-10-22).
    cursor = coll.find(
        {"TTY": {"$in": list(_RXNORM_INGREDIENT_TTYS)}},
        {"RXCUI": 1, "STR": 1})
    for row in cursor:
        rows += 1
        mid = molecule_id(row.get("STR"))
        rxcui = str(row.get("RXCUI") or "").strip()
        if not mid or not rxcui:
            continue
        dest = by_mid.setdefault(mid, [])
        if rxcui not in dest:
            dest.append(rxcui)
    if rows == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="finalize_fda_molecules",
            message=("finalize_fda_molecules: the staged RxNorm vocabulary holds "
                     "zero ingredient rows (TTY IN/PIN) -- the rxnorm source is "
                     "declared and required; missing source is a hard fail (H6)"))
    _log.LogPipeline(
        "INFO", "finalize_fda_molecules: rxnorm vocabulary rows=%d molecules=%d",
        rows, len(by_mid))
    return by_mid


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
    for lr in transient_coll.find({}):
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

    # The LOCAL RxNorm identity join (LLD v83 III.9/III.10): the staged
    # vocabulary's ingredient RXCUIs, folded by the same molecule_id.
    rxnorm_by_mid = _rxnorm_rxcui_by_molecule(ctx, reg, mongo)

    # Full annual replace (decision #1): clear this data_version's collection.
    removed = target_coll.delete_many({}).deleted_count

    built_at = datetime.datetime.utcnow().isoformat()
    written = 0
    rxnorm_joined = 0
    for mid, group in by_molecule.items():
        doc = fda_molecule_union(group)
        joined = rxnorm_by_mid.get(mid)
        if joined:
            rxnorm_joined += 1
            have = set(doc["molecule"]["rxcui"])
            doc["molecule"]["rxcui"].extend(
                c for c in joined if c not in have)
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

    # Record-declared indexes on the published collection (LLD v83 III.8):
    # the brand/generic key multikey lookup indexes. A version_source
    # declaration the run applies -- after the fold, so the build never
    # fights index maintenance.
    from pipeline.run_lifecycle.pipeline_index_catalog import (  # noqa: PLC0415
        apply_runtime_index_package)
    idx = apply_runtime_index_package(mongo, "medicare_fda_indexes", data_version)
    _log.LogPipeline("INFO", "finalize_fda_molecules: index package "
                     "medicare_fda_indexes -> %s", idx)

    folded = labels - orphans
    _log.LogPipeline(
        "INFO",
        "finalize_fda_molecules: labels=%d folded=%d orphans=%d molecules=%d "
        "rxnorm_joined=%d cleared=%d run_id=%s",
        labels, folded, orphans, written, rxnorm_joined, removed, ctx.run_id)
    return {"step": "finalize_fda_molecules",
            "labels": labels, "labels_folded": folded, "labels_orphaned": orphans,
            "molecules_written": written, "molecules_rxnorm_joined": rxnorm_joined,
            "cleared_prior_docs": removed}


def execute(ctx):
    return run_step(ctx)
