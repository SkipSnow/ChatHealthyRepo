# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007).

Consumes the fdaProductToDiagnosisResolution collection produced this run by the
build_fda_drug_collection prerequisite to (a) resolve each ProviderMedicare
drug's RXCUI/molecule, (b) RE-GRAIN the raw Part D drug rows to one entry per
RXCUI (brand+generic collapsed, measures summed), and (c) place each drug under
its parent indication(s), rolling the provider's own day supply up per
indication (count-under-each).

The molecule index is built from the map's molecules[] (rxcui and ingredient):
a drug resolves its RXCUI/molecule from its generic name against the ingredient,
and joins an indication by rxcui when present, else by generic against the
ingredient. The RXCUI re-grain itself is the pure function
medicare_normalization.regrain_drugs_by_rxcui; this engine resolves RXCUI (which
lives here, not in the base build) and drives it. Only the provider's own values
are written here; the specialty benchmark is the rollup step's concern.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    regrain_drugs_by_rxcui,
)

_log = ChatHealthyLoggingService()


def _num(v: Any) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _load_indication_index(map_coll, run_id: str) -> tuple[dict, dict, dict]:
    """Build the three indices the enrich pass needs from the FDA drug map:
      * by_rxcui:       rxcui -> set(indication_id)
      * by_ingredient:  ingredient_lower -> set(indication_id)
      * ingredient_rxcui: ingredient_lower -> (rxcui, ingredient_name)
    The last is how a Part D drug (which carries no RXCUI) resolves its RXCUI
    and molecule from its generic name -- RXCUI resolution lives here.
    """
    by_rxcui: dict[str, set] = defaultdict(set)
    by_ingredient: dict[str, set] = defaultdict(set)
    ingredient_rxcui: dict[str, tuple[str, str]] = {}
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
                ing_key = str(ing).strip().lower()
                by_ingredient[ing_key].add(iid)
                if rx and ing_key not in ingredient_rxcui:
                    ingredient_rxcui[ing_key] = (str(rx), str(ing).strip())
    if count == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="enrich_drugs",
            message=("enrich_drugs: the fdaProductToDiagnosisResolution collection is empty "
                     "for this run; its prerequisite build_fda_drug_collection step "
                     "produced no records to consume"),
        )
    return by_rxcui, by_ingredient, ingredient_rxcui


def _resolve_rxcui(drug: dict, ingredient_rxcui: dict[str, tuple[str, str]]) -> None:
    """Stamp rxcui/molecule onto a raw drug from its generic name, in place."""
    if drug.get("rxcui"):
        return
    gen = str(drug.get("generic") or "").strip().lower()
    hit = ingredient_rxcui.get(gen)
    if hit:
        drug["rxcui"], molecule = hit[0], hit[1]
        if not drug.get("molecule"):
            drug["molecule"] = molecule


def _map_indications(drug: dict, by_rxcui: dict, by_ingredient: dict) -> set:
    iids: set = set()
    rx = drug.get("rxcui")
    ing = str(drug.get("generic") or drug.get("molecule") or "").strip().lower()
    if rx and str(rx) in by_rxcui:
        iids |= by_rxcui[str(rx)]
    if ing and ing in by_ingredient:
        iids |= by_ingredient[ing]
    return iids


def enrich_drugs(
    *,
    state: str,
    run_id: str,
    providermedicare_coll,
    indication_map_coll,
    batch_size: int = 1000,
) -> dict[str, Any]:
    by_rxcui, by_ingredient, ingredient_rxcui = _load_indication_index(
        indication_map_coll, run_id)

    ops: list[UpdateOne] = []
    written = 0
    drugs_mapped = 0
    for doc in providermedicare_coll.find({"run_id": run_id, "provider_state": state}):
        drugs = doc.get("drugs") or []
        if not drugs:
            continue
        # 1) Resolve RXCUI/molecule and the parent indication(s) for every raw row.
        for d in drugs:
            _resolve_rxcui(d, ingredient_rxcui)
            iids = _map_indications(d, by_rxcui, by_ingredient)
            if iids:
                d["parent_indications"] = sorted(iids)
                drugs_mapped += 1
        # 2) Re-grain the raw brand/generic rows to one entry per RXCUI.
        regrained = regrain_drugs_by_rxcui(drugs)
        # 3) Roll the provider's own day supply up per indication, over the
        #    re-grained drugs (count-under-each).
        per_indication: dict[str, float] = defaultdict(float)
        for d in regrained:
            ds = _num(d.get("days_supply"))
            if ds is None:
                continue
            for iid in d.get("parent_indications") or []:
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
        else:
            indications = doc.get("indications") or []
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": {"drugs": regrained, "indications": indications}},
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
