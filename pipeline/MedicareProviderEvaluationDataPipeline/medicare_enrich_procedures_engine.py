# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_procedures engine (EPIC-010-F-007 / S-005).

Rule-based procedure coverage justification. The Medicare Coverage Database
carries, per billing/coding article, the HCPCS codes it governs and the ICD-10-CM
diagnoses it covers; joining the two tables by their shared article key yields,
for each HCPCS, the ICD-10-CM diagnoses it is covered for. Each billed procedure
on ProviderMedicare is stamped with that covered-diagnosis list and the coded
source. Column names are isolated in the *_KEYS tuples; first-fetch testing
corrects any drift in one place.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_POLICY_KEYS = ("article_id", "policy_id", "doc_id", "article", "contractor_assigned_article_id")
_HCPC_CODE_KEYS = ("hcpc_code", "hcpc", "hcpcs_code", "hcpcs")
_ICD10_CODE_KEYS = ("icd10_code", "icd_10_cm_code", "icd_10_code", "icd10", "icd")


def _norm_keys(raw: dict) -> dict:
    return {str(k).strip().strip("'").lower(): v for k, v in raw.items()}


def _first(low: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = low.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().strip("'").strip()
    return ""


def _rows(coll, run_id: str):
    for doc in coll.find({"run_id": run_id}):
        yield doc.get("raw") or {}


def build_hcpc_to_icd10(hcpc_coll, icd10_coll, run_id: str) -> dict[str, set]:
    """Join the coverage HCPCS and ICD-10 tables by article into HCPCS -> ICD-10."""
    article_icd10: dict[str, set] = defaultdict(set)
    for raw in _rows(icd10_coll, run_id):
        low = _norm_keys(raw)
        article = _first(low, _POLICY_KEYS)
        code = _first(low, _ICD10_CODE_KEYS)
        if article and code:
            article_icd10[article].add(code.upper().replace(".", ""))

    hcpc_to_icd10: dict[str, set] = defaultdict(set)
    for raw in _rows(hcpc_coll, run_id):
        low = _norm_keys(raw)
        article = _first(low, _POLICY_KEYS)
        hcpc = _first(low, _HCPC_CODE_KEYS)
        if hcpc and article in article_icd10:
            hcpc_to_icd10[hcpc.upper()] |= article_icd10[article]
    return hcpc_to_icd10


def enrich_procedures(
    *,
    state: str,
    run_id: str,
    providermedicare_coll,
    coverage_hcpc_coll,
    coverage_icd10_coll,
    batch_size: int = 1000,
) -> dict[str, Any]:
    hcpc_to_icd10 = build_hcpc_to_icd10(coverage_hcpc_coll, coverage_icd10_coll, run_id)

    ops: list[UpdateOne] = []
    written = 0
    procedures_justified = 0
    for doc in providermedicare_coll.find({"run_id": run_id, "provider_state": state}):
        procs = doc.get("procedures") or []
        if not procs:
            continue
        changed = False
        for p in procs:
            hc = str(p.get("hcpcs") or "").strip().upper()
            covered = sorted(hcpc_to_icd10.get(hc, set()))
            if covered:
                p["allowed_icd10"] = covered
                p["justification_kind"] = "coverage_db"
                p["justification_source"] = "medicare_coverage"
                procedures_justified += 1
                changed = True
            else:
                p["justification_kind"] = p.get("justification_kind") or "none"
        if not changed:
            continue
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"procedures": procs}}))
        if len(ops) >= batch_size:
            providermedicare_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        providermedicare_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "enrich_procedures[%s]: hcpcs_with_coverage=%d docs_updated=%d procedures_justified=%d",
        state, len(hcpc_to_icd10), written, procedures_justified,
    )
    return {
        "state": state,
        "hcpcs_with_coverage": len(hcpc_to_icd10),
        "providermedicare_updated": written,
        "procedures_justified": procedures_justified,
    }
