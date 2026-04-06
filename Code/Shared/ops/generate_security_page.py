#!/usr/bin/env python3
# Copyright (c) 2026 Skip Snow. All rights reserved.
#
# Generates Website/security-architecture.html from brain JSON sources of truth.
# Sources:
#   - brain/machine_artifacts/content/security.json (design, controls, CUI readiness)
#   - brain/machine_artifacts/content/agile_backlog.json (EPIC-4 features/stories/requirements)
#   - brain/machine_artifacts/content/policies.json (GOV-005, DC-GOV-02)
#
# Usage: python Code/Shared/ops/generate_security_page.py
# Run from repo root (c:\chatHealthy\findCare)

import json
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BRAIN = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")
OUTPUT = os.path.join(REPO_ROOT, "Website", "security-architecture.html")

def load(name):
    with open(os.path.join(BRAIN, name), encoding="utf-8") as f:
        return json.load(f)

def escape(text):
    """HTML-escape text from brain JSON."""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("—", "&mdash;")
        .replace("–", "&ndash;")
        .replace("→", "&rarr;")
        .replace("×", "&times;")
        .replace("'", "&#39;"))

def main():
    security = load("security.json")
    backlog = load("agile_backlog.json")
    policies = load("policies.json")

    sec_record = security["records"][0]
    generated = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Find EPIC-4
    epic4 = None
    for epic in backlog.get("records", backlog.get("epics", [])):
        if epic.get("epic_id") == "EPIC-4":
            epic4 = epic
            break

    # Find DC-GOV-02 for the HIPAA disclosure
    hipaa_disclosure = ""
    for p in policies.get("corporate_policies", []):
        if p.get("id") == "DC-GOV-02":
            hipaa_disclosure = p.get("policy", "")
            break

    # Build NIST table rows
    nist = sec_record.get("compliance_mapping", {}).get("nist_800_171", {})
    nist_rows = ""
    for family in nist.get("controls", []):
        for ctrl in family.get("controls_satisfied", []):
            nist_rows += f"""        <tr>
          <td><span class="control-badge">{escape(ctrl['id'])}</span></td>
          <td>{escape(ctrl['control'])}</td>
          <td>{escape(ctrl['how'])}</td>
        </tr>\n"""

    # Build HITRUST cards
    hitrust = sec_record.get("compliance_mapping", {}).get("hitrust_csf", {})
    hitrust_html = ""
    for domain in hitrust.get("domains", []):
        hitrust_html += f'    <h3>{escape(domain["domain"])}</h3>\n'
        for ctrl in domain.get("controls_satisfied", []):
            hitrust_html += f"""    <div class="compliance-card">
      <h4><span class="control-badge">{escape(ctrl['id'])}</span> {escape(ctrl['control'])}</h4>
      <p>{escape(ctrl['how'])}</p>
    </div>\n"""

    # Build EPIC-4 feature summary table
    epic4_rows = ""
    if epic4:
        for feat in epic4.get("features", []):
            story_count = len(feat.get("stories", []))
            req_count = sum(len(s.get("requirements", [])) for s in feat.get("stories", []))
            status = feat.get("status", "")
            epic4_rows += f"""        <tr>
          <td>{escape(feat['feature_id'])}</td>
          <td>{escape(feat['name'])}</td>
          <td>{story_count}</td>
          <td>{req_count}</td>
          <td>{escape(status)}</td>
        </tr>\n"""

    # Component handoff
    handoff = sec_record.get("component_handoff", {})
    handoff_desc = escape(handoff.get("description", ""))
    handoff_svg = handoff.get("website_svg", "").replace("Website/", "")

    # Local security controls
    local_controls = ""
    for ctrl in sec_record.get("local_security_architecture", {}).get("security_controls", []):
        local_controls += f"      <li>{escape(ctrl)}</li>\n"

    # Deployed security controls
    deployed_controls = ""
    for ctrl in sec_record.get("deployed_security_architecture", {}).get("security_controls", []):
        deployed_controls += f"      <li>{escape(ctrl)}</li>\n"

    # CUI readiness
    cui = sec_record.get("cui_readiness", {})
    cui_supports = ""
    for item in cui.get("architecture_supports", []):
        cui_supports += f"      <li>{escape(item)}</li>\n"
    cui_requires = ""
    for item in cui.get("requires_before_deployment", []):
        cui_requires += f"      <li>{escape(item)}</li>\n"

    html = f"""<!DOCTYPE html>
<!--
  Copyright (c) 2026 Skip Snow. All rights reserved.
  Licensed under the FindCare Evaluation License (FEL-1.0).

  GENERATED FILE — do not edit directly.
  Source of truth: brain/machine_artifacts/content/security.json
  Generator: Code/Shared/ops/generate_security_page.py
  Generated: {generated}
-->
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Security Architecture &mdash; ChatHealthy.ai</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet" />

  <style>
    :root {{
      --ink:        #0d1117;
      --ink-soft:   #4a5568;
      --surface:    #ffffff;
      --teal:       #0b7a75;
      --teal-light: #e6f4f3;
      --teal-mid:   #d0eeec;
      --accent:     #f0a500;
      --warn:       #c0392b;
      --border:     #d8e2e1;
      --radius:     6px;
      --max-w:      1160px;
      --content-w:  860px;
    }}

    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{ font-family: 'DM Sans', sans-serif; background: var(--surface); color: var(--ink); }}
    body {{ display: flex; flex-direction: column; min-height: 100vh; }}

    header {{ background: var(--surface); border-bottom: 1px solid var(--border); box-shadow: 0 1px 6px rgba(11,122,117,0.06); }}
    .header-inner {{ max-width: var(--max-w); margin: 0 auto; padding: 0 24px; height: 56px; display: flex; align-items: center; }}
    .logo {{ display: flex; align-items: center; gap: 10px; text-decoration: none; flex-shrink: 0; }}
    .logo-mark {{ width: 32px; height: 32px; border-radius: 50%; background: var(--teal); display: flex; align-items: center; justify-content: center; }}
    .logo-mark svg {{ width: 18px; height: 18px; fill: white; }}
    .logo-text {{ font-family: 'DM Serif Display', serif; font-size: 1.1rem; color: var(--teal); letter-spacing: -0.01em; }}
    .logo-text span {{ color: var(--accent); }}

    main {{ flex: 1; max-width: var(--max-w); width: 100%; margin: 0 auto; padding: clamp(20px,4vw,48px) clamp(16px,4vw,24px); }}

    .page-header {{ border-bottom: 2px solid var(--teal-mid); padding-bottom: 20px; margin-bottom: 36px; }}
    .page-header .eyebrow {{ font-size: 0.72rem; font-weight: 500; letter-spacing: 0.08em; text-transform: uppercase; color: var(--teal); margin-bottom: 6px; }}
    .page-header h1 {{ font-family: 'DM Serif Display', serif; font-size: clamp(1.5rem,4vw,2.25rem); color: var(--ink); line-height: 1.15; margin-bottom: 8px; }}
    .page-header .effective {{ font-size: 0.82rem; color: var(--ink-soft); }}

    .arch-body {{ max-width: var(--content-w); }}
    .arch-body h2 {{ font-family: 'DM Serif Display', serif; font-size: 1.4rem; color: var(--teal); margin: 40px 0 12px; padding-bottom: 8px; border-bottom: 3px solid var(--teal); }}
    .arch-body h2:first-child {{ margin-top: 0; }}
    .arch-body h3 {{ font-size: 0.88rem; font-weight: 500; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink-soft); margin: 24px 0 8px; }}
    .arch-body p {{ font-size: 0.92rem; line-height: 1.7; color: var(--ink); margin-bottom: 10px; }}
    .arch-body ul, .arch-body ol {{ margin: 6px 0 10px 20px; font-size: 0.92rem; line-height: 1.7; }}
    .arch-body ul li, .arch-body ol li {{ margin-bottom: 4px; }}

    .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 12px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; min-width: 480px; }}
    th {{ background: var(--ink); color: #fff; padding: 8px 12px; text-align: left; font-weight: 500; font-size: 0.82rem; letter-spacing: 0.03em; }}
    td {{ border: 1px solid var(--border); padding: 8px 12px; vertical-align: top; line-height: 1.5; }}
    tr:nth-child(even) td {{ background: var(--teal-light); }}

    .diagram-container {{
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: #fafafa;
      padding: 16px;
      margin: 16px 0;
      overflow: hidden;
    }}
    .diagram-container img {{
      width: 100%;
      height: auto;
      display: block;
    }}

    .control-badge {{
      display: inline-block;
      background: var(--teal-light);
      border: 1px solid var(--teal-mid);
      border-radius: 3px;
      padding: 2px 8px;
      font-size: 0.78rem;
      font-weight: 500;
      color: var(--teal);
      margin-right: 4px;
      margin-bottom: 4px;
    }}

    .compliance-card {{
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
      margin: 12px 0;
      background: #fff;
    }}
    .compliance-card h4 {{
      font-size: 0.92rem;
      font-weight: 500;
      color: var(--teal);
      margin-bottom: 8px;
    }}

    .gen-notice {{ font-size: 0.72rem; color: var(--ink-soft); margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--border); }}

    footer {{ background: var(--surface); border-top: 1px solid var(--border); padding: 16px 24px; text-align: center; font-size: 0.78rem; color: var(--ink-soft); }}
  </style>
</head>
<body>

<header>
  <div class="header-inner">
    <a class="logo" href="/">
      <div class="logo-mark"><svg viewBox="0 0 24 24"><path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-1 14.5v-2h2v2h-2Zm0-4v-5h2v5h-2Z"/></svg></div>
      <div class="logo-text">chat<span>Healthy</span>.ai</div>
    </a>
  </div>
</header>

<main>
  <div class="page-header">
    <div class="eyebrow">EPIC-4: Security</div>
    <h1>Security Architecture</h1>
    <div class="effective">PKI, Key Vault, and Zero-Trust Credential Management &mdash; {sec_record['created']}</div>
  </div>

  <div class="arch-body">

    <h2>1. Local Security Architecture</h2>

    <div class="diagram-container">
      <img src="Images/security_architecture_local.svg" alt="Local Security Architecture Diagram" />
    </div>

    <p>{escape(sec_record['local_security_architecture']['description'])}</p>

    <h3>Local Security Controls</h3>
    <ul>
{local_controls}    </ul>

    <h2>2. Deployed Security Architecture</h2>

    <div class="diagram-container">
      <img src="Images/security_architecture_deployed.svg" alt="Deployed Security Architecture Diagram" />
    </div>

    <p>{escape(sec_record['deployed_security_architecture']['description'])}</p>

    <h3>Deployed Security Controls</h3>
    <ul>
{deployed_controls}    </ul>

    <h2>3. Component Handoff — Security in Action</h2>

    <p>{handoff_desc}</p>

    <div class="diagram-container">
      <img src="{handoff_svg}" alt="FindCare to EvaluateCare Handoff Sequence Diagram" />
    </div>

    <h2>4. EPIC-4 Security Features</h2>

    <div class="table-wrap">
      <table>
        <tr><th>Feature ID</th><th>Name</th><th>Stories</th><th>Requirements</th><th>Status</th></tr>
{epic4_rows}      </table>
    </div>

    <h2>5. NIST SP 800-171 Compliance</h2>

    <p>{escape(nist.get('applicability', ''))}</p>

    <div class="table-wrap">
      <table>
        <tr><th>Control</th><th>Requirement</th><th>How Satisfied</th></tr>
{nist_rows}      </table>
    </div>

    <h2>6. HITRUST CSF v11 Compliance</h2>

    <p>{escape(hitrust.get('applicability', ''))}</p>

{hitrust_html}

    <h2>7. CUI Readiness</h2>

    <p>{escape(cui.get('description', ''))}</p>

    <h3>Architecture Supports</h3>
    <ul>
{cui_supports}    </ul>

    <h3>Requires Before Deployment</h3>
    <ul>
{cui_requires}    </ul>

    <p class="gen-notice">
      Generated from brain/machine_artifacts/content/security.json and agile_backlog.json (EPIC-4).<br/>
      Generator: Code/Shared/ops/generate_security_page.py<br/>
      Generated: {generated}<br/>
      Do not edit this file directly &mdash; edit the brain JSON and regenerate.
    </p>

  </div>
</main>

<footer>
  &copy; 2026 Skip Snow. All rights reserved.
</footer>

</body>
</html>"""

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Generated: {OUTPUT}")
    print(f"  Security controls: {len(sec_record['local_security_architecture']['security_controls'])} local, {len(sec_record['deployed_security_architecture']['security_controls'])} deployed")
    if epic4:
        feat_count = len(epic4.get("features", []))
        story_count = sum(len(f.get("stories", [])) for f in epic4.get("features", []))
        req_count = sum(len(s.get("requirements", [])) for f in epic4.get("features", []) for s in f.get("stories", []))
        print(f"  EPIC-4: {feat_count} features, {story_count} stories, {req_count} requirements")

if __name__ == "__main__":
    main()
