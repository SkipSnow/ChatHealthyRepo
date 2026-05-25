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

import logging
import os
import time
from datetime import datetime, timezone

from pymongo import MongoClient, UpdateOne


# ── Mongo client ─────────────────────────────────────────────────────────────

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
            maxPoolSize=64,
        )
    return _mongo


# ── NPPES field-prefix constants (moved from provider_worker.py) ─────────────

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

    Mirror of the original provider_worker._normalize_row, refactored to take
    a dict instead of (header_list, row_list) and to skip raw-column entries
    whose value is empty (they were not stored in the raw subdoc).
    """
    doc: dict = {}
    consumed: set = set()

    # Taxonomy parallel arrays
    for prefix in (TAX_CODE_PREFIX, TAX_SWITCH_PREFIX, TAX_GROUP_PREFIX):
        consumed.update(h for h in raw if h.startswith(prefix))
    taxonomies: list = []
    for h in sorted(h for h in raw if h.startswith(TAX_CODE_PREFIX)):
        idx = h[len(TAX_CODE_PREFIX):]
        code = (raw.get(h) or "").strip()
        if not code:
            continue
        switch = (raw.get(f"{TAX_SWITCH_PREFIX}{idx}") or "").strip()
        group = (raw.get(f"{TAX_GROUP_PREFIX}{idx}") or "").strip()
        entry = {"code": code, "primary": switch.upper() == "Y"}
        if group:
            entry["group"] = group
        taxonomies.append(entry)
    if taxonomies:
        doc["taxonomies"] = taxonomies

    # License parallel arrays
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
        if number:
            entry["number"] = number
        licenses.append(entry)
    if licenses:
        doc["licenses"] = licenses

    # Other-identifier parallel arrays (one object per slot — identifier
    # required, type/state/issuer optional). Misalignment-safe per the
    # provider_worker rewrite that this code came from.
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

    # Primary practice address (always a list; secondary entries are joined
    # in by Step 6 from pl_pfile_*.csv).
    primary = {
        sub: (raw[field] or "").strip()
        for field, sub in PRACTICE_ADDRESS_FIELDS.items()
        if (raw.get(field) or "").strip()
    }
    consumed.update(PRACTICE_ADDRESS_FIELDS)
    if "zip" in primary:
        primary["zip"] = primary["zip"][:5]
    if primary:
        primary["county"] = {"fips": None}
        doc["practice_address"] = [primary]

    # Mailing address (single sub-document)
    mailing = {
        sub: (raw[field] or "").strip()
        for field, sub in MAILING_ADDRESS_FIELDS.items()
        if (raw.get(field) or "").strip()
    }
    consumed.update(MAILING_ADDRESS_FIELDS)
    if "zip" in mailing:
        mailing["zip"] = mailing["zip"][:5]
    if mailing:
        doc["mailing_address"] = mailing

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
        "provider_collection", "dev_PublicHealthData.providers"
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
    logging.info(
        "normalize_provider_rows_worker: worker=%d normalized=%d "
        "modified=%d failed=%d %.1fs (%.1f rows/s)",
        worker_id, normalized, written, len(failed), duration, rows_per_second,
    )
    return summary


# ── Sub-orchestration ───────────────────────────────────────────────────────

def normalize_provider_rows_orchestrator_fn(context):
    cfg = context.get_input() or {}

    num_workers = int(cfg.get("num_workers", 100))
    provider_collection = cfg.get("provider_collection", "dev_PublicHealthData.providers")
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
    logging.info("normalize_provider_rows_orchestrator: %s", totals)
    return {**result, "totals": totals}
