# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Provider Pipeline discrepancy_report.pdf builder.

Realizes ProviderPipeline_LowLevelDesign_v54 §7.5.

Two artifacts per run:
  1. Email whose HTML body reproduces the PDF header grid plus the per-class
     discrepancy summary (finding class / severity / count).
  2. Attached PDF: title + framed header grid + summary table (one row per
     finding class) + a trailing Appendix listing, per finding class, up to
     report_cap_per_class sample record keys with a '+N more' note.

Datetime format: M/D/YYYY H:MM AM/PM PST (operator preference).
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

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


# A value carrying this prefix is rendered bold in both the PDF and the
# email. The run's outcome is the one thing a reader should not have to hunt
# for, so it is the only field that uses it.
_BOLD = "⁣BOLD⁣"


def _strip_bold(value: str) -> tuple[str, bool]:
    """Return the value without its marker, and whether it was marked."""
    text = str(value)
    return (text[len(_BOLD):], True) if text.startswith(_BOLD) else (text, False)


def _fmt_header_fields(manifest: dict) -> list[tuple[str, str]]:
    """Header field-value pairs, driven entirely by the manifest counts."""
    records_non_certain = manifest.get("records_with_non_certain_information", "Unknown")
    records_with_warnings = manifest.get("records_with_non_fatal_warnings", "Unknown")
    records_with_errors = manifest.get("records_with_non_fatal_errors", "Unknown")
    successful = manifest.get("records_successfully_collected", "Unknown")
    fatal_reason = manifest.get("fatal_reason") or ""
    fatal_present = bool(fatal_reason)
    rows_in_target = manifest.get("rows_in_target", "Unknown")
    total_rows = manifest.get("total_rows", "Unknown")
    target_collection = manifest.get("target_collection") or "target collection"
    return [
        ("Pipeline",                              str(manifest.get("pipeline_name", "provider"))),
        ("Run status",                            _BOLD + str(manifest.get("run_status", "Unknown")).upper()),
        ("Run started",                           _fmt_local_time(manifest.get("run_started_utc"))),
        ("Run ended",                             _fmt_local_time(manifest.get("run_ended_utc"))),
        ("Records successfully collected",        str(successful)),
        ("Records with non certain Information",  str(records_non_certain)),
        ("Records with non-fatal warnings",       str(records_with_warnings)),
        ("Records with non-fatal errors",         str(records_with_errors)),
        ("Fatal error",                           fatal_reason if fatal_present else "None"),
        (f"Rows in {target_collection}",          str(rows_in_target)),
        ("Total rows",                            str(total_rows)),
    ]


def _summary_rows(summary: list[dict]) -> list[tuple[str, str, str]]:
    """Body rows: (finding class, severity, count) per finding class."""
    return [
        (str(s.get("class", "?")),
         str(s.get("severity", "")).upper(),
         str(s.get("count", 0)))
        for s in (summary or [])
    ]


def render_header_as_html(manifest: dict, summary: list[dict]) -> str:
    """Email HTML: the header grid plus the body summary (class / severity /
    count). The per-class key lists live only in the attached PDF's Appendix.
    """
    fields = _fmt_header_fields(manifest)
    per_col = (len(fields) + 1) // 2
    col_left, col_right = fields[:per_col], fields[per_col:]
    rows_html = []
    for i in range(per_col):
        ll, lv = col_left[i]
        if i < len(col_right):
            rl, rv = col_right[i]
        else:
            rl = rv = ""
        lv, lb = _strip_bold(lv)
        rv, rb = _strip_bold(rv)
        lv = f"<strong>{lv}</strong>" if lb else lv
        rv = f"<strong>{rv}</strong>" if rb else rv
        rows_html.append(
            f"<tr>"
            f"<td style='border:1px solid #000;padding:6px 10px;background:#f4f4f4;font-weight:bold'>{ll}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px'>{lv}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px;background:#f4f4f4;font-weight:bold'>{rl}</td>"
            f"<td style='border:1px solid #000;padding:6px 10px'>{rv}</td>"
            f"</tr>"
        )
    summary_rows = _summary_rows(summary)
    if summary_rows:
        summary_html = [
            "<tr>"
            "<th style='border:1px solid #000;padding:6px 10px;background:#e0e0e0'>Finding class</th>"
            "<th style='border:1px solid #000;padding:6px 10px;background:#e0e0e0'>Severity</th>"
            "<th style='border:1px solid #000;padding:6px 10px;background:#e0e0e0'>Count</th>"
            "</tr>"
        ]
        for cls, sev, count in summary_rows:
            summary_html.append(
                f"<tr>"
                f"<td style='border:1px solid #000;padding:6px 10px'>{cls}</td>"
                f"<td style='border:1px solid #000;padding:6px 10px'>{sev}</td>"
                f"<td style='border:1px solid #000;padding:6px 10px'>{count}</td>"
                f"</tr>"
            )
        summary_block = (
            "<h3 style='text-align:center;margin-top:20px'>Discrepancy summary</h3>"
            "<table style='border-collapse:collapse;border:2px solid #000;margin:0 auto;'>"
            f"{''.join(summary_html)}"
            "</table>"
        )
    else:
        summary_block = (
            "<p style='text-align:center;margin-top:20px;color:#555'>"
            "No discrepancies this run.</p>"
        )
    return (
        "<html><body style='font-family:Helvetica,Arial,sans-serif'>"
        f"<h2 style='text-align:center;margin-bottom:16px'>{REPORT_TITLE}</h2>"
        "<table style='border-collapse:collapse;border:2px solid #000;margin:0 auto;'>"
        f"{''.join(rows_html)}"
        "</table>"
        f"{summary_block}"
        "<p style='text-align:center;margin-top:16px;color:#555;font-size:12px'>"
        "See attached discrepancy_report.pdf for the per-class key Appendix."
        "</p>"
        "</body></html>"
    )


