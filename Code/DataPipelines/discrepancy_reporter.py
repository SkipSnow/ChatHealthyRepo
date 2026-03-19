# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""DiscrepancyReporter — writes pipeline run results to admin.PipelineDiscrepancyReport
and sends a Pushover notification on completion.
"""

import logging
import os
from datetime import datetime, timezone

import requests
from pymongo import MongoClient

PUSHOVER_API = "https://api.pushover.net/1/messages.json"
REPORT_COLLECTION = "admin.PipelineDiscrepancyReport"


class DiscrepancyReporter:

    def send(self, config: dict) -> dict:
        pair_results = config.get("pair_results", [])
        reconcile = config.get("reconcile_result", {})
        version = config.get("version", "unknown")
        load_id = config.get("load_id", "unknown")

        expected_records = reconcile.get("expected_records", "N/A")
        inserted_records = reconcile.get("inserted_records", "N/A")
        failed_records = reconcile.get("failed_records", "N/A")
        reconcile_match = reconcile.get("match", False)

        total_loaded = sum(
            r.get("worker", {}).get("num_records", 0) for r in pair_results
        )
        failed_workers = [
            r for r in pair_results if not r.get("worker", {}).get("success", True)
        ]
        failed_enrichments = [
            r for r in pair_results if not r.get("enrich", {}).get("success", True)
        ]

        # ── Write report to MongoDB ───────────────────────────────────────────
        report = {
            "load_id": load_id,
            "version": version,
            "datetime": datetime.now(timezone.utc).isoformat(),
            "reconciliation": reconcile,
            "summary": {
                "total_workers": len(pair_results),
                "failed_load_workers": len(failed_workers),
                "failed_enrichment_workers": len(failed_enrichments),
                "total_loaded": total_loaded,
            },
            "pair_results": pair_results,
        }

        db_name, coll_name = REPORT_COLLECTION.split(".", 1)
        client = MongoClient(os.environ["MONGO_connectionString"])
        try:
            client[db_name][coll_name].insert_one(report)
            logging.info("Discrepancy report written to %s for load_id=%s", REPORT_COLLECTION, load_id)
        finally:
            client.close()

        # ── Pushover notification ─────────────────────────────────────────────
        status_line = "Reconciled OK" if reconcile_match else "MISMATCH — INVESTIGATE"
        message = (
            f"NPI Load complete — v{version}\n"
            f"Expected: {expected_records}  Inserted: {inserted_records}  Failed: {failed_records}\n"
            f"Reconciliation: {status_line}\n"
            f"Failed workers: {len(failed_workers)}\n"
            f"Load ID: {load_id}"
        )

        pushover_token = os.environ.get("PUSHOVER_TOKEN")
        pushover_user = os.environ.get("PUSHOVER_USER")
        if pushover_token and pushover_user:
            resp = requests.post(
                PUSHOVER_API,
                data={
                    "token": pushover_token,
                    "user": pushover_user,
                    "message": message,
                    "title": f"NPI Load — {version}",
                },
                timeout=10,
            )
            logging.info("Pushover response: %s", resp.status_code)
        else:
            logging.warning("Pushover credentials not configured — load_id: %s", load_id)

        return {
            "report_collection": REPORT_COLLECTION,
            "total_loaded": total_loaded,
            "reconcile_match": reconcile_match,
        }
