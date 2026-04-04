#!/usr/bin/env python3
# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# build_code_review_pdf.py — Generate code review business document from brain JSON.
#
# Usage:
#   python build_code_review_pdf.py
#   python build_code_review_pdf.py --result gpt_code_review_result.json --output CodeReview/epic3_review.pdf

import argparse
import json
import os
import sys

from fpdf import FPDF

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BRAIN_CONTENT = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")
GIT_BASE_URL = "https://github.com/SkipSnow/ChatHealthyRepo/blob/dev"


def build_pdf(result_file, system_prompt_file, user_prompt_file, output_path):
    with open(os.path.join(BRAIN_CONTENT, result_file), encoding="utf-8") as f:
        result_data = json.load(f)
    with open(os.path.join(BRAIN_CONTENT, system_prompt_file), encoding="utf-8") as f:
        system_prompt = json.load(f)
    with open(os.path.join(BRAIN_CONTENT, user_prompt_file), encoding="utf-8") as f:
        user_prompt = json.load(f)

    def sanitize(text):
        """Replace unicode chars that latin-1 can't encode."""
        return (text.replace("\u2014", "--").replace("\u2013", "-")
                .replace("\u2018", "'").replace("\u2019", "'")
                .replace("\u201c", '"').replace("\u201d", '"')
                .replace("\u2192", "->").replace("\u2026", "..."))

    result_data["result"] = sanitize(result_data.get("result", ""))
    result_data["title"] = sanitize(result_data.get("title", ""))
    system_prompt["system_prompt"] = sanitize(system_prompt.get("system_prompt", ""))
    user_prompt["user_prompt"] = sanitize(user_prompt.get("user_prompt", ""))

    result_text = result_data.get("result", "")
    title = result_data.get("title", "Code Review")
    date = result_data.get("date", "")
    model = result_data.get("model", "")
    tokens_in = result_data.get("tokens_in", 0)
    tokens_out = result_data.get("tokens_out", 0)

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)

    def heading(text, size=16):
        pdf.set_font("Helvetica", "B", size)
        pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    def subheading(text, size=12):
        pdf.set_font("Helvetica", "B", size)
        pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    def body(text):
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 4.5, text)
        pdf.ln(2)

    def small(text):
        pdf.set_font("Helvetica", "", 8)
        pdf.multi_cell(0, 4, text)
        pdf.ln(1)

    # Page 1 — Title
    pdf.add_page()
    heading("Code Review Report", 20)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"Date: {date}  |  Model: {model}  |  Tokens: {tokens_in:,} in / {tokens_out:,} out", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, "Copyright 2026 Skip Snow. All rights reserved.", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Files reviewed with git URLs
    subheading("Files Reviewed")
    files = [
        "Code/DataPipelines/provider_load_manager.py",
        "Code/DataPipelines/provider_worker.py",
        "Code/DataPipelines/county_enrichment_job.py",
        "Code/DataPipelines/embedding_worker.py",
        "Code/DataPipelines/county_economic_enrichment.py",
        "Code/DataPipelines/data_fetcher_base.py",
        "Code/DataPipelines/discrepancy_reporter.py",
        "Code/DataPipelines/cluster_lifecycle_manager.py",
        "Code/DataPipelines/ops_manager/ops_agent.py",
        "Code/DataPipelines/ops_manager/cluster_tool.py",
        "Code/DataPipelines/ops_manager/alert_tool.py",
        "Code/DataPipelines/ops_manager/triage_tool.py",
        "Code/DataPipelines/ops_manager/audit_trail.py",
        "brain/machine_artifacts/content/controlled_vocabularies.json",
    ]
    for fp in files:
        small(f"  {GIT_BASE_URL}/{fp}")

    pdf.ln(4)

    # Result — split into sections by **Table or **Conclusion
    subheading("Audit Results")

    # Parse the markdown tables into readable text
    lines = result_text.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            pdf.ln(2)
            continue
        if line.startswith("**"):
            clean = line.replace("**", "").strip()
            subheading(clean, 11)
        elif line.startswith("|"):
            # Table row
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if cells and cells[0].startswith("---"):
                continue
            row = " | ".join(cells)
            small(row)
        elif line.startswith("-"):
            small(f"  {line}")
        else:
            body(line)

    # Appendix
    pdf.add_page()
    heading("Appendix A: Assignment", 14)

    subheading("System Prompt")
    body(system_prompt.get("system_prompt", ""))

    subheading("User Prompt")
    # Truncate very long user prompts for the PDF
    up_text = user_prompt.get("user_prompt", "")
    if len(up_text) > 5000:
        body(up_text[:5000] + "\n\n[... truncated for PDF ...]")
    else:
        body(up_text)

    pdf.output(output_path)
    print(f"PDF written: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate code review PDF from brain JSON")
    parser.add_argument("--result", default="gpt_code_review_result.json")
    parser.add_argument("--system-prompt", default="gpt_code_review_system_prompt.json")
    parser.add_argument("--user-prompt", default="gpt_code_review_user_prompt.json")
    parser.add_argument("--output", default=os.path.join(
        REPO_ROOT, "brain", "BusinessArtifacts", "CodeReview", "epic3_pipeline_code_review.pdf"))
    args = parser.parse_args()
    build_pdf(args.result, args.system_prompt, args.user_prompt, args.output)


if __name__ == "__main__":
    main()
