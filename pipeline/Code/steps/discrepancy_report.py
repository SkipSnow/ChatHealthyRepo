# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Discrepancy reporting for pipeline operations.

Discrepancies (warnings/errors) are stored on the FRONTEND cluster in the
`chathealthypipelines` database under the `pipeline.discrepancies` collection.
Workers write discrepancies via DiscrepancyReport.write(); fatal errors are
emitted via DiscrepancyReport.emit() and fatal_error().
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from enum import Enum

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


class FatalErrorReason(str, Enum):
    """Fatal error classifications."""
    MONGO_UNREACHABLE = "mongo_unreachable"
    VAULT_UNREACHABLE = "vault_unreachable"


class DiscrepancyDetail(str, Enum):
    """Discrepancy severity level.

    ERROR (row blocking):
      - Row cannot be loaded/parsed (parse failure, required field missing)
      - County not found after all enrichment step filter functions have run

    WARNING (non-blocking):
      - Enrichment data inconsistent or missing but row is still usable
      - Optional field populated with default
      - Data quality flag raised but row can proceed
      - Any issue that does not prevent the row from being published

    FATAL:
      - Job-terminating failure (MongoDB unreachable, vault unreachable, etc.)
    """
    WARNING = "warning"
    ERROR = "error"
    FATAL = "fatal"


