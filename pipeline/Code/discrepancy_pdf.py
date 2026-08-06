# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Provider Pipeline discrepancy_report.pdf builder.

Realizes ProviderPipeline_LowLevelDesign_v41 §8.4 (rewritten 2026-08-01)
+ EPIC-010-F-102-S-005-REQ-T-001.

Two artifacts per run:
  1. Email whose HTML body reproduces the PDF header (framed 2-col grid)
  2. Attached PDF: title + framed field-value header grid + body table
     with one wrapping row per warning/error (Type / Source line / NPI /
     Field / Explanation).

Header field set (11, spec-verbatim):
  Pipeline, Run started, Run ended,
  Records 100% successfully collected,
  Records with non-fatal warnings, Records with non-fatal errors,
  Warning threshold, Error threshold,
  Fatal error, Rows before fatal, Rows not looked at.

Datetime format: M/D/YYYY H:MM AM/PM PST (operator preference).
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

import io
from datetime import datetime, timezone, timedelta

_log = ChatHealthyLoggingService()

REPORT_TITLE = "ChatHealthy.ai Data pipeline discrepancy report: Provider pipeline"

# Operator-preferred display timezone.
_PST_OFFSET = timedelta(hours=-8)


def _fmt_local_time(iso_or_dt) -> str:
    """Render an ISO timestamp (or datetime) as 'M/D/YYYY H:MM AM/PM PST'."""
    if not iso_or_dt:
        return ""
    try:
        if isinstance(iso_or_dt, datetime):
            dt = iso_or_dt
        else:
            s = str(iso_or_dt).replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        pst = dt.astimezone(timezone(_PST_OFFSET))
        hour_12 = pst.hour % 12 or 12
        ampm = "AM" if pst.hour < 12 else "PM"
        return f"{pst.month}/{pst.day}/{pst.year} {hour_12}:{pst.minute:02d} {ampm} PST"
    except (ValueError, TypeError):
        return str(iso_or_dt)


def _fmt_header_fields(manifest: dict, discrepancies: list[dict]) -> list[tuple[str, str]]:
    """11 header field-value pairs, spec-verbatim labels."""
    errors = [d for d in discrepancies if (d.get("level") or "").lower() == "error"]
    warnings = [d for d in discrepancies if (d.get("level") or "").lower() == "warning"]
    # The manifest is authoritative: it counts the documents actually stored
    # for this run. Recomputing from the rendered rows undercounts whenever a
    # discrepancy carries no npi.
    records_with_warnings = manifest.get("records_with_non_fatal_warnings")
    if records_with_warnings is None:
        records_with_warnings = len(warnings)
    records_with_errors = manifest.get("records_with_non_fatal_errors")
    if records_with_errors is None:
        records_with_errors = len(errors)
    total_source_rows = manifest.get("total_source_rows")
    if total_source_rows is None:
        successful = "Unknown"
    else:
        touched = {d.get("npi") for d in discrepancies if d.get("npi")}
        successful = str(int(total_source_rows) - len(touched))
    fatal_reason = manifest.get("fatal_reason") or ""
    fatal_present = bool(fatal_reason)
    rows_in_target = manifest.get("rows_in_target", "Unknown") if fatal_present else "n/a"
    total_rows = manifest.get("total_rows", "Unknown") if fatal_present else "n/a"
    target_collection = manifest.get("target_collection") or "target collection"
    return [
        ("Pipeline",                            str(manifest.get("pipeline_name", "provider"))),
        ("Run started",                         _fmt_local_time(manifest.get("run_started_utc"))),
        ("Run ended",                           _fmt_local_time(manifest.get("run_ended_utc"))),
        ("Records 100% successfully collected", successful),
        ("Records with non-fatal warnings",     str(records_with_warnings)),
        ("Records with non-fatal errors",       str(records_with_errors)),
        ("Warning threshold",                   str(manifest.get("warning_threshold", "Unknown"))),
        ("Error threshold",                     str(manifest.get("error_threshold", "Unknown"))),
        ("Fatal error",                         fatal_reason if fatal_present else "None"),
        (f"Rows in {target_collection}",        str(rows_in_target)),
        ("Total rows",                          str(total_rows)),
    ]


def render_header_as_html(manifest: dict, discrepancies: list[dict]) -> str:
    """Pixel-perfect HTML reproduction of the PDF header table (LLD v41
    §8.4: 'each email shall essentially be a pixel perfect reproduction
    of the header cited below'). Same source as the PDF's header block."""
    fields = _fmt_header_fields(manifest, discrepancies)
    per_col = (len(fields) + 1) // 2
    col_left, col_right = fields[:per_col], fields[per_col:]
    rows_html = []
    for i in range(per_col):
        ll, lv = col_left[i]
        if i < len(col_right):
            rl, rv = col_right[i]
        else:
            rl = rv = ""
        rows_html.append(
            f"<tr>"
            f"<td style='border:1px solid #000;padding:6px 10px;background:#f4f4f4;font-weight:bold'>{ll}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px'>{lv}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px;background:#f4f4f4;font-weight:bold'>{rl}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px'>{rv}</td>"
            f"</tr>"
        )
    return (
        "<html><body style='font-family:Helvetica,Arial,sans-serif'>"
        f"<h2 style='text-align:center;margin-bottom:16px'>{REPORT_TITLE}</h2>"
        "<table style='border-collapse:collapse;border:2px solid #000;margin:0 auto;'>"
        f"{''.join(rows_html)}"
        "</table>"
        "<p style='text-align:center;margin-top:16px;color:#555;font-size:12px'>"
        "See attached discrepancy_report.pdf for the detailed body."
        "</p>"
        "</body></html>"
    )


