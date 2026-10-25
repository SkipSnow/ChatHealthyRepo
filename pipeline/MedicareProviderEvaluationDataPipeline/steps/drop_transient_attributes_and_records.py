# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CMS_NPI_Prescriber_DropTransientAttributesAndRecords (EPIC-010-F-007-S-002)
-- removes the records the run's scope does not own, then the attribute that
only existed to partition the work.

TWO ACTS, IN THIS ORDER.

Records first. The base reads EVERY CMS state, because that is the only way to
collect every prescription written by the NPIs in our Provider collection: a
provider's prescriptions are filed under the state CMS recorded for the
prescriber, and one provider's prescriptions can therefore arrive from several
CMS states. That is the feature -- we want the ENTIRE provider -- and it is
why the base deliberately over-collects. This task is what makes the
collection answer the run's scope: a record is kept when its provider's
BILLING state is one of the argued states, and dropped otherwise.

The billing address is the NPI's canonical geography (operator ruling
2026-10-24): a provider has one and only one, so a provider belongs to exactly
one state's scope and every prescription of theirs is kept with them, whatever
CMS state it was filed under. It is read from the Provider collection's
business_address.state -- the same fact the enrichment stamps as
billing_state, read through the same function so the two can never diverge.
The CMS prescriber state is never the geography.

Attributes second. source_state is the fan-out key for the base and for this
task; once the records are settled it is scaffolding and goes. It must go
AFTER the deletion, because the deletion finds its partition by it.

This task runs BEFORE the NUCC rollup (operator ruling 2026-10-24): the rollup
rolls up this collection, so it must see the settled set of records and not
the over-collected one.
"""

from __future__ import annotations

from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_engine import (
    billing_state_of)
from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (
    parse_provider_source)

_log = ChatHealthyLoggingService()

TRANSIENT_ATTRIBUTES = ("source_state",)

ALL_STATES = "ALL"


def argued_states(states_arg) -> set[str] | None:
    """The run's scope as a set, or None when the run argues every state.

    None is not 'no states': it is the whole country, and a whole-country run
    drops no record.
    """
    if states_arg is None:
        return None
    values = (states_arg if isinstance(states_arg, (list, tuple, set))
              else str(states_arg).split(","))
    wanted = {str(v).strip().upper() for v in values if str(v).strip()}
    if not wanted or ALL_STATES in wanted:
        return None
    return wanted


def in_scope(provider_doc: dict, wanted: set[str]) -> bool:
    """Is this provider's one billing state among the argued states?"""
    state = billing_state_of(provider_doc)
    return bool(state) and str(state).strip().upper() in wanted


def _pages(coll, state: str, page_size: int):
    """Yield the partition's records page by page, holding no cursor."""
    last_id = None
    while True:
        query: dict[str, Any] = {"source_state": state}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        page = list(coll.find(query, {"_id": 1, "npi": 1},
                              sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield page


def run_step(ctx, page_size: int = 1000) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("source_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message=("CMS_NPI_Prescriber_DropTransientAttributesAndRecords: "
                     "expected ctx.config['partition']['source_state']"),
            component="drop_transient_attributes_and_records",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    tgt = reg.by_source_name("providermedicare")
    target_coll = mongo[tgt.public_data_db][
        reg.public_data_collection_name("providermedicare")]
    prov_db, prov_name = parse_provider_source(ctx.args.provider_collection)
    provider_coll = mongo[prov_db][prov_name]

    wanted = argued_states(getattr(ctx.args, "states", None))
    seen = dropped = 0
    out_of_scope_npis = 0

    # A record whose NPI is in no Provider record is dropped too: the
    # collection maps one-to-one onto our provider data, so a prescriber CMS
    # knows and we do not is out of scope by definition (operator ruling
    # 2026-10-24).
    keep_cache: dict[str, bool] = {}
    absent_npis = 0
    for page in _pages(target_coll, str(state), page_size):
        seen += len(page)
        # One provider query per PAGE, not per record. A find_one per distinct
        # NPI is ~1M round trips across the collection and is what blew this
        # step's budget on 2026-10-24; the page's unknown NPIs are resolved in
        # a single $in.
        unknown = {str(r.get("npi") or "") for r in page} - set(keep_cache)
        unknown.discard("")
        if unknown:
            found: dict[str, dict] = {}
            for doc in provider_coll.find(
                    {"npi": {"$in": sorted(unknown)}},
                    {"npi": 1, "business_address.state": 1}):
                found[str(doc.get("npi") or "")] = doc
            for npi in unknown:
                doc = found.get(npi)
                if doc is None:
                    keep_cache[npi] = False
                    absent_npis += 1
                    continue
                keep_cache[npi] = wanted is None or in_scope(doc, wanted)
                if not keep_cache[npi]:
                    out_of_scope_npis += 1
        doomed = [r["_id"] for r in page
                  if not keep_cache.get(str(r.get("npi") or ""), False)]
        if doomed:
            dropped += target_coll.delete_many(
                {"_id": {"$in": doomed}}).deleted_count

    stripped = target_coll.update_many(
        {"source_state": str(state)},
        {"$unset": {attr: "" for attr in TRANSIENT_ATTRIBUTES}}).modified_count

    result = {"partition": str(state),
              "scope": sorted(wanted) if wanted else ALL_STATES,
              "records_seen": seen,
              "records_dropped": dropped,
              "npis_out_of_scope": out_of_scope_npis,
              "npis_in_no_provider_record": absent_npis,
              "records_stripped": stripped}
    _log.LogPipeline(
        "INFO",
        "CMS_NPI_Prescriber_DropTransientAttributesAndRecords[%s]: scope=%s "
        "seen=%d dropped=%d out_of_scope_npis=%d absent_npis=%d stripped=%d",
        state, result["scope"], seen, dropped, out_of_scope_npis,
        absent_npis, stripped)
    return result


def execute(ctx):
    return run_step(ctx)
