# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_org_ccn_size engine (EPIC-010-F-007 / S-007).

For each organizational ProviderMedicare record:
  * CCN -- mined from the provider spine's Medicare OSCAR / certification
    other-identifier slot (deterministic, no new acquisition).
  * size_tier -- a coarse institution size, derived as a data-driven quintile
    band (the same five-band mechanism as the day-supply tiers) of the certified
    bed count read from the CMS Provider-of-Services file, keyed by CCN. Bed
    counts are an inpatient concept; an ambulatory / no-bed facility carries a
    null size_tier rather than a fabricated one.

POS column names are isolated in the *_KEYS tuples; first-fetch testing corrects
drift in one place.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_CCN_TYPE_CODES = {"06"}
_CCN_TEXT = ("OSCAR", "CERTIFICATION", "CCN")

_POS_CCN_KEYS = ("prvdr_num", "ccn", "provider_number", "prvdr_num_id")
_POS_BED_KEYS = ("crtfd_bed_cnt", "certified_bed_count", "bed_cnt", "gen_bed_cnt", "num_beds")


def _norm_keys(raw: dict) -> dict:
    return {str(k).strip().strip("'").lower(): v for k, v in raw.items()}


def _first(low: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = low.get(k)
        if v is not None and str(v).strip():
            return str(v).strip().strip("'").strip()
    return ""


def _to_int(v: str):
    try:
        f = float(str(v).replace(",", ""))
        return int(f)
    except (TypeError, ValueError):
        return None


def _extract_ccn(spine: dict) -> str | None:
    for oid in spine.get("other_identifiers") or []:
        if not isinstance(oid, dict):
            continue
        code = str(oid.get("type_code") or "").strip()
        text = f"{oid.get('type_description') or ''} {oid.get('issuer') or ''}".upper()
        ident = str(oid.get("identifier") or "").strip()
        if not ident:
            continue
        if code in _CCN_TYPE_CODES or any(t in text for t in _CCN_TEXT):
            return ident
    return None


def _percentile(sorted_vals: list[int], p: float) -> float:
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


def _build_pos_index(pos_coll, run_id: str) -> tuple[dict[str, int], list[float]]:
    """CCN -> certified bed count, plus the four quintile cut-points of bed count."""
    ccn_beds: dict[str, int] = {}
    for doc in pos_coll.find({"run_id": run_id}):
        low = _norm_keys(doc.get("raw") or {})
        ccn = _first(low, _POS_CCN_KEYS)
        beds = _to_int(_first(low, _POS_BED_KEYS))
        if ccn and beds is not None:
            ccn_beds[ccn] = max(ccn_beds.get(ccn, 0), beds)
    bedded = sorted(b for b in ccn_beds.values() if b > 0)
    cutpoints = [_percentile(bedded, p) for p in (0.2, 0.4, 0.6, 0.8)] if bedded else []
    return ccn_beds, cutpoints


def _band(beds: int | None, cutpoints: list[float]) -> int | None:
    if not beds or beds <= 0 or not cutpoints:
        return None
    for i, cut in enumerate(cutpoints):
        if beds <= cut:
            return i + 1
    return 5


def enrich_org_ccn_size(
    *,
    state: str,
    run_id: str,
    providermedicare_coll,
    provider_spine_coll,
    pos_coll,
    batch_size: int = 1000,
) -> dict[str, Any]:
    ccn_beds, cutpoints = _build_pos_index(pos_coll, run_id)

    ops: list[UpdateOne] = []
    written = 0
    orgs = 0
    sized = 0
    for pm in providermedicare_coll.find(
            {"run_id": run_id, "provider_state": state, "entity_type": "O"}):
        orgs += 1
        npi = pm["_id"]
        spine = provider_spine_coll.find_one({"npi": npi}) or \
            provider_spine_coll.find_one({"_id": npi}) or {}
        ccn = _extract_ccn(spine)
        setter: dict[str, Any] = {}
        if ccn:
            setter["ccn"] = ccn
            tier = _band(ccn_beds.get(ccn), cutpoints)
            if tier is not None:
                setter["size_tier"] = tier
                sized += 1
        if not setter:
            continue
        ops.append(UpdateOne({"_id": npi}, {"$set": setter}))
        if len(ops) >= batch_size:
            providermedicare_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        providermedicare_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "enrich_org_ccn_size[%s]: orgs=%d updated=%d size_tiered=%d pos_ccns=%d",
        state, orgs, written, sized, len(ccn_beds),
    )
    return {"state": state, "orgs": orgs, "updated": written, "size_tiered": sized}
