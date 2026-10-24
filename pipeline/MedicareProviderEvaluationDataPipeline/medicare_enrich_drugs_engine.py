# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007, LLD v83 III.10) -- attach each molecule
MASTER's fda block from its molecule-keyed FDA record.

The FDA collection and the master share the key (_id = molecule_id, LLD v83
III.3 decision #7), so the attach is a point lookup per master: copy the FDA
record's assembled facts -- indications, drug_class SET, identity (rxcui/unii/
substance_name union), usage, trust aggregate -- onto the master's fda block.
Serial: ~1,700 masters, one pass, single-writer. A master with no FDA record
keeps its stub fda block and is recorded as a finding (non-fatal).

The normalized indication SET for the evaluation search is attached from the
NormalizedIndicationMap (EPIC-010-F-007-S-008, deferred); until it lands, the
master's fda.indications carries the FDA-label indications only.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_IDENTITY_FIELDS = ("rxcui", "unii", "substance_name")


def fda_block_from(fda_doc: dict) -> dict:
    """The master's fda block from the molecule's FDA record (LLD v83
    III.4.2): indications as assembled, drug_class as the SET, identity as the
    unioned rxcui/unii/substance_name, usage, and the trust aggregate."""
    molecule = fda_doc.get("molecule") or {}
    block: dict[str, Any] = {
        "indications": fda_doc.get("indications") or [],
        "drug_class": list((fda_doc.get("drug_class") or {}).get("classes") or []),
        "identity": {k: list(molecule.get(k) or []) for k in _IDENTITY_FIELDS},
        "trust": dict(fda_doc.get("trust") or
                      {"max": None, "min": None, "median": None, "avg": None}),
    }
    usage = (fda_doc.get("usage") or {}).get("summarized_usage")
    if usage:
        block["usage"] = usage
    return block


def enrich_drugs_for_run(
    *,
    masters_coll,
    fda_drug_coll,
    record_discrepancy=None,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Attach every ProviderMedicare record's fda block from the molecule its
    key names (serial pass). The record is keyed by NPI and molecule
    (REQ-B-001) and the FDA collection is keyed by molecule, so the join reads
    the record's molecule_id and point-looks-up the FDA document. One FDA
    document therefore serves every provider who prescribed that molecule, and
    is fetched once per molecule rather than once per record."""
    if fda_drug_coll.estimated_document_count() == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="enrich_drugs",
            message=("enrich_drugs: the FDA molecule collection is empty; "
                     "its prerequisite finalize_fda_molecules produced no "
                     "records to consume"))

    ops: list[UpdateOne] = []
    enriched = 0
    without_fda = 0
    blocks: dict[str, dict | None] = {}
    for master in masters_coll.find({}, {"_id": 1, "molecule_id": 1}):
        mid = master.get("molecule_id")
        if mid not in blocks:
            doc = fda_drug_coll.find_one({"_id": mid})
            blocks[mid] = fda_block_from(doc) if doc is not None else None
        block = blocks[mid]
        if block is None:
            without_fda += 1
            if record_discrepancy is not None:
                record_discrepancy(
                    reason="FDA_all_other_Attribute_failures",
                    step="enrich_drugs", artifact="providermedicare",
                    record_key=f"{master['_id'].get('npi')}:{mid}"
                    if isinstance(master.get("_id"), dict) else str(mid),
                    detail={"kind": "molecule_without_fda_record",
                            "note": "no FDA molecule record; the master keeps "
                                    "its stub fda block"})
            continue
        ops.append(UpdateOne({"_id": master["_id"]},
                             {"$set": {"fda": block}}))
        if len(ops) >= batch_size:
            masters_coll.bulk_write(ops, ordered=False)
            enriched += len(ops)
            ops = []
    if ops:
        masters_coll.bulk_write(ops, ordered=False)
        enriched += len(ops)

    _log.LogPipeline(
        "INFO", "enrich_drugs: records_enriched=%d without_fda=%d molecules=%d",
        enriched, without_fda, len(blocks))
    return {"records_enriched": enriched, "records_without_fda": without_fda,
            "molecules_seen": len(blocks)}
