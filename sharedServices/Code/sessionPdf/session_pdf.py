# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The session, rendered for paper.

The browser's print dialogue was the first attempt and it does not work on a
phone: iOS opens a print preview with no obvious route to a PDF, and inside a
native shell there is no dialogue at all. A file the server produces
downloads identically on desktop, Android, iOS and in an app.

Rendered here rather than in chathealthy_lib: the library's PDF module is a
pipeline capability and the front end is not permitted to load it.
"""
from __future__ import annotations

import io
from typing import Any

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

FILENAME = "chatHealthySessionInfo.pdf"

_IDENTITY_ORDER = ("user_type", "guid", "origin", "server_env",
                   "created_at", "expires_at", "is_registered",
                   "public_username")


def _rows(story, styles, title: str, pairs, table_cls, table_style):
    from reportlab.platypus import Paragraph, Spacer, Table
    story.append(Paragraph(title, styles["heading"]))
    body = [[Paragraph(str(k), styles["key"]), Paragraph(str(v), styles["val"])]
            for k, v in pairs]
    if not body:
        story.append(Paragraph("nothing recorded", styles["note"]))
        story.append(Spacer(1, 10))
        return
    from reportlab.lib.units import inch
    t = Table(body, colWidths=[2.1 * inch, 4.9 * inch], hAlign="LEFT")
    t.setStyle(table_style)
    story.append(t)
    story.append(Spacer(1, 10))


def render(data: dict[str, Any]) -> bytes:
    """The session as a PDF. Raises rather than returning a broken file."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                        Table, TableStyle)
    except ImportError as exc:
        raise ChatHealthyException(
            mode="pdf_library_missing",
            component="session_pdf",
            message=f"reportlab is not installed, so the session cannot be "
                    f"rendered: {exc}",
            exception=exc) from exc

    base = getSampleStyleSheet()
    teal = colors.HexColor("#0b7a75")
    styles = {
        "title": ParagraphStyle("t", parent=base["Title"], textColor=teal,
                                fontSize=16, spaceAfter=2),
        "heading": ParagraphStyle("h", parent=base["Heading2"], textColor=teal,
                                  fontSize=11, spaceBefore=8, spaceAfter=4),
        "key": ParagraphStyle("k", parent=base["BodyText"], fontSize=8,
                              textColor=colors.HexColor("#374151")),
        "val": ParagraphStyle("v", parent=base["BodyText"], fontSize=8),
        "note": ParagraphStyle("n", parent=base["BodyText"], fontSize=8,
                               textColor=colors.HexColor("#9ca3af")),
    }
    grid = TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1fae5")),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ])

    identity = data.get("identity") or {}
    ordered = [(k, identity.get(k, "")) for k in _IDENTITY_ORDER
               if k in identity]
    ordered += [(k, v) for k, v in identity.items()
                if k not in _IDENTITY_ORDER and k not in ("token", "signature")]

    story: list = []
    story.append(Paragraph("ChatHealthy.ai — Session", styles["title"]))
    story.append(Spacer(1, 8))
    _rows(story, styles, "Identity", ordered, Table, grid)
    _rows(story, styles, "Deployment Facts",
          [(r.get("component", ""), r.get("build") or "did not answer")
           for r in (data.get("deployment_facts") or [])], Table, grid)
    _rows(story, styles, "User Parameters",
          _parameter_pairs(data.get("parameters") or {}), Table, grid)

    threads = data.get("threads") or {}
    _rows(story, styles, "Utterances",
          [(u.get("at", ""), f"{u.get('actor','?')}: {u.get('text','')}")
           for u in (threads.get("utterances") or [])], Table, grid)
    _rows(story, styles, "Actions",
          [(a.get("at", ""), str(a.get("tool_name", "")))
           for a in (threads.get("actions") or [])], Table, grid)

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title="ChatHealthy Session",
    ).build(story)
    return buf.getvalue()


def _parameter_pairs(parameters: Any) -> list:
    """One pair per parameter, addressed by the page that holds it.

    Every parameter used to arrive as a single pair keyed 'pages', so the
    whole set rendered into one table cell. A table splits between rows and
    never inside one, so once the pages carried real values that cell grew
    past the height of a page and the document could not be laid out at
    all: 'tallest cell 904.0 points, too large on page 2'. It is also the
    wrong shape to read -- a parameter is addressed by its page and its
    name, and that is how it is shown.
    """
    if not isinstance(parameters, dict):
        return [("parameters", _flat(parameters))]
    pages = parameters.get("pages")
    others = [(k, _flat(v)) for k, v in parameters.items() if k != "pages"]
    if pages is None:
        return others + [("pages", "(not set)")]
    rows = []
    if isinstance(pages, dict):
        items = pages.items()
    elif isinstance(pages, list):
        items = [(str((p or {}).get("page", i)), p) for i, p in enumerate(pages)]
    else:
        return others + [("pages", _flat(pages))]
    for page_name, page_value in items:
        if isinstance(page_value, dict):
            attributes = {k: v for k, v in page_value.items() if k != "page"}
            if not attributes:
                rows.append((str(page_name), "(nothing set)"))
            for attribute, value in attributes.items():
                rows.extend(_attribute_rows(f"{page_name}.{attribute}", value))
        else:
            rows.extend(_attribute_rows(str(page_name), page_value))
    return others + rows


def _attribute_rows(name: str, value: Any) -> list:
    """One pair per value, so no cell can outgrow a page.

    A parameter holding a list -- the specialties a funnel offered, the
    kinds of place a facility search can return -- is one value but many
    lines, and flattened into a single cell it was 700 points tall against
    a frame of 693. Each element is addressed by its position, which is
    also how a person reads back which one they chose.
    """
    if isinstance(value, list) and value:
        return [(f"{name}[{i}]", _flat(item)) for i, item in enumerate(value)]
    return [(name, _flat(value))]


def _flat(value: Any) -> str:
    """A parameter as one readable line."""
    if value is None or value == "":
        return "(not set)"
    if isinstance(value, dict):
        return ", ".join(f"{k}: {v}" for k, v in value.items() if v) or "(not set)"
    if isinstance(value, list):
        if not value:
            return "(none)"
        out = []
        for item in value:
            if isinstance(item, dict):
                out.append(str(item.get("name") or item.get("code") or item))
            else:
                out.append(str(item))
        return ", ".join(out)
    return str(value)
