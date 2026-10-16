# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""enrich_drugs engine (EPIC-010-F-007) -- the v1 drug-collection composition.

Consumes the fdaProductToDiagnosisResolution collection (the FDA drug collection)
built this run by the build_fda_drug_collection prerequisite and re-grains each
provider's raw Part D drug rows to ONE entry per CANONICAL brand joined to the FDA
record.

Memory-bounded by design: there are ~20MM CMS rows and the FDA collection is NEVER
loaded into memory. The match is CMS-driven and streamed per provider document; each
distinct CMS Brnd_Name is resolved by EXACT indexed equality against the FDA
collection's persisted normalized keys, in THREE tiers tried in order (first hit wins,
and the composed drug entry records which tier matched in `match_provenance`):
  1. brand_name   -- brand_exact_key == normalize_brand(Brnd_Name) (exact brand).
  2. brand_key    -- brand_key == brand_join_key(Brnd_Name) (salt/form-normalized).
  3. generic_name -- generic_key == brand_join_key(Brnd_Name) (CMS put a generic in
     Brnd_Name); the FDA brand_name is adopted as the canonical brand.
Both sides normalize identically (the CMS side here, the FDA side at build time), so
each query is an exact equality with no candidate list and no collation. A small
per-run cache keyed by normalize_brand(Brnd_Name) (bounded by the ~few-thousand
distinct brands) avoids re-querying the same brand; the matched records for one brand
are aggregated on the spot by the pure function aggregate_fda_records. Rows are then
grouped by the final canonical brand (compose_drugs_by_brand) and their volumes summed.

A Part D row carries no rxcui/NDC: a drug is identified only by brand + generic name.
All string handling uses plain string operations only -- NO regular expressions,
which an engineering rule forbids in pipeline code.

The join is INNER: a provider brand matching NEITHER leg is DISCARDED, never written
-- the composed drug collection holds only drugs mappable to the FDA record. Each
discard is counted and recorded as a per-record WARNING finding (class
partd_brand_unmatched_to_fda), never dropped silently -- operator decision: an
unmatched brand is a warning, not an error, and never aborts the run. That finding
class must be declared severity="warning" in the pipeline's
discrepancy_report.finding_types config; the engine does not create it. The
inner-join discard is correct ONLY against the COMPLETE FDA collection (a
full-partition FDA rebuild); against a partial FDA build most brands would be
discarded, so this step presumes the full FDA rebuild has run.

The FDA-side join indexes (brand_exact_key, brand_key, generic_key) and the
composed-collection drug indexes are created with their collections, by
build_fda_drug_collection and build_providermedicare_base respectively -- not here.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    aggregate_fda_records,
    brand_join_key,
    compose_drugs_by_brand,
    normalize_brand,
)

_log = ChatHealthyLoggingService()

# Only the fields the brand aggregation needs are projected off each matched FDA
# label record -- never the whole document.
_FDA_PROJECTION = {
    "openfda": 1, "molecule": 1, "ingredient_rxcuis": 1, "ingredient_names": 1,
    "usage_summary": 1, "indications": 1,
}


def _make_brand_resolver(fda_drug_coll):
    """Return resolve_brand(cms_brand) -> fda_payload | None.

    Resolves one CMS brand by EXACT indexed equality against the FDA collection's
    persisted normalized keys, in THREE tiers tried in order, stopping at the first
    hit and stamping the payload's `match_provenance` with that tier:
      1. brand_name  -- {brand_exact_key: normalize_brand(cms_brand)}  (exact brand,
         no salt/form strip; the primary, highest-confidence match).
      2. brand_key   -- {brand_key: brand_join_key(cms_brand)}  (salt/form-normalized
         brand).
      3. generic_name-- {generic_key: brand_join_key(cms_brand)}  (CMS put a generic
         in Brnd_Name); the aggregated payload's `brand` is the FDA brand_name, so the
         canonical brand is adopted.
    Both sides normalize identically at build time and here, so each query is an exact
    indexed equality -- no candidate list, no collation. The matched records (a brand
    has only a handful) are aggregated on the spot; nothing sized by the FDA collection
    or the 20MM CMS rows is held. The result is memoized per normalize_brand(cms_brand)
    -- the finest key, which determines both tier keys -- a cache bounded by the
    ~few-thousand distinct brands. Returns None when all three tiers miss or the match
    carries no canonical brand.
    """
    cache: dict[str, Any] = {}

    def resolve_brand(cms_brand):
        nk = normalize_brand(cms_brand)
        if nk in cache:
            return cache[nk]
        payload = None
        if nk:
            jk = brand_join_key(cms_brand)
            records = list(fda_drug_coll.find({"brand_exact_key": nk}, _FDA_PROJECTION))
            provenance = "brand_name"
            if not records and jk:
                records = list(fda_drug_coll.find({"brand_key": jk}, _FDA_PROJECTION))
                provenance = "brand_key"
            if not records and jk:
                records = list(fda_drug_coll.find({"generic_key": jk}, _FDA_PROJECTION))
                provenance = "generic_name"
            entry = aggregate_fda_records(records)
            if entry is not None and entry.get("brand"):
                entry["match_provenance"] = provenance
                payload = entry
        cache[nk] = payload
        return payload

    return resolve_brand


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
                     "its prerequisite build_fda_drug_collection step produced no records to "
                     "consume"),
        )
    resolve = _make_brand_resolver(fda_drug_coll)

    ops: list[UpdateOne] = []
    written = 0
    kept_total = 0
    discarded_total = 0
    for doc in providermedicare_coll.find({"run_id": run_id, "provider_state": state}):
        raw_drugs = doc.get("drugs") or []
        if not raw_drugs:
            continue
        kept, discarded = compose_drugs_by_brand(raw_drugs, resolve)
        for disc in discarded:
            # INNER-JOIN DISCARD: a Part D brand matching neither the FDA brand nor
            # generic index is dropped and surfaced as a per-record WARNING finding
            # (count + brand) -- operator decision: an unmatched brand is a warning,
            # not an error, and never aborts the run. The finding class
            # partd_brand_unmatched_to_fda must be declared severity="warning" in
            # discrepancy_report.finding_types; until the operator sets it,
            # record_discrepancy refuses the undeclared class. Correct ONLY against
            # the COMPLETE FDA rebuild.
            runtime.record_discrepancy(
                reason="partd_brand_unmatched_to_fda",
                step="enrich_drugs",
                artifact="provider",
                record_key=doc["_id"],
                state=state,
                detail={
                    "brand": disc.get("brand"),
                    "brand_join_key": disc.get("brand_join_key"),
                    "total_claims": disc.get("total_claims"),
                    "note": ("Part D brand did not match the Rx-only FDA brand set and was "
                             "discarded from the composed drug collection; valid only against "
                             "a complete FDA rebuild"),
                },
            )
        kept_total += len(kept)
        discarded_total += len(discarded)
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"drugs": kept}}))
        if len(ops) >= batch_size:
            providermedicare_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        providermedicare_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "enrich_drugs[%s]: docs_updated=%d drugs_kept=%d drugs_discarded=%d",
        state, written, kept_total, discarded_total,
    )
    return {
        "state": state,
        "providermedicare_updated": written,
        "drugs_kept": kept_total,
        "drugs_discarded": discarded_total,
    }
