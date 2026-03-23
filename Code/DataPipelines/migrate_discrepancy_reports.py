# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""One-time migration: copy admin.PipelineDiscrepancyReport → admin.PipelineDiscrepancyReports.

Adds job_name, job_status, and fail_reason to each document based on existing fields.
Safe to run multiple times — skips documents already present in the destination
by matching on original _id.

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
    """Derive job_name from existing document shape."""
    if doc.get("job") == "CountyEnrichment":
        return "County Enrichment"
    return "Provider Load"


def _infer_job_status(doc: dict, job_name: str) -> tuple[str, str | None]:
    """Return (job_status, fail_reason | None) inferred from existing fields."""
    if job_name == "County Enrichment":
        summary = doc.get("summary", {})
        pct = (summary.get("total_enriched") or {}).get("pct_of_addressable")
        if pct is None:
            return "unknown", None
        if pct >= 98.0:
            return "succeed", None
        return "fail", (
            f"Enrichment SLA not met: {pct:.1f}% enriched (required 98% of addressable providers)"
        )
    else:  # Provider Load
        reconcile = doc.get("reconciliation", {})
        match = reconcile.get("match")
        if match is None:
            return "unknown", None
        if match:
            failed_workers = (doc.get("summary") or {}).get("failed_workers", 0)
            if failed_workers:
                return "fail", f"{failed_workers} worker(s) failed"
            return "succeed", None
        exp = reconcile.get("expected_records", "N/A")
        ins = reconcile.get("inserted_records", "N/A")
        fai = reconcile.get("failed_records", "N/A")
        return "fail", (
            f"Reconciliation mismatch: expected={exp} inserted={ins} failed={fai}"
        )


def migrate() -> None:
    client = MongoClient(os.environ["MONGO_connectionString"])
    src_coll  = client[SRC[0]][SRC[1]]
    dest_coll = client[DEST[0]][DEST[1]]

    existing_ids = set(
        doc["original_id"] for doc in dest_coll.find(
            {"original_id": {"$exists": True}}, {"original_id": 1}
        )
    )
    logging.info("Destination already has %d migrated records.", len(existing_ids))

    docs = list(src_coll.find({}))
    logging.info("Source has %d records to migrate.", len(docs))

    migrated = skipped = 0
    for doc in docs:
        original_id = doc["_id"]
        if original_id in existing_ids:
            skipped += 1
            continue

        job_name = _infer_job_name(doc)
        job_status, fail_reason = _infer_job_status(doc, job_name)

        doc.pop("_id", None)
        doc.pop("job", None)          # replaced by job_name
        doc["job_name"]     = job_name
        doc["job_status"]   = job_status
        doc["original_id"]  = original_id   # preserve link to source
        if fail_reason:
            doc["fail_reason"] = fail_reason

        dest_coll.insert_one(doc)
        migrated += 1

    logging.info("Migration complete — migrated=%d skipped=%d", migrated, skipped)


if __name__ == "__main__":
    migrate()
