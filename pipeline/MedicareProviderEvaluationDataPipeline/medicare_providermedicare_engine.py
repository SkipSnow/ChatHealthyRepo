# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Molecule-master build engine (EPIC-010-F-007, LLD v83 III.3) — the
per-(molecule, partition) atomic apply.

One document per molecule (_id = molecule_id, the MASTER), embedding children:
RAW mode (< 100 CMS records, decided globally by prepare_molecule_masters) one
child per CMS Part D record; STATE-AGGREGATE mode one child per billing/
business state. The build fans out by partition = (billing/business state x
entity type); every row was stamped with its partition by the pre-pass, so a
worker's read is an indexed find({partition_state, partition_entity}).

Concurrency and exactly-once (LLD v83 III.3): multiple state workers converge
on one master. Each worker applies its partition's contribution to a molecule
with ONE ATOMIC UPDATE gated by the applied_partitions marker:

    filter {_id: mid, applied_partitions: {$ne: "<STATE>:<ENTITY>"}}
    update {$addToSet marker, $push children, $inc totals/counts}

Mongo's single-document atomicity makes concurrent partitions safe, and the
marker makes re-application of an already-applied partition a silent no-op --
merge-only; never a replace, never a delete. In aggregate mode the two entity
partitions of a state share ONE state child: the child is ensured once
(atomic $push gated on children.state) and each partition then $incs into it
via an arrayFilter, still gated by its own marker.

Conformance: Sum(total_cms_record_count over masters) + the no-molecule bucket
== CMS Part D row count (LLD v83 III.6, ruling H1 -- a mismatch is a finding).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    child_ge65_suppressed,
    molecule_id,
    partd_child,
)

_log = ChatHealthyLoggingService()

_ALLAGES = ("tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst")
_GE65 = ("ge65_tot_clms", "ge65_tot_30day_fills", "ge65_tot_day_suply",
         "ge65_tot_drug_cst")


def partition_label(state: str, entity: int) -> str:
    """The applied_partitions marker for one (billing/business state, entity)
    partition: '<STATE>:<ENTITY>'."""
    return f"{state}:{int(entity)}"


def _nucc_codes(spine_doc: dict) -> list[str]:
    codes: list[str] = []
    for tax in spine_doc.get("taxonomies") or []:
        code = (tax or {}).get("code") if isinstance(tax, dict) else None
        if code and code not in codes:
            codes.append(code)
    return codes


def _spine_by_npi(provider_spine_coll, npis: set[str], chunk: int = 1000) -> dict[str, dict]:
    """Resolve the Provider_v_5 spine doc for each NPI (chunked $in on the
    NPI-keyed Provider collection). Returns npi -> spine doc."""
    out: dict[str, dict] = {}
    batch: list[str] = []

    def _flush() -> None:
        if not batch:
            return
        for spine in provider_spine_coll.find({"npi": {"$in": batch}}):
            key = str(spine.get("npi") or spine.get("_id") or "").strip()
            if key:
                out[key] = spine
        batch.clear()

    for npi in npis:
        batch.append(npi)
        if len(batch) >= chunk:
            _flush()
    _flush()
    return out


def raw_children_for(rows: list[dict], state: str,
                     nucc_by_npi: dict[str, list[str]]) -> list[dict]:
    """The RAW children for one molecule's partition rows (LLD v83 III.4.2):
    partd_child verbatim quantities + suppression, with state = the partition's
    billing/business state and cms_state = the CMS column verbatim (source
    data only, never the partition basis)."""
    children = []
    for raw in rows:
        child = partd_child(raw)
        child["cms_state"] = child["state"] or None
        child["state"] = state
        child["nucc_codes"] = nucc_by_npi.get(child["npi"], [])
        children.append(child)
    return children


def aggregate_contribution(rows: list[dict]) -> dict:
    """One partition's contribution to a molecule, as plain sums (LLD v83
    III.5): all-ages usage summed (never suppressed); GE65 usage summed over
    VISIBLE rows only; one suppressed-row count; record_count = the rows."""
    out: dict[str, Any] = {k: 0 for k in _ALLAGES}
    ge65 = {k: 0 for k in _GE65}
    suppressed = 0
    for raw in rows:
        child = partd_child(raw)
        for k in _ALLAGES:
            if child[k] is not None:
                out[k] += child[k]
        if child_ge65_suppressed(child):
            suppressed += 1
        else:
            for k in _GE65:
                if child[k] is not None:
                    ge65[k] += child[k]
    out.update(ge65)
    out["ge65_usage_suppressed_count"] = suppressed
    out["record_count"] = len(rows)
    return out


def _zero_aggregate_child(state: str) -> dict:
    """A state-aggregate child at zero (LLD v83 III.4.2): ensured atomically
    before either entity partition $incs into it. GE65 sums start 0 (the
    visible-rows floor)."""
    child = {"state": state, "record_count": 0}
    for k in _ALLAGES:
        child[k] = 0
    for k in _GE65:
        child[k] = 0
    child["ge65_usage_suppressed_count"] = 0
    return child


