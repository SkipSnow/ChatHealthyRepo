# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""prepare_molecule_masters (EPIC-010-F-007, LLD v83 III.10) -- the serial mode
pre-pass for the molecule-master build.

Duties, in order:
  1. STAMP partition_state and partition_entity onto every staged Part D row:
     the prescriber's BILLING/BUSINESS address state from
     Provider_v_5.business_address.state by the row's NPI (LLD v83 III.3,
     operator-ruled 2026-10-20) and the provider's entity type (Type 1
     individual / Type 2 institutional). A row whose NPI is absent from the
     spine is stamped to the catch-all partition ("ALL", entity 1) and the
     not-found finding is recorded once per NPI. The CMS Prscrbr_State_Abrvtn
     column is untouched -- source data only.
  2. Measure the NO-MOLECULE BUCKET: rows whose Gnrc_Name is blank cannot mint
     a molecule_id and cannot land in any master. A named, counted bucket --
     a finding, never a silent drop and never a run kill (hard-fail ruling H1).
  3. FULL ANNUAL REPLACE (decision #1): clear this data_version's published
     master and specialty collections.
  4. Write every master STUB: _id = molecule_id, state_aggregated decided from
     the GLOBAL per-molecule row count (RAW < 100 <= STATE-AGGREGATE), zeroed
     totals and counts, children [], applied_partitions [] -- so the fan-out
     workers only ever merge into existing documents.
"""

from __future__ import annotations

import datetime

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS, molecule_id)
from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (
    parse_provider_source)

_log = ChatHealthyLoggingService()

# The catch-all partition for rows whose NPI the spine does not carry
# (LLD v83 III.3). Entity defaults to individual, the existing convention.
CATCH_ALL_STATE = "ALL"

# Threshold between RAW children and STATE-AGGREGATE children (LLD v83 III.3
# decision #4): < RAW_THRESHOLD CMS records -> one raw child per record;
# >= RAW_THRESHOLD -> one aggregate child per state.
RAW_THRESHOLD = 100

_STAMP_CHUNK = 20000


def _distinct_npis(staging_coll, run_id: str) -> list[str]:
    return [str(n) for n in staging_coll.distinct(
        f"raw.{PARTD_COLS['npi']}", {"run_id": run_id}) if n]


def _spine_states(provider_coll, npis: list[str]) -> dict[str, tuple[str, int]]:
    """npi -> (business_address.state, entity 1|2) from the Provider spine."""
    out: dict[str, tuple[str, int]] = {}
    for i in range(0, len(npis), 5000):
        chunk = npis[i:i + 5000]
        for doc in provider_coll.find(
                {"npi": {"$in": chunk}},
                {"npi": 1, "business_address.state": 1, "entity_type_code": 1}):
            npi = str(doc.get("npi") or "").strip()
            state = str((doc.get("business_address") or {}).get("state") or "").strip()
            entity = 2 if str(doc.get("entity_type_code") or "").strip() == "2" else 1
            if npi and state:
                out[npi] = (state, entity)
    return out


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    partd = reg.by_source_name("medicare_partd")
    staging = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    prov_db, prov_name = parse_provider_source(ctx.args.provider_collection)
    provider_coll = mongo[prov_db][prov_name]

    # ---- 0. Record-declared staging indexes (LLD v83 III.8) ---------------
    # The version_source index packages are declarations the run applies:
    # (run_id, raw.Prscrbr_NPI) carries the stamping filter below;
    # (run_id, partition_state) carries the fan-out workers' partition reads;
    # the Part B (run_id, rendering state) index carries the enrich fan-out.
    from pipeline.run_lifecycle.pipeline_index_catalog import (  # noqa: PLC0415
        apply_runtime_index_package)
    dv = int(ctx.args.data_version)
    for pkg in ("medicare_staging_indexes", "medicare_partb_staging_indexes"):
        res = apply_runtime_index_package(mongo, pkg, dv)
        _log.LogPipeline("INFO", "prepare_molecule_masters: index package %s -> %s",
                         pkg, res)

    # ---- 1. Stamp partition_state / partition_entity by NPI ---------------
    npis = _distinct_npis(staging, ctx.run_id)
    spine = _spine_states(provider_coll, npis)
    not_found = sorted(set(npis) - set(spine))
    for npi in not_found:
        rt.record_discrepancy(
            reason="FDA_NPI_not_found_In_Providers",
            step="prepare_molecule_masters", artifact="providermedicare",
            record_key=npi,
            detail={"kind": "npi_not_in_provider_spine",
                    "note": "row(s) stamped to the catch-all partition"})

    # Group NPIs by their (state, entity) partition and stamp in chunks: one
    # update_many per chunk, server-side, so 28M rows are stamped in a few
    # hundred multi-row updates rather than 28M single writes.
    # A business state outside the fifty-plus-DC set (territories, military,
    # foreign) has no partition of its own and maps to the catch-all -- the
    # same rule the state enumerator's "ALL" worker realizes.
    from pipeline.run_lifecycle.steps._partitions import ALL_US_STATES  # noqa: PLC0415
    us_states = set(ALL_US_STATES)
    by_partition: dict[tuple[str, int], list[str]] = {}
    for npi, (state, entity) in spine.items():
        part_state = state if state in us_states else CATCH_ALL_STATE
        by_partition.setdefault((part_state, entity), []).append(npi)
    stamped = 0
    npi_col = f"raw.{PARTD_COLS['npi']}"
    for (state, entity), group in sorted(by_partition.items()):
        for i in range(0, len(group), _STAMP_CHUNK):
            chunk = group[i:i + _STAMP_CHUNK]
            res = staging.update_many(
                {"run_id": ctx.run_id, npi_col: {"$in": chunk}},
                {"$set": {"partition_state": state, "partition_entity": entity}})
            stamped += res.modified_count
    # Catch-all: everything this run left unstamped (spine-missing NPIs).
    res = staging.update_many(
        {"run_id": ctx.run_id, "partition_state": {"$exists": False}},
        {"$set": {"partition_state": CATCH_ALL_STATE, "partition_entity": 1}})
    catch_all_rows = res.modified_count

    # ---- 2. The no-molecule bucket + global per-molecule counts -----------
    gnrc_col = f"raw.{PARTD_COLS['generic']}"
    counts: dict[str, int] = {}
    no_molecule_rows = 0
    for row in staging.aggregate([
            {"$match": {"run_id": ctx.run_id}},
            {"$group": {"_id": f"${gnrc_col}", "n": {"$sum": 1}}},
    ], allowDiskUse=True):
        mid = molecule_id(row.get("_id"))
        if mid is None:
            no_molecule_rows += int(row.get("n") or 0)
            continue
        counts[mid] = counts.get(mid, 0) + int(row.get("n") or 0)
    if no_molecule_rows:
        rt.record_discrepancy(
            reason="FDA_all_other_Attribute_failures",
            step="prepare_molecule_masters", artifact="providermedicare",
            record_key="no_molecule_bucket",
            detail={"kind": "rows_without_molecule",
                    "row_count": no_molecule_rows,
                    "note": "rows whose Gnrc_Name is blank cannot mint a "
                            "molecule_id; counted in the conformance "
                            "arithmetic (LLD v83 III.6, ruling H1)"})

    # ---- 3. Full annual replace (decision #1) ------------------------------
    tgt = reg.by_source_name("providermedicare")
    master_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("providermedicare")]
    spec_tgt = reg.by_source_name("specialtymedicare")
    spec_coll = mongo[spec_tgt.public_data_db][reg.public_data_collection_name("specialtymedicare")]
    cleared_masters = master_coll.delete_many({}).deleted_count
    cleared_specialty = spec_coll.delete_many({}).deleted_count

    # ---- 4. Master stubs ----------------------------------------------------
    built_at = datetime.datetime.utcnow().isoformat()
    stubs = []
    for mid, n in counts.items():
        stubs.append({
            "_id": mid,
            "molecule_id": mid,
            "fda": {"indications": [], "drug_class": [],
                    "trust": {"max": None, "min": None, "median": None, "avg": None}},
            "totals": {"tot_clms": 0, "tot_30day_fills": 0,
                       "tot_day_suply": 0, "tot_drug_cst": 0},
            "total_cms_record_count": 0,
            "ge65_usage_suppressed_count": 0,
            "state_aggregated": n >= RAW_THRESHOLD,
            "applied_partitions": [],
            "children": [],
            "provenance": {"source_run_id": ctx.run_id,
                           "source_collection": "StagingMedicarePartD_v_1",
                           "built_at": built_at},
        })
    if stubs:
        master_coll.insert_many(stubs, ordered=False)

    _log.LogPipeline(
        "INFO",
        "prepare_molecule_masters: npis=%d spine_missing=%d stamped=%d "
        "catch_all_rows=%d molecules=%d state_aggregated=%d "
        "no_molecule_rows=%d cleared_masters=%d cleared_specialty=%d",
        len(npis), len(not_found), stamped, catch_all_rows, len(counts),
        sum(1 for n in counts.values() if n >= RAW_THRESHOLD),
        no_molecule_rows, cleared_masters, cleared_specialty)
    return {"step": "prepare_molecule_masters",
            "distinct_npis": len(npis),
            "spine_missing_npis": len(not_found),
            "rows_stamped": stamped,
            "catch_all_rows": catch_all_rows,
            "molecules": len(counts),
            "state_aggregated_molecules":
                sum(1 for n in counts.values() if n >= RAW_THRESHOLD),
            "no_molecule_rows": no_molecule_rows,
            "cleared_masters": cleared_masters,
            "cleared_specialty": cleared_specialty}


def execute(ctx):
    return run_step(ctx)