class DiscrepancyReport:
    """Emits discrepancy reports. Manages its own mongo connection."""

    def __init__(
        self,
        *,
        run_id: str,
        env: str,
        pipeline_name: str,
        source: str,
    ) -> None:
        self.run_id = run_id
        self.env = env
        self.pipeline_name = pipeline_name
        self.source = source
        self.start_time = datetime.now(timezone.utc).isoformat()
        self.operator_email = self._get_operator_email_from_vault()
        self.mongo_connection = self._establish_mongo_connection()
        self.config = self._load_pipeline_config()

    def _establish_mongo_connection(self):
        """Establish MongoDB connection using pipelineEditor identity."""
        try:
            utilities = ChatHealthyMongoUtilities()
            return utilities.getConnection("pipelineEditor")
        except ChatHealthyException:
            raise
        except Exception as exc:
            raise ChatHealthyException(
                mode="mongo_unreachable",
                message=f"Failed to establish MongoDB connection: {exc}",
                component="DiscrepancyReport",
            ) from exc

    def _load_pipeline_config(self) -> dict:
        """Load pipeline configuration from MongoDB."""
        try:
            if not self.mongo_connection:
                return {}
            from pipeline_db import get_db
            db = get_db(self.env)
            config = db["PipelineConfig"].find_one({"_id": self.pipeline_name})
            return config or {}
        except Exception:
            return {}

    def _get_operator_email_from_vault(self) -> str | None:
        """Fetch operator email from Key Vault."""
        try:
            vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
            if not vault_uri:
                return None
            credential = DefaultAzureCredential()
            client = SecretClient(vault_uri=vault_uri, credential=credential)
            secret = client.get_secret("notification-to-email")
            return (secret.value or "").strip() if secret else None
        except Exception:
            return None

    def _build_manifest_doc(self, discrepancy_level: DiscrepancyDetail, explanation: str) -> dict:
        """Build manifest_doc from discrepancy details."""
        status = "fatal" if discrepancy_level == DiscrepancyDetail.FATAL else "failed" if discrepancy_level == DiscrepancyDetail.ERROR else "completed"
        doc = {
            "run_id": self.run_id,
            "pipeline_name": self.pipeline_name,
            "status": status,
        }
        if discrepancy_level == DiscrepancyDetail.FATAL:
            doc["abort_reason"] = explanation
            doc["fatal_reason"] = explanation
        return doc

    def _build_discrepancy_dict(
        self,
        discrepancy_level: str | DiscrepancyDetail,
        explanation: str,
        source_line: str | None = None,
        npi: str | None = None,
        field: str | None = None,
    ) -> dict:
        """Build discrepancy document for mongo."""
        if isinstance(discrepancy_level, str):
            level_enum = DiscrepancyDetail(discrepancy_level)
        else:
            level_enum = discrepancy_level

        return {
            "run_id": self.run_id,
            "level": level_enum.value,
            "source_line": source_line,
            "npi": npi,
            "field": field,
            "explanation": explanation,
        }

    def write(
        self,
        discrepancy_level: str | DiscrepancyDetail,
        explanation: str,
        source_line: str | None = None,
        npi: str | None = None,
        field: str | None = None,
    ) -> bool:
        """Write a discrepancy. Called by workers.

        Args:
            discrepancy_level: Severity level (warning, error, fatal).
            explanation: Error explanation.
            source_line: Source location.
            npi: Optional NPI.
            field: Optional field name.

        Returns:
            True if written successfully, False otherwise.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return False

            if isinstance(discrepancy_level, str):
                level_enum = DiscrepancyDetail(discrepancy_level)
            else:
                level_enum = discrepancy_level

            disc_dict = self._build_discrepancy_dict(level_enum, explanation, source_line, npi, field)
            discs_coll = self.mongo_connection["chathealthypipelines"]["pipeline.discrepancies"]
            discs_coll.insert_one(disc_dict)
            return True
        except ChatHealthyException as exc:
            log.error("write: could not write discrepancy: %s", exc, exc=exc)
            return False
        except Exception as exc:
            log.error("write: could not write discrepancy: %s", exc)
            return False

    def increment_warning_count(self) -> int:
        """Atomically increment global warning counter for this run_id.
        Returns the new count after increment.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return -1
            counters_coll = self.mongo_connection["chathealthypipelines"]["pipeline.run_counters"]
            result = counters_coll.find_one_and_update(
                {"run_id": self.run_id},
                {"$inc": {"warning_count": 1}},
                upsert=True,
                return_document=True,
            )
            return result.get("warning_count", 0)
        except ChatHealthyException as exc:
            log.error("increment_warning_count failed: %s", exc, exc=exc)
            return -1
        except Exception as exc:
            log.error("increment_warning_count failed: %s", exc)
            return -1

    def increment_error_count(self) -> int:
        """Atomically increment global error counter for this run_id.
        Returns the new count after increment.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return -1
            counters_coll = self.mongo_connection["chathealthypipelines"]["pipeline.run_counters"]
            result = counters_coll.find_one_and_update(
                {"run_id": self.run_id},
                {"$inc": {"error_count": 1}},
                upsert=True,
                return_document=True,
            )
            return result.get("error_count", 0)
        except ChatHealthyException as exc:
            log.error("increment_error_count failed: %s", exc, exc=exc)
            return -1
        except Exception as exc:
            log.error("increment_error_count failed: %s", exc)
            return -1

    def get_counts(self) -> dict:
        """Get current warning and error counts for this run_id.
        Returns: {"warning_count": N, "error_count": M}
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return {"warning_count": 0, "error_count": 0}
            counters_coll = self.mongo_connection["chathealthypipelines"]["pipeline.run_counters"]
            result = counters_coll.find_one({"run_id": self.run_id})
            if not result:
                return {"warning_count": 0, "error_count": 0}
            return {
                "warning_count": result.get("warning_count", 0),
                "error_count": result.get("error_count", 0),
            }
        except ChatHealthyException as exc:
            log.error("get_counts failed: %s", exc, exc=exc)
            return {"warning_count": 0, "error_count": 0}
        except Exception as exc:
            log.error("get_counts failed: %s", exc)
            return {"warning_count": 0, "error_count": 0}

    def write_email(self) -> bool:
        """Read all discrepancies from mongo, send email with fatal error in body and others in PDF.

        Returns:
            True if email sent successfully, False otherwise.
        """
        log = ChatHealthyLoggingService()
        try:
            if not self.mongo_connection:
                return False

            reports_coll = self.mongo_connection["chathealthypipelines"]["pipeline.discrepancy_reports"]
            all_reports = list(reports_coll.find({"run_id": self.run_id}))

            if not all_reports:
                return False

            # Separate by level: fatal, error, warning
            fatal_reports = []
            error_reports = []
            warning_reports = []
            for report in all_reports:
                level = report.get("level", "").lower()
                if level == "fatal":
                    fatal_reports.append(report)
                elif level == "error":
                    error_reports.append(report)
                elif level == "warning":
                    warning_reports.append(report)

            if not fatal_reports:
                return False

            # Send email with fatal error in body
            from notification_client import NotificationClient
            client = NotificationClient()
            subject = f"Provider pipeline {self.run_id} - fatal"
            body_lines = []
            body_lines.append(f"FATAL: {fatal_reports[0].get('explanation')}")
            body_lines.append(f"run_id={self.run_id}")
            body_lines.append(f"source={fatal_reports[0].get('source')}")
            body = "\n".join(body_lines)

            # Build PDF with all issues in order: fatal, error, warning
            attachments = []
            if fatal_reports or error_reports or warning_reports:
                from discrepancy_pdf import build_discrepancy_pdf
                pdf_reports = []
                if fatal_reports:
                    pdf_reports.append({"level": "fatal", "items": fatal_reports})
                if error_reports:
                    pdf_reports.append({"level": "error", "items": error_reports})
                if warning_reports:
                    pdf_reports.append({"level": "warning", "items": warning_reports})

                pdf_content = build_discrepancy_pdf(
                    {"run_id": self.run_id, "total_issues": len(all_reports)},
                    pdf_reports
                )
                attachments.append({"filename": "discrepancies.pdf", "content": pdf_content})

            # Send email
            receivers = []
            if self.operator_email and self.operator_email not in receivers:
                receivers.append(self.operator_email)
            kv_operator = (os.environ.get("NOTIFICATION_TO_EMAIL") or "").strip()
            if kv_operator and kv_operator not in receivers:
                receivers.append(kv_operator)
            for addr in receivers:
                if addr and "@" in addr:
                    client.send_email(addr, subject, body, attachments=attachments)

            return True
        except ChatHealthyException as exc:
            log.error("write_email: could not send report: %s", exc, exc=exc)
            return False
        except Exception as exc:
            log.error("write_email: could not send report: %s", exc)
            return False


