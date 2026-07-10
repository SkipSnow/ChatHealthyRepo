# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

from discrepancy_pdf import build_discrepancy_pdf
from notification_client import NotificationClient
from pipeline_runtime import PipelineRuntime


def execute(ctx):
    rt = PipelineRuntime(ctx)
    discs = list(rt.discrepancies_collection().find({"run_id": ctx.run_id}))
    manifest_doc = ctx.manifest.to_document()
    pdf = build_discrepancy_pdf(manifest_doc, discs)
    summary = {"total": len(discs), "pdf_bytes": len(pdf)}

    client = ctx.notification_client or NotificationClient()
    subject = f"Provider pipeline {ctx.run_id} — {len(discs)} discrepancies"
    body = f"run_id={ctx.run_id}\nstatus={ctx.manifest.status}\ndiscrepancies={len(discs)}"
    receivers = list(ctx.config.get("notification_receivers") or [])
    if getattr(ctx.args, "operator_email", None):
        receivers.append(ctx.args.operator_email)
    for addr in receivers:
        if addr and "@" in addr:
            client.send_email(addr, subject, body, attachments=[{"filename": "discrepancies.pdf", "content": pdf}])
    sms = getattr(ctx.args, "operator_sms", None)
    if sms:
        client.send_sms(sms, f"{ctx.run_id}: {len(discs)} discrepancies")

    ctx.manifest.discrepancy_summary = summary
    return summary
