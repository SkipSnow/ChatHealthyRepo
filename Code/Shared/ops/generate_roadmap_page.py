#!/usr/bin/env python3
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
#
# Generates the milestone timeline section of Website/roadmap.html from brain JSON.
# Sources:
#   - brain/machine_artifacts/content/business_plan.json (timeline.milestones)
#   - brain/machine_artifacts/content/agile_backlog.json (epic summaries)
#
# IMPORTANT: This generator currently patches the milestones section of the existing
# roadmap.html. The full Gantt chart is hand-maintained until the complete page
# generator is built. This ensures milestones stay in sync with the brain.
#
# Usage: python Code/Shared/ops/generate_roadmap_page.py
# Run from repo root ($CHATHEALTHY_PROJECT_ROOT)

import json
import os
import re
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BRAIN = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")
ROADMAP = os.path.join(REPO_ROOT, "Website", "roadmap.html")

def load(name):
    with open(os.path.join(BRAIN, name), encoding="utf-8") as f:
        return json.load(f)

def get_milestones():
    """Extract milestones from business_plan.json — use first record with timeline."""
    bp = load("business_plan.json")
    for rec in bp.get("records", []):
        tl = rec.get("timeline", {})
        ms = tl.get("milestones", [])
        if ms:
            return [m for m in ms if m.get("publish", True)]
    return []

def milestone_to_gantt_pct(date_str, start="2026-04-01", end="2026-12-31"):
    """Convert a date to a percentage position on the Gantt chart timeline."""
    from datetime import date
    d = date.fromisoformat(date_str)
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    total = (e - s).days
    offset = (d - s).days
    return round(100 * offset / total, 1)

def main():
    milestones = get_milestones()

    with open(ROADMAP, "r", encoding="utf-8") as f:
        html = f.read()

    # Build milestone markers for the Business Milestones Gantt row
    colors = {
        "alpha": "var(--teal)",
        "funding": "#d97706",
        "seed": "#d97706",
        "beta": "#7c3aed",
        "production": "#059669",
        "native": "#2563eb",
        "discuss": "#7c3aed",
    }

    def pick_color(text):
        t = text.lower()
        for key, color in colors.items():
            if key in t:
                return color
        return "var(--ink)"

    markers_html = ""
    for m in milestones:
        pct = milestone_to_gantt_pct(m["date"])
        color = pick_color(m["milestone"])
        short = m["milestone"].split("(")[0].strip()
        month = datetime.fromisoformat(m["date"]).strftime("%b %d")
        markers_html += f'            <div class="gantt-milestone" style="left:{pct}%; background:{color}; border-color:{color};"></div>\n'
        markers_html += f'            <div class="gantt-biz-label" style="left:{pct}%; color:{color};">{short}<br>{month}</div>\n'

    # Replace the business milestones gantt-track content
    pattern = r'(<div class="gantt-track" style="position:relative;">\s*\n)(.*?)(          </div>\s*\n\s*</div>\s*\n\s*\n\s*<!-- ── Alpha milestone)'
    replacement = r'\1' + markers_html + r'\3'

    new_html, count = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)

    if count == 0:
        print("WARNING: Could not find Business Milestones row to patch. Roadmap unchanged.")
        print("Milestones from brain:")
        for m in milestones:
            print(f"  {m['date']} — {m['milestone']}")
        return

    with open(ROADMAP, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"Patched roadmap.html with {len(milestones)} milestones from brain:")
    for m in milestones:
        pct = milestone_to_gantt_pct(m["date"])
        print(f"  {m['date']} ({pct}%) — {m['milestone']}")

if __name__ == "__main__":
    main()
