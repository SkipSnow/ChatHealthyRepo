# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

import io

_log = ChatHealthyLoggingService()


def build_discrepancy_pdf(manifest: dict, discrepancies: list[dict]) -> bytes:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        _log.warning("reportlab not installed — returning minimal PDF bytes")
        return b"%PDF-1.4 minimal discrepancy placeholder\n"

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "ChatHealthy Provider Pipeline Discrepancy Report")
    y -= 24
    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"run_id: {manifest.get('run_id')}")
    y -= 14
    c.drawString(40, y, f"status: {manifest.get('status')} env: {manifest.get('env')}")
    y -= 20
    c.drawString(40, y, f"Total discrepancies: {len(discrepancies)}")
    y -= 20
    for disc in discrepancies[:200]:
        line = f"{disc.get('npi')} — {disc.get('reason')}"
        c.drawString(40, y, line[:100])
        y -= 12
        if y < 40:
            c.showPage()
            y = 750
    c.save()
    return buf.getvalue()
