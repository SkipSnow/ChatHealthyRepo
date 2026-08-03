# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import os

from discrepancy_pdf import build_discrepancy_pdf
from notification_client import NotificationClient
from pipeline_runtime import PipelineRuntime


def emit_discrepancy_report(
    *,
    frontend_mongo,
    run_id: str,
    manifest_status: str,
    manifest_doc: dict,
    config: dict,
    operator_email: str | None = None,
    operator_sms: str | None = None,
    notification_client=None,
) -> dict:
    """Build the discrepancy PDF from pipeline.discrepancies and email/SMS
    it. Callable in every terminal state — perfect run, failed run, aborted
    run — per operator directive 2026-08-03 "we always in every case, even
    with a perfect job or an abend get the discrepancy report".

    The Controller's finally block calls this directly (Level 1). The
    DAG-node `execute()` below delegates here so the same code path runs
    whether the report is emitted by the DAG on the happy path or by
    Controller's finally on the abend path — no divergence."""
    discrepancies_coll = frontend_mongo["chathealthyfrontend"]["pipeline.discrepancies"]
    discs = list(discrepancies_coll.find({"run_id": run_id}))
    pdf = build_discrepancy_pdf(manifest_doc, discs)
    summary = {"total": len(discs), "pdf_bytes": len(pdf)}

    client = notification_client or NotificationClient()
    subject = f"Provider pipeline {run_id} — {len(discs)} discrepancies"
    body = f"run_id={run_id}\nstatus={manifest_status}\ndiscrepancies={len(discs)}"
    receivers = list(config.get("notification_receivers") or [])
    if operator_email:
        receivers.append(operator_email)
    # Operator recipient is a KV-hydrated secret (NOTIFICATION-TO-EMAIL ->
    # NOTIFICATION_TO_EMAIL env var per bootstrap._load_all_secrets_into_env).
    # Kept out of the repo so the operator's address is not source-visible.
    # Absence here surfaces as "no report" -- surfaced 2026-08-02 when fire
    # #3 built the PDF, found zero recipients, discarded the bytes.
    kv_operator = (os.environ.get("NOTIFICATION_TO_EMAIL") or "").strip()
    if kv_operator and kv_operator not in receivers:
        receivers.append(kv_operator)
    for addr in receivers:
        if addr and "@" in addr:
            client.send_email(
                addr, subject, body,
                attachments=[{"filename": "discrepancies.pdf", "content": pdf}],
            )
    if operator_sms:
        client.send_sms(operator_sms, f"{run_id}: {len(discs)} discrepancies")
    return summary


def execute(ctx):
    """DAG step wrapper. Delegates to emit_discrepancy_report so the happy-
    path DAG dispatch and the Controller-finally abend path share one
    implementation."""
    rt = PipelineRuntime(ctx)
    manifest_doc = ctx.manifest.to_document()
    summary = emit_discrepancy_report(
        frontend_mongo=rt.frontend,
        run_id=ctx.run_id,
        manifest_status=ctx.manifest.status,
        manifest_doc=manifest_doc,
        config=ctx.config,
        operator_email=getattr(ctx.args, "operator_email", None),
        operator_sms=getattr(ctx.args, "operator_sms", None),
        notification_client=ctx.notification_client,
    )
    ctx.manifest.discrepancy_summary = summary
    return summary