def render_header_as_text(manifest: dict, discrepancies: list[dict]) -> str:
    """Plaintext fallback of the header (SMS / non-HTML mail clients)."""
    fields = _fmt_header_fields(manifest, discrepancies)
    lines = [REPORT_TITLE, "=" * len(REPORT_TITLE), ""]
    for label, value in fields:
        lines.append(f"  {label:<40} {value}")
    lines.append("")
    return "\n".join(lines)


def build_discrepancy_pdf(manifest: dict, discrepancies: list[dict]) -> bytes:
    """Build discrepancy_report.pdf per LLD v41 §8.4. Raises ImportError
    if reportlab is missing (fail loud rather than ship a corrupt
    placeholder that would silently reach the operator)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Table, TableStyle,
    )

    fields = _fmt_header_fields(manifest, discrepancies)

    label_style = ParagraphStyle(
        "HeaderLabel", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica-Bold", fontSize=8, leading=10,
    )
    value_style = ParagraphStyle(
        "HeaderValue", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica", fontSize=8, leading=10,
    )

    # 2-column: rows of [label, value, label, value]
    per_col = (len(fields) + 1) // 2
    left, right = fields[:per_col], fields[per_col:]
    while len(right) < len(left):
        right.append(("", ""))
    wrapped_rows = []
    for (ll, lv), (rl, rv) in zip(left, right):
        wrapped_rows.append([
            Paragraph(str(ll), label_style),
            Paragraph(str(lv), value_style),
            Paragraph(str(rl), label_style),
            Paragraph(str(rv), value_style),
        ])
    header_table = Table(
        wrapped_rows,
        colWidths=[2.2 * inch, 1.6 * inch, 2.2 * inch, 1.6 * inch],
    )
    header_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f0f0")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f0f0")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    # Body: one wrapping row per warning/error, distinct cells (LLD v41
    # spec 2026-08-01: 'distinct cells that can wrap').
    body_header_style = ParagraphStyle(
        "BodyHead", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica-Bold", fontSize=7, leading=9,
    )
    body_cell_style = ParagraphStyle(
        "BodyCell", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica", fontSize=7, leading=9,
    )
    def _p(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(str(text).replace("<", "&lt;").replace(">", "&gt;"), style)
    body_rows = [[
        _p("TYPE", body_header_style),
        _p("SOURCE LINE", body_header_style),
        _p("NPI", body_header_style),
        _p("FIELD LIST", body_header_style),
        _p("EXPLANATION", body_header_style),
    ]]
    if not discrepancies:
        body_rows.append([
            _p("", body_cell_style), _p("", body_cell_style),
            _p("", body_cell_style),
            _p("(no discrepancies this run)", body_cell_style),
            _p("", body_cell_style),
        ])
    else:
        for d in discrepancies:
            # A fatal is a job-level event: it has no source line, no NPI and
            # no field list, and those cells say so rather than reading as
            # missing data.
            blank = "N/A" if (d.get("level") or "").lower() == "fatal" else "-"
            field_list = d.get("field_list")
            if isinstance(field_list, (list, tuple)):
                fields = ", ".join(str(f) for f in field_list) or blank
            else:
                fields = str(field_list or d.get("field") or blank)
            body_rows.append([
                _p((d.get("level") or "?").upper(), body_cell_style),
                _p(d.get("source_line") or blank, body_cell_style),
                _p(d.get("npi") or blank, body_cell_style),
                _p(fields, body_cell_style),
                _p(d.get("explanation") or d.get("details") or d.get("message")
                   or d.get("reason") or blank, body_cell_style),
            ])
    body_table = Table(
        body_rows,
        colWidths=[0.6 * inch, 0.8 * inch, 1.0 * inch, 1.8 * inch, 3.4 * inch],
        repeatRows=1,
    )
    body_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, colors.black),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e0e0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        *[
            ("BACKGROUND", (0, i + 1), (-1, i + 1),
             colors.HexColor("#ffe6e6") if (d.get("level") or "").lower() == "error"
             else colors.HexColor("#fff5e0") if (d.get("level") or "").lower() == "warning"
             else colors.white)
            for i, d in enumerate(discrepancies)
        ],
    ]))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"],
        fontSize=14, spaceAfter=14, alignment=1,
        textColor=colors.black,
    )
    body_heading_style = ParagraphStyle(
        "BodyHeading", parent=styles["Heading2"],
        fontSize=10, spaceBefore=10, spaceAfter=4, textColor=colors.black,
    )

    story = [
        Paragraph(REPORT_TITLE, title_style),
        header_table,
        Paragraph("Discrepancies (one row per non-fatal warning or error)", body_heading_style),
        body_table,
    ]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.6 * inch, bottomMargin=0.5 * inch,
        title="ChatHealthy Provider Pipeline — Discrepancy Report",
    )
    doc.build(story)
    return buf.getvalue()
