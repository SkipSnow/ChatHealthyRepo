# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_refactor_collab.py — Claude ↔ GPT iterative design collaboration for ARCH-001.
#
# GPT proposes, Claude responds. Iterate until agreed or max iterations.
# Output: JSON + PDF + transcript
#
# Usage: python brain_refactor_collab.py

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
BRAIN_DIR = PROJECT_ROOT / "brain"
MAX_ITERATIONS = 100

SYSTEM_PROMPT = json.dumps({
    "role": "Chief Hands-On Enterprise Architect",
    "identity": "GPT",
    "background": "30 years experience as delivery manager and application architect with absolute contemporary AI-native skills",
    "project": "ChatHealthy.ai — Full Codebase Refactor (ARCH-001)",
    "assignment_type": "Iterative Design Collaboration",
    "rules": [
        "Think carefully. Review your work twice before responding.",
        "You are collaborating with Claude (engineer) iteratively. Claude will respond to each of your outputs. The goal is an agreed design in fewer than 100 iterations.",
        "Your output must be actionable — class names, file paths, method signatures, dependency direction.",
        "Two business components are MANDATORY: 'Find Care' and 'Evaluate Care Quality'. This is a business architecture decision, not negotiable.",
        "For each of the 13 UAT features, you must either propose a separate class OR justify why it belongs in a shared class.",
        "HUGGINGFACE CONSTRAINT: HuggingFace hosting surface must be the absolute smallest possible. NO business logic in HuggingFace. HuggingFace contains only the deployment entry point, static assets, and the thinnest possible host adapter. All business logic, tool implementations, safety, consent, and domain classes must be host-independent and portable to any platform.",
        "REFACTOR SCOPE: The entire collection of files committed to git — not just main.py. This includes backend, frontend, shared libraries, pipelines, ops scripts, brain artifacts, and deployment workflows.",
        "You have received a Perplexity external audit with 18 findings. Your design must address or acknowledge each finding.",
        "Do NOT propose microservices, separate deployments, or new infrastructure. This is a code refactor within the existing architecture.",
        "Do NOT propose multi-tenancy. Single-tenant by design (GOV-002).",
        "DELIVERABLES: Your final agreed design must be produced as three artifacts: (1) JSON machine artifact, (2) PDF business artifact, (3) Word document with architecture diagrams. Claude will generate these from your final output.",
        "DIAGRAMS: Your Word document must include architecture diagrams — component diagram, dependency diagram, and file tree. Use simple boxes and lines, white backgrounds, black text. No crossed lines. No overlapping objects.",
    ],
    "governance_policies": [
        "GOV-001: Provider Neutrality — no revenue from care-chain entities",
        "GOV-002: Single-Tenant Architecture — multi-tenancy is a healthcare anti-pattern",
        "GOV-003: Public By Default (temporary)",
        "GOV-004: AI Governance — The model may suggest. The system must decide.",
        "GOV-005: Three-Application Architecture — Website (Cloudflare), Chat (HuggingFace), Pipelines (Azure). Strict boundaries.",
        "GOV-007: Production changes require Boss sign-off",
    ],
    "constraints": [
        "Host: HuggingFace Docker Space — THINNEST POSSIBLE surface. No business logic. Entry point and static assets only.",
        "LLM: Anthropic Claude Sonnet 4.6 (tool-use, streaming)",
        "Database: MongoDB Atlas (reads only from Chat app, writes to PublicHealthData prohibited)",
        "External APIs: ClinicalTrials.gov, NPI Registry, Google Routes, OpenAI embeddings",
        "Python 3.12, single process, no background workers",
        "Will stay on HuggingFace for next couple of sprints, but architecture must be portable",
    ],
    "iteration_protocol": {
        "max_iterations": MAX_ITERATIONS,
        "goal": "Agreed design in fewer than 100 iterations",
        "process": "You propose, Claude responds with feedback/questions/counter-proposals, you revise. Repeat until both agree.",
        "termination": "Either party calls for final sign-off when design is stable",
    },
    "max_think_seconds": 300,
}, indent=2, default=str)


