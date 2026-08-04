# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline: emit_discrepancy_report + DAG wrapper.

Per operator directive 2026-08-03, all pipeline coordination metadata
(including pipeline.discrepancies) lives on the PIPELINE cluster in the
`chathealthypipelines` database. This module reads discrepancies from the
pipeline_mongo connection only.
"""

from __future__ import annotations

import os

from discrepancy_pdf import build_discrepancy_pdf
from notification_client import NotificationClient
from pipeline_runtime import PipelineRuntime

_DISCREPANCIES_DB = "chathealthypipelines"
_DISCREPANCIES_COLL = "pipeline.discrepancies"


def emit_discrepancy_report(
    *,
    pipeline_mongo,
    run_id: str,
    manifest_status: str,
    manifest_doc: dict,
    config: dict,
    operator_email: str | None = None,
    operator_sms: str | None = None,
    notification_client=None,
) -> dict:
    """Build the discrepancy PDF from pipeline.discrepancies on the pipeline
    cluster and email it. Callable in every terminal state (perfect run,
    failed run, aborted run) per operator directive 2026-08-03 "we always
    in every case, even with a perfect job or an abend get the discrepancy
    report".

    When manifest_doc.get('abort_reason') is truthy the email body is
    prepended with `FATAL: {abort_reason}` so the operator sees the abend
    reason at the top of the notification. When it is falsy, no FATAL line
    appears.
    """
    discrepancies_coll = pipeline_mongo[_DISCREPANCIES_DB][_DISCREPANCIES_COLL]
    discs = list(discrepancies_coll.find({"run_id": run_id}))
    pdf = build_discrepancy_pdf(manifest_doc, discs)
    summary = {"total": len(discs), "pdf_bytes": len(pdf)}

    client = notification_client or NotificationClient()
    subject = f"Provider pipeline {run_id} - {len(discs)} discrepancies"
    body_lines = []
    abort_reason = manifest_doc.get("abort_reason") if isinstance(manifest_doc, dict) else None
    if abort_reason:
        body_lines.append(f"FATAL: {abort_reason}")
    body_lines.append(f"run_id={run_id}")
    body_lines.append(f"status={manifest_status}")
    body_lines.append(f"discrepancies={len(discs)}")
    body = "\n".join(body_lines)

    receivers = list(config.get("notification_receivers") or [])
    if operator_email:
        receivers.append(operator_email)
    # Operator recipient is a KV-hydrated secret (NOTIFICATION-TO-EMAIL ->
    # NOTIFICATION_TO_EMAIL env var per bootstrap._load_all_secrets_into_env).
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
    """DAG step wrapper. Delegates to emit_discrepancy_report so the happy
    path DAG dispatch and the Controller finally abend path share one
    implementation. Uses rt.mongo (the pipeline cluster) as the discrepancy
    source of truth."""
    rt = PipelineRuntime(ctx)
    manifest_doc = ctx.manifest.to_document()
    summary = emit_discrepancy_report(
        pipeline_mongo=rt.mongo,
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
