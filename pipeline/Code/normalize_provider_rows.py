# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""normalize_provider_rows — Step 5 sub-orchestration and worker.

Step 4 (raw load) writes each provider as
    {npi, load_id, record_id, worker_id, raw: { <original-NPPES-column>: value }}

Step 5 (this file) reads every such doc whose `raw` subdocument is still
present, runs the same NPPES normalization that the previous monolithic load
worker performed (taxonomies, licenses, other_identifiers, practice_address
list, mailing_address), sets the normalized fields on the doc, and unsets
`raw`. The unset doubles as the idempotency guard — re-running Step 5 only
sees docs that haven't yet been normalized.

Partitioning: one Step 5 worker per Step 4 worker_id. Natural balance,
no skip/limit cost, no need to recompute byte boundaries.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService


import os
import time
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne

from code_labels import (
    state_label_of,
    country_label_of,
    sex_label_of,
    yes_no_label_of,
)


# ── Mongo client ─────────────────────────────────────────────────────────────

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")


# ── NPPES field-prefix constants ─────────────────────────────────────────────

TAX_CODE_PREFIX = "Healthcare Provider Taxonomy Code_"
TAX_SWITCH_PREFIX = "Healthcare Provider Primary Taxonomy Switch_"
TAX_GROUP_PREFIX = "Healthcare Provider Taxonomy Group_"

LICENSE_NUMBER_PREFIX = "Provider License Number_"
LICENSE_STATE_PREFIX  = "Provider License Number State Code_"

OID_PREFIX        = "Other Provider Identifier_"
OID_TYPE_PREFIX   = "Other Provider Identifier Type Code_"
OID_STATE_PREFIX  = "Other Provider Identifier State_"
OID_ISSUER_PREFIX = "Other Provider Identifier Issuer_"

# NPPES Data Dissemination Code Values (Feb 2025), section 1.11 —
# Other Provider Identifier Issuer Codes. Two-code closed dictionary.
OID_TYPE_DESCRIPTIONS = {
    "01": "OTHER",
    "05": "MEDICAID",
}

PRACTICE_ADDRESS_FIELDS = {
    "Provider First Line Business Practice Location Address": "line1",
    "Provider Second Line Business Practice Location Address": "line2",
    "Provider Business Practice Location Address City Name": "city",
    "Provider Business Practice Location Address State Name": "state",
    "Provider Business Practice Location Address Postal Code": "zip",
    "Provider Business Practice Location Address Country Code (If outside U.S.)": "country",
    "Provider Business Practice Location Address Telephone Number": "phone",
    "Provider Business Practice Location Address Fax Number": "fax",
}

MAILING_ADDRESS_FIELDS = {
    "Provider First Line Business Mailing Address": "line1",
    "Provider Second Line Business Mailing Address": "line2",
    "Provider Business Mailing Address City Name": "city",
    "Provider Business Mailing Address State Name": "state",
    "Provider Business Mailing Address Postal Code": "zip",
    "Provider Business Mailing Address Country Code (If outside U.S.)": "country",
    "Provider Business Mailing Address Telephone Number": "phone",
    "Provider Business Mailing Address Fax Number": "fax",
}


# ── Normalization (pure) ─────────────────────────────────────────────────────

