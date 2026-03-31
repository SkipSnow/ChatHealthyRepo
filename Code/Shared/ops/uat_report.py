# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# uat_report.py — DevOps tool: generates clean UAT welcome report.
#
# Starts blank every session. Reads features from brain/uat config.
# Build number comes from /health endpoint at runtime.
# Done, Bugs Fixed, New Features columns are ALWAYS blank at start.
#
# Usage: imported by main.py, called to build the welcome message.
# Location: Code/Shared/ops/ (DevOps tool, not business logic)

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("findcare.uat_report")

# UAT feature definitions — the WHAT, not the results
# Results are filled in by Boss during testing
UAT_FEATURES = [
    {"id": 1,  "feature": "Provider Search (DE + MS + VA, vector + regex)"},
    {"id": 2,  "feature": "Specialty Identification (NUCC + AI expansion)"},
    {"id": 3,  "feature": "Clinical Trials Search (+ travel time)"},
    {"id": 4,  "feature": "About ChatHealthy / Skip Snow"},
    {"id": 5,  "feature": "Safety Filter (dual-trigger, IP lock, audit)"},
    {"id": 6,  "feature": "Lead Capture (follow-up offer)"},
    {"id": 7,  "feature": "Consent Framework (two-stream)"},
    {"id": 8,  "feature": "Provider Detail (NPI lookup + external links)"},
    {"id": 9,  "feature": "URL Guardian (validate + defang broken links)"},
    {"id": 10, "feature": "Chat UX (timer, stop, markdown, emergency)"},
    {"id": 11, "feature": "Blob Storage Infrastructure"},
    {"id": 12, "feature": "Unanswerable Question Handling (3-path)"},
    {"id": 13, "feature": "Markdown Table Rendering (GFM tables in chat)"},
]


def build_uat_welcome(build: str, version: str, env: str) -> str:
    """Build a clean UAT welcome message. All result columns blank.

    Args:
        build: current build number from MongoDB
        version: app version (e.g., v0.1.3)
        env: environment label (local/dev/qa/prod)
    """
    total = len(UAT_FEATURES)

    lines = [
        f"**UAT Session: {version}**\n\n"
        f"| Environment | Build | Scenarios |\n"
        f"|:-----------:|:-----:|:---------:|\n"
        f"| {env.upper()} | {build} | {total} |\n\n",
        "| # | Feature | Done | Bugs Fixed | New Features |",
        "|:---:|---------|:----:|:----------:|:------------:|",
    ]

    for f in UAT_FEATURES:
        lines.append(
            f"| {f['id']:>3} | {f['feature']} | | | |"
        )

    lines.append(
        f"| | **TOTALS** | **0/{total}** | **0** | **0** |"
    )

    lines.append(
        "\n*Boss: mark Done (Y/DEF/FAIL), tally bugs and features as you test.*"
    )

    return "\n".join(lines)
