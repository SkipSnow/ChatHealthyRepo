# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_contract_conformance (EPIC-010-F-007, LLD v83 III.6/III.7) — the
conformance gate.

Serial, after the collections are built. Three checks with the operator-ruled
semantics: the Part D exactly-once arithmetic (H1, finding) including the
no-molecule bucket recounted from staging; the FDA fold arithmetic (H2,
finding) against the run's in-scope label count; JSON-Schema validation of
every published record (H3, HARD FAIL). Resolves collections through the
registry. No external calls.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_conformance_engine import check_conformance
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS, molecule_id)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    collections = {}
    for source_name in ("providermedicare", "specialtymedicare", "fda_drug"):
        entry = reg.by_source_name(source_name)
        collections[source_name] = mongo[entry.public_data_db][
            reg.public_data_collection_name(source_name)]

    partd = reg.by_source_name("medicare_partd")
    staging_partd = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    # No run_id filters below: the drained-and-replaced staging is the one
    # truth (Start At, operator design 2026-10-22).
    cms_row_count = staging_partd.count_documents({})

    # The no-molecule bucket, recounted here so the check is self-contained
    # (the same arithmetic the pre-pass measured and recorded).
    gnrc_col = f"raw.{PARTD_COLS['generic']}"
    no_molecule_rows = 0
    for row in staging_partd.aggregate([
            {"$group": {"_id": f"${gnrc_col}", "n": {"$sum": 1}}},
    ], allowDiskUse=True):
        if molecule_id(row.get("_id")) is None:
            no_molecule_rows += int(row.get("n") or 0)

    # The FDA in-scope label count the fold arithmetic compares against:
    # this run's stamped (non-OTC, prescription) labels. Only meaningful on a
    # build run; on consume runs the FDA arithmetic is skipped.
    fda_in_scope = None
    if bool(ctx.args.build_fda_drug):
        ofda = reg.by_source_name("openfda_labels")
        staging_fda = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
        fda_in_scope = staging_fda.count_documents(
            {"fda_record_number": {"$exists": True}})

    result = check_conformance(
        collections=collections,
        cms_partd_row_count=cms_row_count,
        no_molecule_rows=no_molecule_rows,
        fda_in_scope_labels=fda_in_scope,
        record_discrepancy=rt.record_discrepancy,
    )
    _log.LogPipeline("INFO", "medicare_contract_conformance: %s",
                     {k: v["checked"] for k, v in result["report"].items()})
    return result


def execute(ctx):
    return run_step(ctx)