def normalize_raw_record(raw: dict) -> dict:
    """Take a {original-NPPES-column: value} dict and return the normalized
    provider document (without identifiers like npi/load_id/record_id/worker_id,
    which the caller preserves from the existing doc).

    Takes a dict instead of (header_list, row_list) and skips raw-column
    entries whose value is empty (they were not stored in the raw subdoc).
    """
    doc: dict = {}
    consumed: set = set()

    # Taxonomy parallel arrays. NPPES encodes ONE primary designator per
    # NPI as a per-slot "Healthcare Provider Primary Taxonomy Switch_N"
    # column with 'Y' on exactly one slot (per NPPES Readme v.2 sec. 1.1).
    # Semantically it's a single per-NPI fact. We lift it out of the
    # taxonomies[] array and stamp it top-level as primary_taxonomy_code
    # (+ its label). Each taxonomies[] entry becomes just {code, code_label}
    # -- no per-entry primary flag, no group, no bloated classification.
    # code_label is stamped later by provider_record_builder using the
    # NUCC catalog; normalize doesn't have the catalog loaded here.
    for prefix in (TAX_CODE_PREFIX, TAX_SWITCH_PREFIX, TAX_GROUP_PREFIX):
        consumed.update(h for h in raw if h.startswith(prefix))
    taxonomies: list = []
    primary_code: str | None = None
    for h in sorted(h for h in raw if h.startswith(TAX_CODE_PREFIX)):
        idx = h[len(TAX_CODE_PREFIX):]
        code = (raw.get(h) or "").strip()
        if not code:
            continue
        switch = (raw.get(f"{TAX_SWITCH_PREFIX}{idx}") or "").strip()
        if switch.upper() == "Y" and primary_code is None:
            primary_code = code
        taxonomies.append({"code": code})
    if taxonomies:
        doc["taxonomies"] = taxonomies
    if primary_code:
        doc["primary_taxonomy_code"] = primary_code

    # License parallel arrays. Per LLD v39 sec. 7.1, licenses[i].state
    # gets a sibling state_label.
    consumed.update(h for h in raw if h.startswith(LICENSE_NUMBER_PREFIX))
    consumed.update(h for h in raw if h.startswith(LICENSE_STATE_PREFIX))
    licenses: list = []
    for h in sorted(h for h in raw if h.startswith(LICENSE_NUMBER_PREFIX)):
        idx = h[len(LICENSE_NUMBER_PREFIX):]
        number = (raw.get(h) or "").strip()
        state  = (raw.get(f"{LICENSE_STATE_PREFIX}{idx}") or "").strip()
        if not number and not state:
            continue
        entry: dict = {}
        if state:
            entry["state"] = state
            lbl = state_label_of(state)
            if lbl:
                entry["state_label"] = lbl
        if number:
            entry["number"] = number
        licenses.append(entry)
    if licenses:
        doc["licenses"] = licenses

    # Other-identifier parallel arrays (one object per slot — identifier
    # required, type/state/issuer optional). Misalignment-safe.
    for prefix in (OID_PREFIX, OID_TYPE_PREFIX, OID_STATE_PREFIX, OID_ISSUER_PREFIX):
        consumed.update(h for h in raw if h.startswith(prefix))
    other_identifiers: list = []
    for h in sorted(h for h in raw if h.startswith(OID_PREFIX)):
        idx = h[len(OID_PREFIX):]
        identifier = (raw.get(h) or "").strip()
        if not identifier:
            continue
        type_code = (raw.get(f"{OID_TYPE_PREFIX}{idx}") or "").strip()
        state     = (raw.get(f"{OID_STATE_PREFIX}{idx}") or "").strip()
        issuer    = (raw.get(f"{OID_ISSUER_PREFIX}{idx}") or "").strip()
        entry = {"identifier": identifier}
        if type_code:
            entry["type_code"] = type_code
            desc = OID_TYPE_DESCRIPTIONS.get(type_code)
            if desc:
                entry["type_description"] = desc
        if state:
            entry["state"] = state
        if issuer:
            entry["issuer"] = issuer
        other_identifiers.append(entry)
    if other_identifiers:
        doc["other_identifiers"] = other_identifiers

    # business_address (the NPPES mailing address) is one object, because NPPES
    # gives an NPI exactly one. practice_addresses[] holds the primary from the
    # main NPPES file here; secondaries are concat'd by
    # attach_practice_addresses from pl_pfile_*.csv in Step 6. The county
    # subdoc carries a fips=None placeholder on every entry; the county
    # enrichment passes populate it.
    practice_addresses: list = []

    def _apply_address_labels(addr: dict) -> None:
        """LLD v39 sec. 7.1: state and country get sibling _label fields."""
        if addr.get("state"):
            lbl = state_label_of(addr["state"])
            if lbl:
                addr["state_label"] = lbl
        if addr.get("country"):
            lbl = country_label_of(addr["country"])
            if lbl:
                addr["country_code_label"] = lbl

    practice = {
        sub: (raw[field] or "").strip()
        for field, sub in PRACTICE_ADDRESS_FIELDS.items()
        if (raw.get(field) or "").strip()
    }
    consumed.update(PRACTICE_ADDRESS_FIELDS)
    if "zip" in practice:
        practice["zip"] = practice["zip"][:5]
    if practice:
        practice["address_type"] = "practice"
        practice["county"] = {"fips": None}
        _apply_address_labels(practice)
        practice_addresses.append(practice)

    business = {
        sub: (raw[field] or "").strip()
        for field, sub in MAILING_ADDRESS_FIELDS.items()
        if (raw.get(field) or "").strip()
    }
    consumed.update(MAILING_ADDRESS_FIELDS)
    if "zip" in business:
        business["zip"] = business["zip"][:5]
    if business:
        business["address_type"] = "business"
        business["county"] = {"fips": None}
        _apply_address_labels(business)
        doc["business_address"] = business

    if practice_addresses:
        doc["practice_addresses"] = practice_addresses

    # active event log — derived from NPPES NPI Deactivation Date /
    # NPI Reactivation Date. Absent when neither is set (provider currently
    # active with no inactivity history). Present when either is set,
    # carrying one entry per event. Subsumes the former top-level
    # npi_deactivation_date / npi_reactivation_date scalar fields.
    DEACT_COL = "NPI Deactivation Date"
    REACT_COL = "NPI Reactivation Date"
    consumed.update([DEACT_COL, REACT_COL])
    active: list = []
    deact_date = (raw.get(DEACT_COL) or "").strip()
    react_date = (raw.get(REACT_COL) or "").strip()
    if deact_date:
        active.append({
            "event":     "deactivated",
            "date":      deact_date,
            "is_active": False,
            "source":    "nppes_deactivation_date",
        })
    if react_date:
        active.append({
            "event":     "reactivated",
            "date":      react_date,
            "is_active": True,
            "source":    "nppes_reactivation_date",
        })
    if active:
        doc["active"] = active

    # Remaining scalar fields — snake_case the key, skip empty values
    for h, v in raw.items():
        if h in consumed:
            continue
        v = v.strip() if isinstance(v, str) else v
        if v:
            key = (
                h.lower()
                .replace(" ", "_")
                .replace("(", "")
                .replace(")", "")
                .replace("/", "_")
                .replace(".", "")
            )
            doc[key] = v

    # Top-level coded fields get sibling _label per LLD v39 sec. 7.1.
    if doc.get("provider_sex_code"):
        lbl = sex_label_of(doc["provider_sex_code"])
        if lbl:
            doc["provider_sex_code_label"] = lbl
    if doc.get("is_sole_proprietor"):
        lbl = yes_no_label_of(doc["is_sole_proprietor"])
        if lbl:
            doc["is_sole_proprietor_label"] = lbl

    return doc


