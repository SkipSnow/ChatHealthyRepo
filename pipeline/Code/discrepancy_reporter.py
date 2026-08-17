from chathealthy_lib.logging_service import ChatHealthyLoggingService
# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""DiscrepancyReporter — writes pipeline run results to admin.PipelineDiscrepancyReports
and sends an email notification via SparkPost on completion.
"""

import os
from datetime import datetime, timezone

from sparkpost import SparkPost


from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

# This file is metadata. One cluster, one database, stated here and nowhere
# else: pipelineAdmin on the front end. It wrote to `admin`, MongoDB's reserved
# system database, which is owned by neither cluster and which no reader of
# pipeline metadata has ever looked in.


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

        # ── Enrichment stats from staging collection ──────────────────────────
        _env = config.get("env_prefix", os.environ.get("ENV_PREFIX", "dev"))
        provider_collection = config.get("provider_collection", "PipelinePublicHealthData.providers")
        db_name, coll_name = provider_collection.split(".", 1)
        staging_coll = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")[db_name][coll_name]

        source_counts: dict = {
            (doc["_id"] or "unenriched"): doc["count"]
            for doc in staging_coll.aggregate([
                {"$match": {"county": {"$exists": True}}},
                {"$group": {"_id": "$county.source", "count": {"$sum": 1}}},
            ])
        }
        total_providers = sum(source_counts.values())
        out_of_scope_total = source_counts.get("out_of_scope", 0)
        # Per-pass failure labels sum into one "geocoder funnel gave up" total.
        # The four labels capture WHERE the funnel stopped (pass2/3/4/6);
        # for the load-summary purpose we count them as one bucket.
        geocoder_failed_by_pass: dict = {
            "geocoder_pass2_failed": source_counts.get("geocoder_pass2_failed", 0),
            "geocoder_pass3_failed": source_counts.get("geocoder_pass3_failed", 0),
            "geocoder_pass4_failed": source_counts.get("geocoder_pass4_failed", 0),
            "geocoder_pass6_failed": source_counts.get("geocoder_pass6_failed", 0),
        }
        geocoder_failed = sum(geocoder_failed_by_pass.values())
        unenriched = source_counts.get("unenriched", 0)
        addressable = total_providers - out_of_scope_total
        enriched = addressable - geocoder_failed - unenriched
        pct_enriched = round(100 * enriched / addressable, 1) if addressable else 0.0
        unresolved_in_scope = geocoder_failed + unenriched

        out_of_scope_by_reason: dict = {
            (doc["_id"] or "legacy"): doc["count"]
            for doc in staging_coll.aggregate([
                {"$match": {"county.source": "out_of_scope"}},
                {"$group": {"_id": "$county.reason", "count": {"$sum": 1}}},
            ])
        }

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
                "total_providers": total_providers,
                "addressable": addressable,
                "enriched": enriched,
                "pct_enriched": pct_enriched,
                "unresolved_in_scope": unresolved_in_scope,
                "geocoder_failed_total": geocoder_failed,
                "geocoder_failed_by_pass": geocoder_failed_by_pass,
                "unenriched": unenriched,
                "out_of_scope_total": out_of_scope_total,
                "out_of_scope_by_reason": out_of_scope_by_reason,
            },
            "failed_rows": all_failed_rows,  # up to 20 per worker; overflow in Azure logs
            "worker_results": worker_results,
            "pipeline_run": {
                "load_id": load_id,
                "version": version,
            },
        })

        db_name, coll_name = "pipelineAdmin.PipelineDiscrepancyReports".split(".", 1)
        ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")[db_name][coll_name].insert_one(report)
        ChatHealthyLoggingService().info("Discrepancy report written to %s for load_id=%s", "pipelineAdmin.PipelineDiscrepancyReports", load_id)

        # ── SparkPost email notification ──────────────────────────────────────
        status_line = "Reconciled OK" if reconcile_match else "MISMATCH — INVESTIGATE"
        subject = f"NPI Load {'OK' if reconcile_match else 'MISMATCH'} — v{version}"
        reason_lines = "\n".join(
            f"  {reason}: {count:,}"
            for reason, count in sorted(out_of_scope_by_reason.items(), key=lambda x: -x[1])
        ) or "  (none)"
        body = (
            f"NPI Load complete — v{version}\n"
            f"Expected: {expected_records}  Inserted: {inserted_records}  Failed: {failed_records}\n"
            f"Reconciliation: {status_line}\n"
            f"Failed workers: {len(failed_workers)}\n"
            f"Load ID: {load_id}\n"
            f"\n"
            f"Enrichment summary:\n"
            f"  Total providers:     {total_providers:,}\n"
            f"  Addressable:         {addressable:,}\n"
            f"  Enriched:            {enriched:,} ({pct_enriched}%)\n"
            f"  Unresolved in-scope: {unresolved_in_scope:,} "
            f"(funnel gave up: {geocoder_failed:,} "
            f"[pass2={geocoder_failed_by_pass['geocoder_pass2_failed']:,}, "
            f"pass3={geocoder_failed_by_pass['geocoder_pass3_failed']:,}, "
            f"pass4={geocoder_failed_by_pass['geocoder_pass4_failed']:,}, "
            f"pass6={geocoder_failed_by_pass['geocoder_pass6_failed']:,}], "
            f"unenriched: {unenriched:,})\n"
            f"\n"
            f"Out-of-scope: {out_of_scope_total:,} total\n"
            f"{reason_lines}"
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
                ChatHealthyLoggingService().info("SparkPost email sent to %s", to_email)
            except Exception as exc:
                ChatHealthyLoggingService().error("SparkPost send failed: %s", exc)
        else:
            ChatHealthyLoggingService().warning("SparkPost credentials not configured — load_id: %s", load_id)

        return {
            "report_collection": "pipelineAdmin.PipelineDiscrepancyReports",
            "total_loaded": total_loaded,
            "reconcile_match": reconcile_match,
        }
