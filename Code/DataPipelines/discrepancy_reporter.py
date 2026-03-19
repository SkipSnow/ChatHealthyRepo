"""DiscrepancyReporter — generates Excel report, uploads to Azure Blob,
creates a 24-hour SAS URL, and sends a Pushover notification with the link.
"""

import io
import logging
import os
from datetime import datetime, timedelta, timezone

import openpyxl
import requests
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, generate_blob_sas

PUSHOVER_API = "https://api.pushover.net/1/messages.json"

# Excel detail tab row cap. If pair_results exceeds this, only the first
# DETAIL_ROW_LIMIT worker rows are written. The full set is always in the blob.
DETAIL_ROW_LIMIT = 10_000


class DiscrepancyReporter:

    def send(self, config: dict) -> dict:
        pair_results = config.get("pair_results", [])
        reconcile = config.get("reconcile_result", {})
        version = config.get("version", "unknown")
        container = config.get("blob_container", "provider-data")

        total_loaded = sum(
            r.get("worker", {}).get("num_records", 0) for r in pair_results
        )
        failed_workers = [
            r for r in pair_results if not r.get("worker", {}).get("success", True)
        ]
        failed_enrichments = [
            r for r in pair_results if not r.get("enrich", {}).get("success", True)
        ]

        # ── Build Excel workbook ──────────────────────────────────────────────
        wb = openpyxl.Workbook()

        # Summary tab
        ws = wb.active
        ws.title = "Summary"
        ws.append(["NPI Provider Data Load Report"])
        ws.append(["Version", version])
        ws.append(["Date", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")])
        ws.append([])
        ws.append(["Total Records Loaded", total_loaded])
        ws.append(["Total Workers", len(pair_results)])
        ws.append(["Failed Load Workers", len(failed_workers)])
        ws.append(["Failed Enrichment Workers", len(failed_enrichments)])
        ws.append([])
        ws.append(["Reconciliation"])
        ws.append(["CSV Record Count", reconcile.get("csv_record_count", "N/A")])
        ws.append(["Loaded Count", reconcile.get("loaded_count", "N/A")])
        ws.append(["Match", "YES" if reconcile.get("match") else "NO — INVESTIGATE"])

        # Detail tab — capped at DETAIL_ROW_LIMIT rows
        wd = wb.create_sheet("Detail")
        wd.append(["Worker ID", "Stage", "Success", "Records", "Error / Failed Records"])
        rows_written = 0
        truncated = False
        for r in pair_results:
            if rows_written >= DETAIL_ROW_LIMIT:
                truncated = True
                break
            w = r.get("worker", {})
            wd.append([
                w.get("worker_id", ""),
                "Load",
                "YES" if w.get("success") else "NO",
                w.get("num_records", 0),
                w.get("error", ""),
            ])
            rows_written += 1

            if rows_written >= DETAIL_ROW_LIMIT:
                truncated = True
                break
            e = r.get("enrich", {})
            failed_list = ", ".join(str(f) for f in e.get("failed", []))
            wd.append([
                e.get("worker_id", ""),
                "Enrich",
                "YES" if e.get("success") else "NO",
                "",
                failed_list or "",
            ])
            rows_written += 1

        if truncated:
            wd.append([
                "", "", "",
                f"** Truncated at {DETAIL_ROW_LIMIT} rows. Full results in blob. **",
                "",
            ])

        # ── Upload to blob ────────────────────────────────────────────────────
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        blob_name = f"reports/npi_load_{version}_{timestamp}.xlsx"

        excel_bytes = io.BytesIO()
        wb.save(excel_bytes)
        excel_bytes.seek(0)

        conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        service = BlobServiceClient.from_connection_string(conn_str)
        container_client = service.get_container_client(container)
        container_client.get_blob_client(blob_name).upload_blob(
            excel_bytes, overwrite=True
        )

        # ── Generate 24-hour SAS URL ──────────────────────────────────────────
        account_name = service.account_name
        account_key = service.credential.account_key
        sas_token = generate_blob_sas(
            account_name=account_name,
            container_name=container,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        report_url = (
            f"https://{account_name}.blob.core.windows.net"
            f"/{container}/{blob_name}?{sas_token}"
        )

        # ── Pushover notification ─────────────────────────────────────────────
        status_line = "✅ Reconciled" if reconcile.get("match") else "⚠️ MISMATCH"
        message = (
            f"NPI Load complete — v{version}\n"
            f"Records: {total_loaded:,}\n"
            f"Reconciliation: {status_line}\n"
            f"Failed workers: {len(failed_workers)}\n"
            f"Report (24hr link): {report_url}"
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
            logging.warning(
                "Pushover credentials not configured. Report URL: %s", report_url
            )

        return {"report_url": report_url, "total_loaded": total_loaded}
