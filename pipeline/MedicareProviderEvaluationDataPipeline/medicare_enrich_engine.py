# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Enrichment engine (EPIC-010-F-007-S-002) -- completes a base record from the
Provider collection and the FDA molecule collection.

One pass per partition, not three. The base collection holds 26.7 million
records (measured 2026-10-23), so each sweep of it is expensive: this task
reads a partition once and attaches everything that comes from outside the CMS
file, rather than one sweep per enrichment.

What it attaches:
  * from the Provider collection, by NPI -- the billing state, the entity type
    (individual or organization, REQ-B-002), the NUCC taxonomy codes
    (REQ-B-003), and for an organization the CCN (REQ-B-011) and the size tier
    derived from its certified bed count (REQ-B-012);
  * from the FDA molecule collection, by molecule -- the fda block and the
    indications it carries, each keeping its trust (REQ-B-009, REQ-B-010).

Both lookups are cached per key within the partition: a prescriber holds many
molecule records and a molecule is prescribed by many providers, so the Provider
document is fetched once per NPI and the FDA document once per molecule rather
than once per record.

The write is $set of the enrichment fields only. It never touches the base
facts, and re-running a partition recomputes the same values from the same
sources, so a re-driven unit is harmless.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_drugs_engine import (
    fda_block_from)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_org_engine import (
    _band, _build_pos_index, _extract_ccn)

_log = ChatHealthyLoggingService()


def entity_type_of(provider_doc: dict) -> str | None:
    """Individual or organization as the Provider collection gives it
    (REQ-B-002); None when the NPI is in no Provider record."""
    code = str((provider_doc or {}).get("entity_type_code") or "").strip()
    if code == "1":
        return "I"
    if code == "2":
        return "O"
    return None


def nucc_codes_of(provider_doc: dict) -> list[str]:
    """The provider's NUCC taxonomy codes, first-seen order (REQ-B-003)."""
    codes: list[str] = []
    for tax in (provider_doc or {}).get("taxonomies") or []:
        code = (tax or {}).get("code") if isinstance(tax, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def billing_state_of(provider_doc: dict) -> str | None:
    """The provider's billing state from its business address; None when the
    NPI is in no Provider record or the record carries no state."""
    state = ((provider_doc or {}).get("business_address") or {}).get("state")
    state = str(state or "").strip()
    return state or None


def provider_facts(provider_doc: dict) -> dict[str, Any]:
    """Everything one Provider document contributes to a record."""
    return {
        "billing_state": billing_state_of(provider_doc),
        "entity_type": entity_type_of(provider_doc),
        "nucc_codes": nucc_codes_of(provider_doc),
    }


def enrich_partition(
    *,
    state: str,
    target_coll,
    provider_coll,
    fda_coll,
    pos_coll,
    record_discrepancy=None,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Enrich one source-state partition of the base collection."""
    ccn_beds, cutpoints = _build_pos_index(pos_coll)

    provider_cache: dict[str, dict] = {}
    fda_cache: dict[str, dict | None] = {}
    ops: list[UpdateOne] = []
    seen = written = without_provider = without_fda = sized = 0
    missing_npis: set[str] = set()

    for rec in target_coll.find(
            {"source_state": state},
            {"_id": 1, "npi": 1, "molecule_id": 1}):
        seen += 1
        npi = str(rec.get("npi") or "")
        mid = rec.get("molecule_id")

        if npi not in provider_cache:
            doc = (provider_coll.find_one({"npi": npi})
                   or provider_coll.find_one({"_id": npi}) or {})
            provider_cache[npi] = doc
        provider_doc = provider_cache[npi]
        if not provider_doc:
            if npi not in missing_npis:
                missing_npis.add(npi)
                without_provider += 1
                if record_discrepancy is not None:
                    record_discrepancy(
                        reason="FDA_NPI_not_found_In_Providers",
                        step="enrich", artifact="providermedicare",
                        record_key=npi,
                        detail={"kind": "npi_not_in_provider_collection"})

        setter: dict[str, Any] = dict(provider_facts(provider_doc))

        if setter["entity_type"] == "O":
            ccn = _extract_ccn(provider_doc)
            setter["ccn"] = ccn
            tier = _band(ccn_beds.get(ccn), cutpoints) if ccn else None
            setter["size_tier"] = tier
            if tier is not None:
                sized += 1

        if mid not in fda_cache:
            fda_doc = fda_coll.find_one({"_id": mid})
            fda_cache[mid] = fda_doc
        fda_doc = fda_cache[mid]
        if fda_doc is None:
            without_fda += 1
        else:
            setter["fda"] = fda_block_from(fda_doc)
            setter["indications"] = fda_doc.get("indications") or []

        ops.append(UpdateOne({"_id": rec["_id"]}, {"$set": setter}))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    result = {
        "partition": state,
        "records_seen": seen,
        "records_written": written,
        "npis_without_provider_record": without_provider,
        "records_without_fda_molecule": without_fda,
        "organizations_sized": sized,
        "providers_resolved": len(provider_cache),
        "molecules_resolved": len(fda_cache),
    }
    _log.LogPipeline(
        "INFO",
        "enrich[%s]: records=%d written=%d npis_without_provider=%d "
        "without_fda=%d sized=%d",
        state, seen, written, without_provider, without_fda, sized)
    return result
