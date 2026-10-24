# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_contract_conformance (EPIC-010-F-007) -- the audits, one per
published collection.

Three collections, three methods, chosen by size:

  * ProviderMedicare -- 26.7 million records, too many to re-read. Audited by
    the deterministic cross-section sample (medicare_audit_engine): each
    sampled record is RECOMPUTED from the CMS rows that built it and compared
    with what was published, and validated against the published contract.
    Recomputation is the stronger claim: a schema sweep shows only that the
    builder agrees with its own output.
  * fdaProductToDiagnosisResolution -- 20,142 records. Audited whole: every
    record validated, plus the fold arithmetic (the sum of folded label set
    ids against the run's in-scope label count).
  * SpecialtyMedicare -- one record per NUCC code. Audited whole.

Runs before the transient attributes are dropped, because the sampled records
are recomputed from the source rows of their NPI. source_state is gone
by then -- drop_transient_attributes_and_records runs before the audits --
so the source rows are found by Prscrbr_NPI and filtered to the molecule.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_audit_engine import (
    choose_sample, compare, recompute_from_source)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_conformance_engine import (
    _validator)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS, molecule_id)

_log = ChatHealthyLoggingService()

_CONTRACTS = {
    "providermedicare": "ChatHealthyProviderMedicareSchema.json",
    "specialtymedicare": "ChatHealthySpecialtyMedicareSchema.json",
    "fda_drug": "ChatHealthyFdaProductToDiagnosisResolutionSchema.json",
}


def _audit_levers(ctx) -> dict:
    """The sampler's levers, from durable configuration; no defaults (H7)."""
    from pipeline.run_lifecycle.pipeline_config import load_pipeline_config  # noqa: PLC0415
    cfg = load_pipeline_config(env_prefix=ctx.env_prefix)
    for entry in cfg.get("pipeline_config") or []:
        if entry.get("env") == ctx.env_prefix and entry.get("audit"):
            return entry["audit"]
    raise ChatHealthyException(
        mode="config_error",
        component="medicare_contract_conformance",
        message=(f"no audit levers declared for env {ctx.env_prefix!r} in the "
                 f"pipeline configuration; the audit reads volume_bands, "
                 f"sampled_record_cap and mismatch_is_fatal from the record"))


def _audit_whole(coll, contract: str, name: str) -> dict[str, Any]:
    """Validate every record of a small published collection."""
    validator = _validator(contract)
    checked = nonconforming = 0
    offenders: list[dict] = []
    for doc in coll.find({}):
        checked += 1
        errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        if errors:
            nonconforming += 1
            if len(offenders) < 10:
                offenders.append({"_id": str(doc.get("_id")),
                                  "error": f"{list(errors[0].path)}: "
                                           f"{errors[0].message}"})
    _log.LogPipeline("INFO", "audit[%s]: whole collection, checked=%d "
                     "nonconforming=%d", name, checked, nonconforming)
    return {"method": "whole", "checked": checked,
            "nonconforming": nonconforming, "offenders": offenders}


def _audit_providermedicare(target_coll, partd_coll, levers: dict,
                            record_discrepancy) -> dict[str, Any]:
    """The deterministic cross-section: sample, recompute, validate."""
    candidates = list(target_coll.find(
        {}, {"_id": 1, "npi": 1, "molecule_id": 1, "record_count": 1,
             "indications": 1,
             "brand_names": 1, "generic_names": 1,
             "tot_clms": 1, "tot_day_suply": 1}))
    prescriber_counts: dict[str, int] = defaultdict(int)
    for rec in candidates:
        prescriber_counts[str(rec.get("molecule_id") or "")] += 1

    sample = choose_sample(candidates, dict(prescriber_counts),
                           bands=int(levers["volume_bands"]),
                           cap=int(levers["sampled_record_cap"]))

    validator = _validator(_CONTRACTS["providermedicare"])
    mismatched: list[dict] = []
    nonconforming: list[dict] = []
    for rec in sample:
        npi = str(rec.get("npi") or "")
        mid = str(rec.get("molecule_id") or "")
        rows = [doc.get("raw") or {} for doc in partd_coll.find(
            {f"raw.{PARTD_COLS['npi']}": npi})]
        rows = [r for r in rows
                if molecule_id(r.get(PARTD_COLS["generic"])) == mid]
        problems = compare(rec, recompute_from_source(rows)) if rows else [
            "no source rows found for this record"]
        if problems:
            mismatched.append({"npi": npi, "molecule_id": mid,
                               "cell": rec.get("_audit_cell"),
                               "problems": problems})
        errors = sorted(validator.iter_errors(
            {k: v for k, v in rec.items() if k != "_audit_cell"}),
            key=lambda e: list(e.path))
        if errors:
            nonconforming.append({"npi": npi, "molecule_id": mid,
                                  "error": f"{list(errors[0].path)}: "
                                           f"{errors[0].message}"})

    cells = [r.get("_audit_cell") for r in sample]
    _log.LogPipeline(
        "INFO",
        "audit[providermedicare]: population=%d sampled=%d cells=%d "
        "mismatched=%d nonconforming=%d",
        len(candidates), len(sample), len(cells), len(mismatched),
        len(nonconforming))
    for m in mismatched:
        record_discrepancy(
            reason="FDA_all_other_Attribute_failures",
            step="medicare_contract_conformance", artifact="providermedicare",
            record_key=f"{m['npi']}:{m['molecule_id']}",
            detail={"kind": "audit_recomputation_mismatch", **m})
    return {"method": "deterministic cross-section sample",
            "population": len(candidates), "sampled": len(sample),
            "cells": cells, "mismatched": mismatched,
            "nonconforming": nonconforming}


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    levers = _audit_levers(ctx)

    def coll_for(source_name):
        entry = reg.by_source_name(source_name)
        return mongo[entry.public_data_db][
            reg.public_data_collection_name(source_name)]

    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][
        reg.staging_collection_name("medicare_partd")]

    report: dict[str, Any] = {
        "providermedicare": _audit_providermedicare(
            coll_for("providermedicare"), partd_coll, levers,
            rt.record_discrepancy),
        "fda_drug": _audit_whole(coll_for("fda_drug"),
                                 _CONTRACTS["fda_drug"], "fda_drug"),
        "specialtymedicare": _audit_whole(coll_for("specialtymedicare"),
                                          _CONTRACTS["specialtymedicare"],
                                          "specialtymedicare"),
    }

    nonconforming = (len(report["providermedicare"]["nonconforming"])
                     + report["fda_drug"]["nonconforming"]
                     + report["specialtymedicare"]["nonconforming"])
    if nonconforming:
        raise ChatHealthyException(
            mode="runtime_error",
            component="medicare_contract_conformance",
            message=(f"{nonconforming} audited record(s) do not conform to "
                     f"their contract -- publishing a non-compliant record is "
                     f"a software bug"),
            report=report)

    mismatched = len(report["providermedicare"]["mismatched"])
    if mismatched and bool(levers["mismatch_is_fatal"]):
        raise ChatHealthyException(
            mode="runtime_error",
            component="medicare_contract_conformance",
            message=(f"{mismatched} audited record(s) disagree with the "
                     f"source rows they were built from"),
            report=report)

    return {"step": "medicare_contract_conformance", "audits": report}


def execute(ctx):
    return run_step(ctx)