def build_provider_molecule(
    *,
    state: str,
    entity_type: int,
    provider_spine_coll,
    partd_coll,
    master_coll,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Apply one (state, entity) partition's CMS rows to the molecule masters.

    Reads the partition's stamped rows, groups them per molecule, and applies
    one atomic, marker-gated update per molecule (raw mode) or the ensure+inc
    pair (aggregate mode). Returns the apply counts; a molecule already carrying
    this partition's marker is disregarded as already processed -- no error,
    no warning (LLD v83 III.3)."""
    label = partition_label(state, entity_type)

    rows_by_molecule: dict[str, list[dict]] = defaultdict(list)
    npis: set[str] = set()
    no_molecule_rows = 0
    # No run_id filter: the drained-and-replaced staging is the one truth
    # (Start At, operator design 2026-10-22).
    cursor = partd_coll.find(
        {"partition_state": state,
         "partition_entity": int(entity_type)})
    total_rows = 0
    for doc in cursor:
        raw = doc.get("raw") or {}
        total_rows += 1
        mid = molecule_id(raw.get(PARTD_COLS["generic"]))
        if not mid:
            no_molecule_rows += 1   # counted centrally by the pre-pass bucket
            continue
        rows_by_molecule[mid].append(raw)
        npi = str(raw.get(PARTD_COLS["npi"]) or "").strip()
        if npi:
            npis.add(npi)

    # Mode per molecule, decided globally by the pre-pass stubs.
    modes: dict[str, bool] = {
        d["_id"]: bool(d.get("state_aggregated"))
        for d in master_coll.find(
            {"_id": {"$in": list(rows_by_molecule.keys())}},
            {"state_aggregated": 1})
    }

    spine = _spine_by_npi(provider_spine_coll, npis)
    nucc_by_npi = {npi: _nucc_codes(doc) for npi, doc in spine.items()}

    raw_ops: list[UpdateOne] = []
    ensure_ops: list[UpdateOne] = []
    agg_ops: list[UpdateOne] = []
    missing_stub = 0
    for mid, rows in rows_by_molecule.items():
        if mid not in modes:
            # No stub: the pre-pass and this partition disagree about the
            # molecule set -- surfaced by the conformance arithmetic (III.6).
            missing_stub += 1
            continue
        contrib = aggregate_contribution(rows)
        inc_totals = {
            "totals.tot_clms": contrib["tot_clms"],
            "totals.tot_30day_fills": contrib["tot_30day_fills"],
            "totals.tot_day_suply": contrib["tot_day_suply"],
            "totals.tot_drug_cst": contrib["tot_drug_cst"],
            "total_cms_record_count": contrib["record_count"],
            "ge65_usage_suppressed_count": contrib["ge65_usage_suppressed_count"],
        }
        if not modes[mid]:
            children = raw_children_for(rows, state, nucc_by_npi)
            raw_ops.append(UpdateOne(
                {"_id": mid, "applied_partitions": {"$ne": label}},
                {"$addToSet": {"applied_partitions": label},
                 "$push": {"children": {"$each": children}},
                 "$inc": inc_totals}))
        else:
            ensure_ops.append(UpdateOne(
                {"_id": mid, "state_aggregated": True,
                 "children.state": {"$ne": state}},
                {"$push": {"children": _zero_aggregate_child(state)}}))
            child_incs = {f"children.$[c].{k}": contrib[k]
                          for k in (*_ALLAGES, *_GE65)}
            child_incs["children.$[c].record_count"] = contrib["record_count"]
            child_incs["children.$[c].ge65_usage_suppressed_count"] = (
                contrib["ge65_usage_suppressed_count"])
            agg_ops.append(UpdateOne(
                {"_id": mid, "applied_partitions": {"$ne": label}},
                {"$addToSet": {"applied_partitions": label},
                 "$inc": {**inc_totals, **child_incs}},
                array_filters=[{"c.state": state}]))

    applied = 0
    skipped_already_applied = 0

    def _run(ops: list[UpdateOne], count_applies: bool) -> None:
        nonlocal applied, skipped_already_applied
        for i in range(0, len(ops), batch_size):
            chunk = ops[i:i + batch_size]
            res = master_coll.bulk_write(chunk, ordered=False)
            if count_applies:
                applied += res.modified_count
                skipped_already_applied += len(chunk) - res.modified_count

    _run(ensure_ops, count_applies=False)
    _run(raw_ops, count_applies=True)
    _run(agg_ops, count_applies=True)

    _log.LogPipeline(
        "INFO",
        "build_provider_molecule[%s]: rows=%d molecules=%d applied=%d "
        "already_applied=%d missing_stub=%d no_molecule_rows=%d npis=%d",
        label, total_rows, len(rows_by_molecule), applied,
        skipped_already_applied, missing_stub, no_molecule_rows, len(npis))
    return {
        "partition": label,
        "rows": total_rows,
        "molecules": len(rows_by_molecule),
        "applied": applied,
        "already_applied": skipped_already_applied,
        "missing_stub": missing_stub,
        "no_molecule_rows": no_molecule_rows,
    }