def render_header_as_text(manifest: dict, summary: list[dict]) -> str:
    """Plaintext fallback of the header + body summary."""
    fields = _fmt_header_fields(manifest)
    lines = [REPORT_TITLE, "=" * len(REPORT_TITLE), ""]
    for label, value in fields:
        text, _ = _strip_bold(value)
        lines.append(f"  {label:<40} {text}")
    lines.append("")
    lines.append("Discrepancy summary (class / severity / count):")
    rows = _summary_rows(summary)
    if not rows:
        lines.append("  (no discrepancies this run)")
    for cls, sev, count in rows:
        lines.append(f"  {cls:<40} {sev:<8} {count}")
    lines.append("")
    return "\n".join(lines)


def build_discrepancy_pdf(
    manifest: dict,
    summary: list[dict],
    appendix: list[dict] | None = None,
) -> bytes:
    """Build discrepancy_report.pdf (LLD v54 §7.5). Body = the per-class
    summary (class / severity / count). A trailing Appendix carries, per
    finding class, up to report_cap_per_class sample keys with a '+N more'
    note. Raises ImportError if reportlab is missing (fail loud)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    appendix = appendix or []
    fields = _fmt_header_fields(manifest)

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
        lv_text, lv_bold = _strip_bold(lv)
        rv_text, rv_bold = _strip_bold(rv)
        wrapped_rows.append([
            Paragraph(str(ll), label_style),
            Paragraph(lv_text, label_style if lv_bold else value_style),
            Paragraph(str(rl), label_style),
            Paragraph(rv_text, label_style if rv_bold else value_style),
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

    # Body: the per-class summary -- finding class, severity, count.
    body_header_style = ParagraphStyle(
        "BodyHead", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica-Bold", fontSize=8, leading=10,
    )
    body_cell_style = ParagraphStyle(
        "BodyCell", parent=getSampleStyleSheet()["Normal"],
        fontName="Helvetica", fontSize=8, leading=10,
    )

    def _p(text: str, style: ParagraphStyle) -> Paragraph:
        return Paragraph(str(text).replace("<", "&lt;").replace(">", "&gt;"), style)

    summary_rows = _summary_rows(summary)
    body_rows = [[
        _p("FINDING CLASS", body_header_style),
        _p("SEVERITY", body_header_style),
        _p("COUNT", body_header_style),
    ]]
    if not summary_rows:
        body_rows.append([
            _p("(no discrepancies this run)", body_cell_style),
            _p("-", body_cell_style),
            _p("0", body_cell_style),
        ])
    else:
        for cls, sev, count in summary_rows:
            body_rows.append([
                _p(cls, body_cell_style),
                _p(sev, body_cell_style),
                _p(count, body_cell_style),
            ])
    body_table = Table(
        body_rows,
        colWidths=[3.8 * inch, 1.2 * inch, 2.6 * inch],
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
             colors.HexColor("#ffe6e6") if sev == "ERROR"
             else colors.HexColor("#ffd6d6") if sev == "FATAL"
             else colors.HexColor("#fff5e0") if sev == "WARNING"
             else colors.white)
            for i, (_cls, sev, _count) in enumerate(summary_rows)
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
    appendix_key_style = ParagraphStyle(
        "AppendixKeys", parent=styles["Normal"],
        fontName="Helvetica", fontSize=8, leading=11,
    )

    story = [
        Paragraph(REPORT_TITLE, title_style),
        header_table,
        Paragraph("Discrepancy summary (one row per finding class)", body_heading_style),
        body_table,
    ]

    # Appendix: the collection of business-record keys, per finding class,
    # capped at report_cap_per_class with a '+N more' note.
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph("Appendix — record keys by finding class", body_heading_style))
    if not appendix:
        story.append(Paragraph("(no discrepancies this run)", appendix_key_style))
    for entry in appendix:
        cls = str(entry.get("class", "?"))
        sev = str(entry.get("severity", "")).upper()
        total = entry.get("total", 0)
        keys = entry.get("keys") or []
        overflow = entry.get("overflow", 0)
        story.append(Paragraph(
            f"{cls} ({sev}) — {total} occurrence(s)", body_heading_style))
        listed = ", ".join(str(k) for k in keys) or "(none)"
        if overflow:
            listed += f"  … +{overflow} more"
        story.append(Paragraph(listed, appendix_key_style))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.6 * inch, bottomMargin=0.5 * inch,
        title="ChatHealthy Provider Pipeline — Discrepancy Report",
    )
    doc.build(story)
    return buf.getvalue()
