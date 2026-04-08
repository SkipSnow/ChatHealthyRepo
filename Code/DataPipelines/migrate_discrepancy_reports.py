# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""One-time migration: copy admin.PipelineDiscrepancyReport → admin.PipelineDiscrepancyReports.

Adds job_name, job_status (object with optional fail_reason), and pipeline_run
sub-document. Fields are written in canonical order. Safe to re-run — uses
replace_one so already-migrated records are updated in place.

Run from the DataPipelines directory:
    python migrate_discrepancy_reports.py
"""

import logging
import os
import sys

from pymongo import MongoClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
sys.stdout.reconfigure(encoding="utf-8")

SRC  = ("admin", "PipelineDiscrepancyReport")
DEST = ("admin", "PipelineDiscrepancyReports")


def _infer_job_name(doc: dict) -> str:
    if doc.get("job") == "CountyEnrichment":
        return "County Enrichment"
    return "Provider Load"


def _infer_job_status(doc: dict, job_name: str) -> dict:
    """Return job_status sub-document {status, fail_reason?}."""
    if job_name == "County Enrichment":
        summary = doc.get("summary", {})
        pct = (summary.get("total_enriched") or {}).get("pct_of_addressable")
        if pct is None:
            return {"status": "unknown"}
        if pct >= 98.0:
            return {"status": "succeed"}
        return {"status": "fail", "fail_reason": (
            f"Enrichment SLA not met: {pct:.1f}% enriched "
            f"(required 98% of addressable providers)"
        )}
    else:  # Provider Load
        reconcile = doc.get("reconciliation", {})
        match = reconcile.get("match")
        if match is None:
            return {"status": "unknown"}
        if match:
            failed = (doc.get("summary") or {}).get("failed_workers", 0)
            if failed:
                return {"status": "fail", "fail_reason": f"{failed} worker(s) failed"}
            return {"status": "succeed"}
        exp = reconcile.get("expected_records", "N/A")
        ins = reconcile.get("inserted_records", "N/A")
        fai = reconcile.get("failed_records", "N/A")
        return {"status": "fail", "fail_reason": (
            f"Reconciliation mismatch: expected={exp} inserted={ins} failed={fai}"
        )}


def _build_ordered_doc(
    doc: dict, original_id, job_name: str, job_status: dict
) -> dict:
    """Rebuild document in canonical field order."""
    worker_results = doc.get("worker_results") or doc.get("pair_results")

    pipeline_run = {}
    if doc.get("load_id"):
        pipeline_run["load_id"] = doc["load_id"]
    if doc.get("version"):
        pipeline_run["version"] = doc["version"]

    ordered: dict = {"job_name": job_name, "job_status": job_status}
    ordered.update({
        "datetime":       doc.get("datetime"),
        "reconciliation": doc.get("reconciliation"),
        "summary":        doc.get("summary"),
        "failed_rows":    doc.get("failed_rows", []),
        "worker_results": worker_results,
        "pipeline_run":   pipeline_run,
        "original_id":    original_id,
    })
    return {k: v for k, v in ordered.items() if v is not None}


def migrate() -> None:
    client = MongoClient(os.environ["MONGO_connectionString"])
    src_coll  = client[SRC[0]][SRC[1]]
    dest_coll = client[DEST[0]][DEST[1]]

    total = src_coll.count_documents({})
    logging.info("Source has %d records.", total)

    replaced = inserted = 0
    for doc in src_coll.find({}):
        original_id = doc["_id"]
        job_name = _infer_job_name(doc)
        job_status = _infer_job_status(doc, job_name)
        ordered = _build_ordered_doc(doc, original_id, job_name, job_status)

        result = dest_coll.replace_one(
            {"original_id": original_id},
            ordered,
            upsert=True,
        )
        if result.matched_count:
            replaced += 1
        else:
            inserted += 1

    logging.info("Migration complete — inserted=%d replaced=%d", inserted, replaced)


if __name__ == "__main__":
    migrate()