# ── Activity ────────────────────────────────────────────────────────────────

def normalize_provider_rows_worker_fn(config: dict) -> dict:
    """Normalize every Step-4 raw doc whose `worker_id` matches this worker.

    Idempotent — filters on `raw` field presence so re-runs are no-ops on
    already-normalized docs.

    Returns the per-worker counters consumed by the Step 5 step report.
    """
    worker_id = config["worker_id"]
    provider_collection = config.get(
        "provider_collection", "PipelinePublicHealthData.providers"
    )
    batch_size = int(config.get("normalize_batch_size", 500))

    db_name, coll_name = provider_collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    started_at = datetime.now(timezone.utc).isoformat()
    started_clock = time.monotonic()

    match = {"worker_id": worker_id, "raw": {"$exists": True}}
    cursor = coll.find(match, {"_id": 1, "raw": 1})

    ops: list = []
    normalized = 0
    written = 0
    failed: list = []

    for doc in cursor:
        try:
            raw = doc.get("raw") or {}
            normalized_fields = normalize_raw_record(raw)
            ops.append(UpdateOne(
                {"_id": doc["_id"]},
                {
                    "$set":   normalized_fields,
                    "$unset": {"raw": ""},
                },
            ))
            normalized += 1
            if len(ops) >= batch_size:
                result = coll.bulk_write(ops, ordered=False)
                written += result.modified_count
                ops = []
        except Exception as exc:
            failed.append({"_id": str(doc.get("_id")), "error": str(exc)})

    if ops:
        result = coll.bulk_write(ops, ordered=False)
        written += result.modified_count

    duration = time.monotonic() - started_clock
    rows_per_second = round(normalized / duration, 1) if duration > 0 else 0

    summary = {
        "worker_id":         worker_id,
        "started_at":        started_at,
        "finished_at":       datetime.now(timezone.utc).isoformat(),
        "normalized":        normalized,
        "modified_count":    written,
        "failed_count":      len(failed),
        "failed":            failed[:50],
        "duration_seconds":  round(duration, 2),
        "rows_per_second":   rows_per_second,
        "success":           len(failed) == 0,
    }
    ChatHealthyLoggingService().info(
        "normalize_provider_rows_worker: worker=%d normalized=%d "
        "modified=%d failed=%d %.1fs (%.1f rows/s)",
        worker_id, normalized, written, len(failed), duration, rows_per_second,
    )
    return summary


# ── Sub-orchestration ───────────────────────────────────────────────────────

def normalize_provider_rows_orchestrator_fn(context):
    cfg = context.get_input() or {}

    num_workers = int(cfg.get("num_workers", 100))
    provider_collection = cfg.get("provider_collection", "PipelinePublicHealthData.providers")
    normalize_batch_size = int(cfg.get("normalize_batch_size", 500))

    worker_configs = [
        {
            "worker_id":             i + 1,
            "provider_collection":   provider_collection,
            "normalize_batch_size":  normalize_batch_size,
        }
        for i in range(num_workers)
    ]

    fan_cfg = {
        "load_id":          cfg.get("load_id"),
        "worker_activity":  "normalize_provider_rows_worker_activity",
        "worker_configs":   worker_configs,
        "num_workers":      num_workers,
        "step_label":       "Step 5: Normalize provider rows",
        "warm_config":      {"num_workers": num_workers, "load_id": cfg.get("load_id")},
        "cool_config":      {"load_id": cfg.get("load_id")},
    }
    result = yield context.call_sub_orchestrator("fan_out_workers_orchestrator", fan_cfg)

    worker_results = result.get("results") or []
    totals = {
        "normalized":     sum(r.get("normalized",     0) for r in worker_results),
        "modified_count": sum(r.get("modified_count", 0) for r in worker_results),
        "failed_count":   sum(r.get("failed_count",   0) for r in worker_results),
        "worker_count":   len(worker_results),
    }
    ChatHealthyLoggingService().info("normalize_provider_rows_orchestrator: %s", totals)
    return {**result, "totals": totals}
