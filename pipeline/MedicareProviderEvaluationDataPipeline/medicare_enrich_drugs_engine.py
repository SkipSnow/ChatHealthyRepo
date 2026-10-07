# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007).

Consumes the published Normalized Indication Map to place each ProviderMedicare
drug under its parent indication(s) and to roll the provider's own day supply up
per indication. The molecule index is built from the map's molecules[] (rxcui
and ingredient); a drug joins by rxcui when present, else by its generic name
against the ingredient. A drug indicated for several parents counts its day
supply under each (count-under-each). Only the provider's own values are written
here; the specialty benchmark is the rollup step's concern.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _load_indication_index(map_coll, run_id: str) -> tuple[dict, dict]:
    """Build (rxcui -> set(indication_id), ingredient_lower -> set(indication_id))."""
    by_rxcui: dict[str, set] = defaultdict(set)
    by_ingredient: dict[str, set] = defaultdict(set)
    count = 0
    for doc in map_coll.find({}, {"indication_id": 1, "molecules": 1}):
        iid = doc.get("indication_id") or doc.get("_id")
        if not iid:
            continue
        count += 1
        for m in doc.get("molecules") or []:
            rx = (m or {}).get("rxcui")
            ing = (m or {}).get("ingredient")
            if rx:
                by_rxcui[str(rx)].add(iid)
            if ing:
                by_ingredient[str(ing).strip().lower()].add(iid)
    if count == 0:
        raise ChatHealthyException(
            mode="config_error",
            component="enrich_drugs",
            message=("enrich_drugs: the Normalized Indication Map is empty at this "
                     "data_version; build/release it first (--build-indication-map)"),
        )
    return by_rxcui, by_ingredient


def enrich_drugs(
    *,
    state: str,
    run_id: str,
    providermedicare_coll,
    indication_map_coll,
    batch_size: int = 1000,
) -> dict[str, Any]:
    by_rxcui, by_ingredient = _load_indication_index(indication_map_coll, run_id)

    ops: list[UpdateOne] = []
    written = 0
    drugs_mapped = 0
    for doc in providermedicare_coll.find({"run_id": run_id, "provider_state": state}):
        drugs = doc.get("drugs") or []
        if not drugs:
            continue
        per_indication: dict[str, float] = defaultdict(float)
        changed = False
        for d in drugs:
            rx = d.get("rxcui")
            ing = (d.get("generic") or d.get("molecule") or "").strip().lower()
            iids: set = set()
            if rx and str(rx) in by_rxcui:
                iids |= by_rxcui[str(rx)]
            if ing and ing in by_ingredient:
                iids |= by_ingredient[ing]
            if iids:
                d["parent_indications"] = sorted(iids)
                drugs_mapped += 1
                changed = True
                ds = _num(d.get("days_supply"))
                if ds is not None:
                    for iid in iids:
                        per_indication[iid] += ds
        if per_indication:
            doc_inds = {
                i.get("indication_id"): i
                for i in (doc.get("indications") or [])
                if i.get("source") != "drug"
            }
            for iid, ds in per_indication.items():
                doc_inds[iid] = {"indication_id": iid, "source": "drug", "days_supply": round(ds, 4)}
            indications = list(doc_inds.values())
            changed = True
        else:
            indications = doc.get("indications") or []
        if not changed:
            continue
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"drugs": drugs, "indications": indications}},
        ))
        if len(ops) >= batch_size:
            providermedicare_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        providermedicare_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO", "enrich_drugs[%s]: docs_updated=%d drug_slots_mapped=%d",
        state, written, drugs_mapped,
    )
    return {"state": state, "providermedicare_updated": written, "drug_slots_mapped": drugs_mapped}
