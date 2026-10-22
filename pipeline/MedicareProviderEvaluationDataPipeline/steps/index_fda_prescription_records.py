# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""index_fda_prescription_records (EPIC-010-F-007) -- stamp a sequential fda_record_number
on the staged PRESCRIPTION openFDA labels so build_fda_label_resolution can
range-partition the FDA work across worker PROCESSES (LLD I.3.4a).

A label is in scope when it carries indications_and_usage prose and is NOT OTC
(openfda.product_type naming a HUMAN OTC DRUG). OTC labels are left unstamped and so are
excluded from the fdaProductToDiagnosisResolution collection. Serial, idempotent
(already-stamped staging is not re-stamped). Reads carry no run_id: a full load
drains-then-replaces the staging collection, so the staged rows are the one truth
regardless of which run staged them (Start At, operator design 2026-10-22).
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


def _is_otc(label: dict) -> bool:
    for value in (label.get("openfda") or {}).get("product_type") or []:
        if "OTC" in str(value).upper():
            return True
    return False


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    ofda = reg.by_source_name("openfda_labels")
    staging_coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]

    # Build mode: this serial pre-pass drains the FDA leg's per-label result
    # store so the fan-out repopulates it from zero -- drain-then-replace, the
    # same single-truth rule the staging loader applies. Without the drain,
    # run-id-free folds would mix stale labels from an older staging.
    if bool(ctx.args.build_fda_drug):
        from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (  # noqa: PLC0415
            TRANSIENT_COLLECTION)
        removed = mongo[ofda.staging_db][TRANSIENT_COLLECTION].delete_many({}).deleted_count
        _log.LogPipeline(
            "INFO", "index_fda_prescription_records: drained transient rows=%d", removed)

    already = staging_coll.count_documents(
        {"fda_record_number": {"$exists": True}})
    if already:
        _log.LogPipeline(
            "INFO", "index_fda_prescription_records: already stamped count=%d",
            already)
        return {"step": "index_fda_prescription_records", "fda_records": already,
                "restamped": False}

    staging_coll.create_index([("fda_record_number", 1)])
    number = otc_excluded = no_indication = 0
    ops: list[UpdateOne] = []
    for doc in staging_coll.find({}).sort("_id", 1):
        label = doc.get("raw") or {}
        if not (label.get("indications_and_usage") or []):
            no_indication += 1
            continue
        if _is_otc(label):
            otc_excluded += 1
            continue
        number += 1
        ops.append(UpdateOne({"_id": doc["_id"]},
                             {"$set": {"fda_record_number": number}}))
        if len(ops) >= 1000:
            staging_coll.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        staging_coll.bulk_write(ops, ordered=False)

    if number == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="index_fda_prescription_records",
            message=("index_fda_prescription_records: no prescription FDA labels "
                     "staged; openFDA staging empty or all OTC"))
    _log.LogPipeline(
        "INFO",
        "index_fda_prescription_records: stamped=%d otc_excluded=%d no_indication=%d",
        number, otc_excluded, no_indication)
    return {"step": "index_fda_prescription_records", "fda_records": number,
            "otc_excluded": otc_excluded, "no_indication": no_indication,
            "restamped": True}


def execute(ctx):
    return run_step(ctx)