def _build_user_prompt() -> str:
    """Build the initial assignment prompt."""
    # Read source files for context
    main_py = (PROJECT_ROOT / "Code" / "ConversationalUX" / "FindCareChat" / "backend" / "main.py")
    main_lines = len(main_py.read_text(encoding="utf-8", errors="replace").splitlines()) if main_py.exists() else "?"

    # Read external audit
    audit_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "external_audit_v1.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    findings_summary = []
    for f in audit.get("findings", []):
        findings_summary.append(f"- {f['id']} [{f['severity'].upper()}]: {f['title']}")

    # Read backlog
    backlog_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "backlog_0_1_4_perplexity.json"
    backlog = json.loads(backlog_path.read_text(encoding="utf-8")) if backlog_path.exists() else {}
    backlog_summary = []
    for b in backlog.get("items", []):
        backlog_summary.append(f"- {b['id']}: {b['title']} [{b['priority']}]")

    return f"""# ASSIGNMENT: Full Codebase Refactor Design (ARCH-001)

This is an iterative design collaboration. You propose, I (Claude, engineer) respond.
We iterate until we agree. Goal: agreed design in under 100 iterations.

## BUSINESS ARCHITECTURE (MANDATORY)

Exactly TWO business components:

1. **Find Care** — help users find healthcare providers, specialties, navigate the system
2. **Evaluate Care Quality** — help users evaluate care options (clinical trials, provider credentials, quality metrics)

Every feature belongs to one of these or to shared infrastructure. This is fundamental.

## HUGGINGFACE CONSTRAINT

HuggingFace is the current host but must have the SMALLEST POSSIBLE surface:
- No business logic in HuggingFace content
- HuggingFace contains ONLY: deployment entry point, static assets, thinnest host adapter
- All business logic must be host-independent and portable
- We stay on HuggingFace for the next couple of sprints, but the architecture must allow migration to any host with minimal change

## REFACTOR SCOPE

The ENTIRE codebase committed to git:

```
ChatHealthyRepo/
  Code/
    ConversationalUX/FindCareChat/       <- React frontend + FastAPI backend
    ConversationalUX/ChatHealthyWhoAmIChat/  <- Legacy (retiring)
    DataPipelines/                        <- Azure Function App
    Shared/                               <- Common utilities, ops scripts
  Website/                                <- Static site (Cloudflare)
  brain/                                  <- BusinessArtifacts, machine_artifacts, manifest
  .github/workflows/                      <- CI/CD
```

## CURRENT STATE — 13 UAT FEATURES

For EACH, propose a separate class or justify shared:

| # | Feature | Current Location |
|---|---------|-----------------|
| 1 | Provider Search (DE+MS+VA, vector+regex) | find_providers() in main.py |
| 2 | Specialty Identification (NUCC + AI) | find_specialty_codes() in main.py |
| 3 | Clinical Trials Search (+ travel time) | search_clinical_trials() + _get_travel_info() in main.py |
| 4 | About ChatHealthy / Skip Snow | get_skip_snow_context() / get_chathealthy_context() in main.py |
| 5 | Safety Filter (dual-trigger, IP lock, audit) | _safety_check() + helpers in main.py |
| 6 | Lead Capture (follow-up offer) | record_user_details() in main.py |
| 7 | Consent Framework (two-stream) | Inside record_user_details() + system prompt |
| 8 | Provider Detail (NPI lookup + external links) | lookup_provider_external() in main.py |
| 9 | URL Guardian (validate + defang) | url_guardian.py — URLGuardian class |
| 10 | Chat UX (timer, stop, markdown, emergency) | FastAPI endpoints + React frontend |
| 11 | Blob Storage Infrastructure | Azure blob client in DataPipelines |
| 12 | Unanswerable Question Handling (3-path) | record_unknown_question() in main.py |
| 13 | Markdown Table Rendering (GFM) | Frontend only (remark-gfm) |

## CURRENT main.py ({main_lines} lines)

- 9 tool functions (all in one file)
- Safety enforcement (functions, not classes)
- Support helpers (de-identify, query expansion, travel info)
- System prompt builder (_system_prompt() with 7 rules)
- FastAPI endpoints (/welcome, /health, /chat)
- Tool router (_handle_tool_calls via globals().get(name) — SECURITY RISK)
- URL Guardian (separate file — only existing class)

## PERPLEXITY EXTERNAL AUDIT (EXT-AUDIT-001)

{chr(10).join(findings_summary)}

## PERPLEXITY BACKLOG (v0.1.4 — acknowledge, don't implement)

{chr(10).join(backlog_summary)}

## REFERENCE DOCUMENTS

Available in brain/BusinessArtifacts/:
- external_audit_v1.docx — full Perplexity audit
- staffing_cost_comparison.docx — staffing/cost analysis

## DELIVERABLES

When we reach agreement, the final design must produce THREE artifacts:
1. **JSON** — machine artifact (brain/machine_artifacts/document_type_json/refactor_design_arch001.json)
2. **PDF** — business artifact (brain/BusinessArtifacts/refactor_design_arch001.pdf)
3. **Word document** — with architecture DIAGRAMS (component diagram, dependency diagram, file tree)

Claude will generate these from your final agreed output.

## ITERATION PROTOCOL

- You propose, I respond with feedback, questions, or counter-proposals
- We revise until both agree
- Max 100 iterations. Goal: agreement in fewer than 100.
- When stable, either of us calls for final sign-off

Begin with your initial design proposal. Return JSON."""