def fatal_error(
    report: DiscrepancyReport,
    level: str | DiscrepancyDetail,
    explanation: str,
    source_line: str | None = None,
    records_processed: int = 0,
    rows_before_fatal: int = 0,
) -> bool:
    """Handle fatal error: write to mongo and send email report.

    Args:
        report: DiscrepancyReport with mongo connection.
        level: Fatal error severity level.
        explanation: Fatal error explanation.
        source_line: Fatal error source location.
        records_processed: Number of records processed before fatal error.
        rows_before_fatal: Which row we were on when fatal occurred.

    Returns:
        True if report sent successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        if isinstance(level, str):
            level_enum = DiscrepancyDetail(level)
        else:
            level_enum = level

        # Query for actual warning/error counts from discrepancies collection
        discs_coll_name = report.config.get("warnings_errors_collection", "pipeline.discrepancies")
        discs_coll = report.mongo_connection["chathealthypipelines"][discs_coll_name]

        warning_count = discs_coll.count_documents({"run_id": report.run_id, "level": "warning"})
        error_count = discs_coll.count_documents({"run_id": report.run_id, "level": "error"})

        # Build manifest for PDF generation
        end_time = datetime.now(timezone.utc).isoformat()
        manifest = {
            "run_id": report.run_id,
            "pipeline_name": report.pipeline_name,
            "run_started_utc": report.start_time,
            "run_ended_utc": end_time,
            "fatal_reason": explanation,
            "records_100_percent_successfully_collected": False,
            "records_with_non_fatal_warnings": warning_count,
            "records_with_non_fatal_errors": error_count,
            "rows_before_fatal": rows_before_fatal if rows_before_fatal > 0 else "n/a",
            "rows_not_looked_at": "Unknown",
            "warning_threshold": report.config.get("warning_threshold", "Unknown"),
            "error_threshold": report.config.get("error_threshold", "Unknown"),
        }

        # Write fatal error document
        reports_coll = report.mongo_connection["chathealthypipelines"]["pipeline.discrepancy_reports"]
        fatal_doc = {
            "run_id": report.run_id,
            "job_name": report.pipeline_name,
            "source": source_line or report.source,
            "level": level_enum.value,
            "explanation": explanation,
            "manifest": manifest,
        }
        reports_coll.insert_one(fatal_doc)
        return report.write_email()
    except ChatHealthyException as exc:
        log.error("fatal_error: could not write fatal error: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("fatal_error: could not write fatal error: %s", exc)
        return False


def createWarning(
    report: DiscrepancyReport,
    job_name: str,
    source: str,
    details: str,
) -> bool:
    """Record a warning using the report's mongo connection.

    Args:
        report: DiscrepancyReport with mongo connection.
        job_name: Name of the job/pipeline.
        source: Source location of the warning.
        details: Warning details.

    Returns:
        True if recorded successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        reports_coll = report.mongo_connection["chathealthypipelines"]["pipeline.discrepancy_reports"]
        warning_doc = {
            "run_id": report.run_id,
            "job_name": job_name,
            "source": source,
            "level": "warning",
            "details": details,
        }
        reports_coll.insert_one(warning_doc)
        return True
    except ChatHealthyException as exc:
        log.error("createWarning: could not write warning: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("createWarning: could not write warning: %s", exc)
        return False


def CreateNonFatalError(
    report: DiscrepancyReport,
    job_name: str,
    source: str,
    details: str,
) -> bool:
    """Record a non-fatal error using the report's mongo connection.

    Args:
        report: DiscrepancyReport with mongo connection.
        job_name: Name of the job/pipeline.
        source: Source location of the error.
        details: Error details.

    Returns:
        True if recorded successfully, False otherwise.
    """
    log = ChatHealthyLoggingService()
    try:
        if not report.mongo_connection:
            return False

        reports_coll = report.mongo_connection["chathealthypipelines"]["pipeline.discrepancy_reports"]
        error_doc = {
            "run_id": report.run_id,
            "job_name": job_name,
            "source": source,
            "level": "error",
            "details": details,
        }
        reports_coll.insert_one(error_doc)
        return True
    except ChatHealthyException as exc:
        log.error("CreateNonFatalError: could not write error: %s", exc, exc=exc)
        return False
    except Exception as exc:
        log.error("CreateNonFatalError: could not write error: %s", exc)
        return False
