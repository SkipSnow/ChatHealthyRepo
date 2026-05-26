# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""provider_flags_enrichment — Step 12 sub-orchestration.

Owns the per-provider flag computation (`compute_provider_flags`) and the
activity that scans every in-scope provider, applies that computation, and
writes the four flags back to Mongo.

The four flags carried by every provider record:
    can_prescribe       — OR  across taxonomies (any prescribing taxonomy → True)
    is_homeopathic      — OR  across taxonomies
    is_npi_registered   — OR  across taxonomies
    is_disqualified     — AND across taxonomies (every taxonomy must be disqualified)

entity_type_code == "2" (organization) short-circuits to:
    can_prescribe=False, is_homeopathic=False, is_npi_registered=True,
    is_disqualified=False.

Catalog source is ProprietaryData.SpecialtyAndProviderFlags via
specialty_classification_catalog.load_catalog().

This file owns logic previously located inline in
county_enrichment_job.apply_proprietary_flags_fn; that function is retired
when the parent orchestrator routes Step 12 here instead.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Iterable

from pymongo import MongoClient, UpdateOne
from pymongo.errors import BulkWriteError


# ── Pure computation ─────────────────────────────────────────────────────────

def compute_provider_flags(
    doc: dict,
    catalog: Dict[str, Dict[str, bool]],
) -> dict:
    """Return the four flags for one provider document.

    Pure function — no I/O. The catalog argument is the in-memory dict
    returned by specialty_classification_catalog.load_catalog().
    """
    # is_npi_registered dropped from the provider record per the schema
    # reconciliation: at the provider level it's tautologically true (every
    # provider we load came from NPPES, by definition). The flag remains
    # on the catalog where it actually discriminates (used vs unused NUCC
    # codes); it just doesn't bubble up to the provider doc.
    entity_type = doc.get("entity_type_code", "")
    if entity_type == "2":
        return {
            "can_prescribe":   False,
            "is_homeopathic":  False,
            "is_disqualified": False,
        }

    taxonomies = doc.get("taxonomies", []) or []
    entries: list = []
    for t in taxonomies:
        code = t.get("code", "") if isinstance(t, dict) else ""
        e = catalog.get(code) if code else None
        if e is not None:
            entries.append(e)

    if not entries:
        # No catalog entry matched any of the provider's taxonomies.
        # Treat as disqualified — the provider is outside the proprietary
        # catalog's curated allow-list.
        return {
            "can_prescribe":   False,
            "is_homeopathic":  False,
            "is_disqualified": True,
        }

    return {
        "can_prescribe":   any(e["can_prescribe"]   for e in entries),
        "is_homeopathic":  any(e["is_homeopathic"]  for e in entries),
        "is_disqualified": all(e["is_disqualified"] for e in entries),
    }


# ── Activity ─────────────────────────────────────────────────────────────────

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
        )
    return _mongo


def apply_provider_flags_fn(config: dict) -> dict:
    """Scan every in-scope provider; compute flags via compute_provider_flags;
    write the four flags back via bulk_write. Fail-hard on any BulkWriteError.

    Tunables:
        flag_stamp_batch_size — bulk_write op batch size (default 500)
    """
    from county_enrichment_job import _build_states_filter, PROVIDERS_COLLECTION
    from specialty_classification_catalog import load_catalog

    catalog = load_catalog()

    collection_name = config.get("provider_collection", PROVIDERS_COLLECTION)
    db_name, coll_name = collection_name.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    batch_size = int(config.get("flag_stamp_batch_size", 500))
    if batch_size < 1:
        raise ValueError(f"flag_stamp_batch_size must be >= 1, got {batch_size}")

    base_filter = _build_states_filter(config)
    total = coll.count_documents(base_filter)
    logging.info(
        "provider_flags_enrichment: %d providers in scope "
        "(catalog has %d entries, batch_size=%d)",
        total, len(catalog), batch_size,
    )
    if total == 0:
        return {
            "classified": 0, "prescribers": 0, "homeopathic": 0,
            "disqualified": 0, "unknown_taxonomy": 0,
            "batch_size": batch_size,
        }

    ops: list = []
    written = 0
    prescribers = 0
    homeopathic = 0
    disqualified = 0
    unknown_taxonomy = 0

    def _flush(pending_ops):
        nonlocal written
        if not pending_ops:
            return
        try:
            result = coll.bulk_write(pending_ops, ordered=False)
        except BulkWriteError as exc:
            details = exc.details or {}
            logging.error(
                "provider_flags_enrichment bulk_write FAILED: "
                "code=%s nModified=%s writeErrors=%s",
                getattr(exc, "code", None),
                details.get("nModified"),
                str(details.get("writeErrors", ""))[:500],
            )
            raise
        # Count matched, not modified, so a re-run against already-correct
        # records doesn't falsely trip the `written < total` sanity check.
        written += result.matched_count

    cursor = coll.find(
        base_filter,
        {"npi": 1, "entity_type_code": 1, "taxonomies": 1, "_id": 1},
    )

    for doc in cursor:
        flags = compute_provider_flags(doc, catalog)

        if flags["can_prescribe"]:
            prescribers += 1
        if flags["is_homeopathic"]:
            homeopathic += 1
        if flags["is_disqualified"]:
            disqualified += 1
        # Unknown-taxonomy = no catalog entry matched; that path also returns
        # is_disqualified=True, so detect it explicitly.
        entity_type = doc.get("entity_type_code", "")
        if entity_type != "2" and not any(
            isinstance(t, dict)
            and catalog.get(t.get("code", "")) is not None
            for t in (doc.get("taxonomies") or [])
        ):
            unknown_taxonomy += 1

        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": flags},
        ))

        if len(ops) >= batch_size:
            _flush(ops)
            ops = []

    _flush(ops)

    if written < total:
        raise RuntimeError(
            f"provider_flags_enrichment: only {written} of {total} in-scope "
            f"records were stamped (short by {total - written}). The pipeline "
            f"is failing rather than ship un-stamped records."
        )

    logging.info(
        "provider_flags_enrichment: classified %d providers "
        "(prescribers=%d, homeopathic=%d, disqualified=%d, "
        "unknown_taxonomy=%d, batch_size=%d)",
        written, prescribers, homeopathic, disqualified, unknown_taxonomy,
        batch_size,
    )
    return {
        "classified":       written,
        "prescribers":      prescribers,
        "homeopathic":      homeopathic,
        "disqualified":     disqualified,
        "unknown_taxonomy": unknown_taxonomy,
        "batch_size":       batch_size,
    }


# ── Sub-orchestration ────────────────────────────────────────────────────────

def provider_flags_enrichment_orchestrator_fn(context):
    cfg = context.get_input() or {}

    context.set_custom_status("Step 12: Applying provider-level flags")
    result = yield context.call_activity("apply_provider_flags_activity", cfg)

    logging.info("provider_flags_enrichment_orchestrator: %s", result)
    return result
