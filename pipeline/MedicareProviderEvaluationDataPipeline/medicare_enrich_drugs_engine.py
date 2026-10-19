# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007) -- attach molecule FDA facts.

In the provider-core model each document is one provider x molecule and already
carries molecule_id (minted by the base build from the CMS Gnrc_Name). This step
attaches the molecule's FDA facts by querying the FDA file (fdaProductToDiagnosis-
Resolution) for that molecule's labels (by molecule_id) and aggregating them:
  - drug_class : the SET of classes across the molecule's labels (a molecule can
                 carry both OTC and Rx labels).
  - identity   : rxcui / unii / substance_name unioned across the labels.
No brand resolution happens here any more -- the base build grouped CMS rows to
the molecule directly, so the join is molecule_id -> molecule_id, not brand.

Fanned by provider_state; a per-worker cache keyed by molecule_id avoids
re-querying a molecule the state prescribes many times. Memory-bounded: the FDA
file is never loaded whole; only a molecule's handful of labels are read on
first use.

The normalized indication SET that the (indications, npi, ...) search index keys
on is attached from the NormalizedIndicationMap (EPIC-010-F-007-S-008) by
molecule_id; that collection is its own build and is NOT read here. Until it
lands the indication set stays as the base build left it (empty).
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

# Only the fields the molecule-facts aggregation needs are projected off each
# matched FDA label record -- never the whole document.
_FDA_PROJECTION = {"drug_class": 1, "openfda": 1}
_IDENTITY_FIELDS = ("rxcui", "unii", "substance_name")


def _aggregate_molecule_facts(records: list[dict]) -> dict:
    """Aggregate one molecule's FDA label records into the facts carried on every
    provider x molecule document for that molecule: the drug_class SET and the
    unioned identity. First-seen order, de-duplicated."""
    classes: list[str] = []
    cls_seen: set = set()
    identity: dict[str, list[str]] = {k: [] for k in _IDENTITY_FIELDS}
    id_seen: dict[str, set] = {k: set() for k in _IDENTITY_FIELDS}
    for doc in records:
        cls = str((doc.get("drug_class") or {}).get("class") or "").strip()
        if cls and cls not in cls_seen:
            cls_seen.add(cls)
            classes.append(cls)
        openfda = doc.get("openfda") or {}
        for field in _IDENTITY_FIELDS:
            vals = openfda.get(field) or []
            if isinstance(vals, str):
                vals = [vals]
            for value in vals:
                text = str(value).strip()
                if text and text not in id_seen[field]:
                    id_seen[field].add(text)
                    identity[field].append(text)
    return {"drug_class": classes, "identity": identity}


def enrich_drugs(
    *,
    state: str,
    run_id: str,
    providermedicare_coll,
    fda_drug_coll,
    runtime,
    batch_size: int = 1000,
) -> dict[str, Any]:
    if fda_drug_coll.estimated_document_count() == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="enrich_drugs",
            message=("enrich_drugs: the fdaProductToDiagnosisResolution collection is empty; "
                     "its prerequisite build_fda_drug_collection produced no records to consume"),
        )

    cache: dict[str, dict] = {}

    def facts(molecule_id: str) -> dict:
        if molecule_id in cache:
            return cache[molecule_id]
        records = list(fda_drug_coll.find({"molecule_id": molecule_id}, _FDA_PROJECTION))
        resolved = _aggregate_molecule_facts(records)
        cache[molecule_id] = resolved
        return resolved

    ops: list[UpdateOne] = []
    written = 0
    no_fda_molecule = 0
    for doc in providermedicare_coll.find({"run_id": run_id, "prescriber_state": state}):
        mid = doc.get("molecule_id")
        resolved = facts(mid) if mid else {"drug_class": [], "identity": {}}
        if not resolved["drug_class"]:
            no_fda_molecule += 1
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"drug_class": resolved["drug_class"], "identity": resolved["identity"]}}))
        if len(ops) >= batch_size:
            providermedicare_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        providermedicare_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO", "enrich_drugs[%s]: updated=%d no_fda_molecule=%d",
        state, written, no_fda_molecule,
    )
    return {
        "state": state,
        "providermedicare_updated": written,
        "no_fda_molecule": no_fda_molecule,
    }
