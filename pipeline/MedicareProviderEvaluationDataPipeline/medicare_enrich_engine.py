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
from pymongo.errors import BulkWriteError

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_drugs_engine import (
    fda_block_from)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    _bson, _dec)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_org_engine import (
    _band, _build_pos_index, _extract_ccn)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    record_refused_writes)

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


def _pages(target_coll, state: str, page_size: int):
    """Yield the partition's records page by page, holding no cursor."""
    last_id = None
    while True:
        query: dict[str, Any] = {"source_state": state}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        page = list(target_coll.find(
            query, {"_id": 1, "npi": 1, "molecule_id": 1},
            sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield from page


def _apply_conforming(target_coll, ops: list, keys: list[str], *,
                      record_discrepancy) -> int:
    """Apply a batch of enrichment updates; return how many the store took.

    UNORDERED, so an update the collection's validator refuses leaves the
    conforming updates in the same batch applied. Each refused document
    becomes its own per-record finding carrying its NPI and molecule.
    """
    try:
        target_coll.bulk_write(ops, ordered=False)
        return len(ops)
    except BulkWriteError as exc:
        refused = record_refused_writes(
            details=exc.details,
            keys=keys,
            artifact="providermedicare",
            step="enrich",
            record_discrepancy=record_discrepancy,
            exception=exc,
        )
        return len(ops) - refused


def enrich_partition(
    *,
    state: str,
    target_coll,
    provider_coll,
    fda_coll,
    pos_coll,
    record_discrepancy,
    batch_size: int = 500,
    page_size: int = 1000,
) -> dict[str, Any]:
    """Enrich one source-state partition of the base collection.

    The partition is read by key pages, never by one long-lived cursor. A
    cursor held open across the per-record provider and FDA lookups goes idle
    and the server reaps it -- which is what killed five partitions on
    2026-10-23 with CursorNotFound after 45 minutes. Each page is drained in
    one round trip and the server cursor is gone before any lookup runs; the
    resume point is the page's last _id, so nothing is remembered server-side
    between pages. _id is the {npi, molecule_id} subdocument, which BSON
    orders field by field, so $gt on the whole _id is a total order matching
    the source_state_1__id_1 index: every record is visited exactly once.
    """
    ccn_beds, cutpoints = _build_pos_index(pos_coll)

    provider_cache: dict[str, dict] = {}
    fda_cache: dict[str, dict | None] = {}
    ops: list[UpdateOne] = []
    keys: list[str] = []
    seen = written = offered = without_provider = without_fda = sized = 0
    with_identity = without_identity = 0
    missing_npis: set[str] = set()

    for rec in _pages(target_coll, state, page_size):
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
            # _dec on the way in: a stored Decimal128 carries no arithmetic,
            # so it comes home to Decimal before anything reads it.
            fda_doc = _dec(fda_coll.find_one({"_id": mid}))
            fda_cache[mid] = fda_doc
        fda_doc = fda_cache[mid]
        if fda_doc is None:
            without_fda += 1
        else:
            setter["fda"] = fda_block_from(fda_doc)
            setter["indications"] = fda_doc.get("indications") or []
            # The external identity, resolved once by the serial FDA finalize
            # and carried here by the molecule's own document.
            if fda_doc.get("rxcui_in"):
                setter["rxcui_in"] = fda_doc["rxcui_in"]
                with_identity += 1
                if fda_doc.get("unii"):
                    setter["unii"] = fda_doc["unii"]
                if fda_doc.get("identity_path"):
                    setter["identity_path"] = fda_doc["identity_path"]
            else:
                without_identity += 1

        ops.append(UpdateOne({"_id": rec["_id"]}, {"$set": _bson(setter)}))
        keys.append(f"{npi}:{mid}")
        if len(ops) >= batch_size:
            offered += len(ops)
            written += _apply_conforming(
                target_coll, ops, keys, record_discrepancy=record_discrepancy)
            ops = []
            keys = []
    if ops:
        offered += len(ops)
        written += _apply_conforming(
            target_coll, ops, keys, record_discrepancy=record_discrepancy)

    result = {
        "partition": state,
        "records_seen": seen,
        "records_written": written,
        "records_refused_by_contract": offered - written,
        "npis_without_provider_record": without_provider,
        "records_without_fda_molecule": without_fda,
        "records_with_external_identity": with_identity,
        "records_without_external_identity": without_identity,
        "organizations_sized": sized,
        "providers_resolved": len(provider_cache),
        "molecules_resolved": len(fda_cache),
    }
    _log.LogPipeline(
        "INFO",
        "enrich[%s]: records=%d written=%d refused=%d "
        "npis_without_provider=%d without_fda=%d sized=%d",
        state, seen, written, offered - written, without_provider,
        without_fda, sized)
    return result
