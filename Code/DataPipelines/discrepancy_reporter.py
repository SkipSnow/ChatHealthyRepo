# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""DiscrepancyReporter — writes pipeline run results to admin.PipelineDiscrepancyReports
and sends an email notification via SparkPost on completion.
"""

import logging
import os
from datetime import datetime, timezone

from pymongo import MongoClient
from sparkpost import SparkPost

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


REPORT_COLLECTION = "admin.PipelineDiscrepancyReports"


class DiscrepancyReporter:

    def send(self, config: dict) -> dict:
        worker_results = config.get("worker_results", [])
        reconcile = config.get("reconcile_result", {})
        version = config.get("version", "unknown")
        load_id = config.get("load_id", "unknown")

        expected_records = reconcile.get("expected_records", "N/A")
        inserted_records = reconcile.get("inserted_records", "N/A")
        failed_records = reconcile.get("failed_records", "N/A")
        reconcile_match = reconcile.get("match", False)

        total_loaded = sum(r.get("num_records", 0) for r in worker_results)
        failed_workers = [r for r in worker_results if not r.get("success", True)]

        # ── Collect failed rows across all workers ────────────────────────────
        all_failed_rows = [
            {"worker_id": r["worker_id"], **row}
            for r in worker_results
            for row in r.get("failed_rows", [])
        ]
        total_failed_rows = sum(r.get("rows_failed", 0) for r in worker_results)

        succeeded = reconcile_match and not failed_workers
        job_status: dict = {"status": "succeed" if succeeded else "fail"}
        if not succeeded:
            reasons = []
            if not reconcile_match:
                reasons.append(
                    f"Reconciliation mismatch: expected={expected_records} "
                    f"inserted={inserted_records} failed={failed_records}"
                )
            if failed_workers:
                reasons.append(f"{len(failed_workers)} worker(s) failed")
            job_status["fail_reason"] = "; ".join(reasons)

        # ── Write report to MongoDB ───────────────────────────────────────────
        report = {"job_name": "Provider Load", "job_status": job_status}
        report.update({
            "datetime": datetime.now(timezone.utc).isoformat(),
            "reconciliation": reconcile,
            "summary": {
                "total_workers": len(worker_results),
                "failed_workers": len(failed_workers),
                "total_loaded": total_loaded,
                "total_failed_rows": total_failed_rows,
            },
            "failed_rows": all_failed_rows,  # up to 20 per worker; overflow in Azure logs
            "worker_results": worker_results,
            "pipeline_run": {
                "load_id": load_id,
                "version": version,
            },
        })

        db_name, coll_name = REPORT_COLLECTION.split(".", 1)
        _get_mongo_client()[db_name][coll_name].insert_one(report)
        logging.info("Discrepancy report written to %s for load_id=%s", REPORT_COLLECTION, load_id)

        # ── SparkPost email notification ──────────────────────────────────────
        status_line = "Reconciled OK" if reconcile_match else "MISMATCH — INVESTIGATE"
        subject = f"NPI Load {'OK' if reconcile_match else 'MISMATCH'} — v{version}"
        body = (
            f"NPI Load complete — v{version}\n"
            f"Expected: {expected_records}  Inserted: {inserted_records}  Failed: {failed_records}\n"
            f"Reconciliation: {status_line}\n"
            f"Failed workers: {len(failed_workers)}\n"
            f"Load ID: {load_id}"
        )

        api_key    = os.environ.get("SPARKMAIL_API_KEY")
        from_email = os.environ.get("NOTIFICATION_FROM_EMAIL")
        to_email   = os.environ.get("NOTIFICATION_TO_EMAIL")
        if api_key and from_email and to_email:
            try:
                sp = SparkPost(api_key)
                sp.transmissions.send(
                    recipients=[to_email],
                    from_email=from_email,
                    subject=subject,
                    text=body,
                )
                logging.info("SparkPost email sent to %s", to_email)
            except Exception as exc:
                logging.error("SparkPost send failed: %s", exc)
        else:
            logging.warning("SparkPost credentials not configured — load_id: %s", load_id)

        return {
            "report_collection": REPORT_COLLECTION,
            "total_loaded": total_loaded,
            "reconcile_match": reconcile_match,
        }