def call_gpt(messages: list) -> tuple:
    """Call GPT-5.3. Returns (response_text, tokens_in, tokens_out)."""
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.3-chat-latest",
        messages=messages,
    )
    text = response.choices[0].message.content
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


def main():
    print("=" * 70)
    print("ARCH-001: Full Codebase Refactor -- Claude + GPT Design Collaboration")
    print(f"Model: gpt-5.3-chat-latest  |  Max iterations: {MAX_ITERATIONS}")
    print("=" * 70)

    user_prompt = _build_user_prompt()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    total_in = total_out = 0
    transcript = []
    iteration = 0

    # Iteration 1: GPT's opening proposal
    iteration += 1
    print(f"\n{'='*50}")
    print(f"Iteration {iteration}/{MAX_ITERATIONS} — GPT proposing...")
    print(f"{'='*50}")

    gpt_text, tin, tout = call_gpt(messages)
    total_in += tin
    total_out += tout
    print(f"GPT responded: {tin:,} in / {tout:,} out")
    print(f"Preview: {gpt_text[:500]}...")

    transcript.append({
        "iteration": iteration,
        "speaker": "GPT",
        "tokens_in": tin,
        "tokens_out": tout,
        "content": gpt_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Save GPT's initial proposal
    proposal_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "refactor_proposal_iteration1.json"
    proposal_path.write_text(gpt_text if gpt_text.strip().startswith("{") else json.dumps({"raw": gpt_text}, default=str), encoding="utf-8")
    print(f"\nInitial proposal saved: {proposal_path}")

    # Save transcript
    transcript_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "refactor_collab_transcript.json"
    transcript_data = {
        "title": "ARCH-001 Refactor Design Collaboration Transcript",
        "model": "gpt-5.3-chat-latest",
        "started": datetime.now(timezone.utc).isoformat(),
        "iterations": iteration,
        "total_tokens": {"input": total_in, "output": total_out},
        "transcript": transcript,
    }
    transcript_path.write_text(json.dumps(transcript_data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    print(f"Transcript saved: {transcript_path}")

    print(f"\n{'='*70}")
    print(f"Iteration 1 complete. GPT has proposed.")
    print(f"Tokens: {total_in:,} in / {total_out:,} out")
    print(f"{'='*70}")
    print(f"\nNext: Claude reviews and responds. Run this script again with --respond to continue.")

    return gpt_text


if __name__ == "__main__":
    main()
