# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_refactor_collab.py — Claude + GPT iterative design collaboration for ARCH-001.
#
# GPT proposes, Claude responds. Iterate until agreed or max iterations.
# Output: JSON proposal per iteration + full transcript
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
    "project": "ChatHealthy.ai -- Full Codebase Refactor (ARCH-001)",
    "assignment_type": "Iterative Design Collaboration",
    "rules": [
        "Think carefully. Review your work twice before responding.",
        "You are collaborating with Claude (engineer) iteratively. Claude will respond to each of your outputs. The goal is an agreed design in fewer than 100 iterations.",
        "Your output must be actionable -- class names, file paths, method signatures, dependency direction.",
        "Two business components are MANDATORY: 'Find Care' and 'Evaluate Care Quality'. This is a business architecture decision, not negotiable.",
        "For each of the 13 UAT features, you must either propose a separate class OR justify why it belongs in a shared class.",
        "HUGGINGFACE CONSTRAINT: HuggingFace hosting surface must be the absolute smallest possible. NO business logic in HuggingFace. HuggingFace contains only the deployment entry point, static assets, and the thinnest possible host adapter. All business logic, tool implementations, safety, consent, and domain classes must be host-independent and portable to any platform.",
        "REFACTOR SCOPE: The entire collection of files committed to git -- not just main.py. This includes backend, frontend, shared libraries, pipelines, ops scripts, brain artifacts, and deployment workflows.",
        "You have received a Perplexity external audit with 18 findings. Your design must address or acknowledge each finding.",
        "Do NOT propose microservices, separate deployments, or new infrastructure. This is a code refactor within the existing architecture.",
        "Do NOT propose multi-tenancy. Single-tenant by design (GOV-002).",
        "DELIVERABLES: Your final agreed design must be produced as three artifacts: (1) JSON machine artifact, (2) PDF business artifact, (3) Word document with architecture diagrams. Claude will generate these from your final output.",
        "DIAGRAMS: Your Word document must include architecture diagrams -- component diagram, dependency diagram, and file tree. Use simple boxes and lines, white backgrounds, black text. No crossed lines. No overlapping objects.",
    ],
    "governance_policies": [
        "GOV-001: Provider Neutrality -- no revenue from care-chain entities",
        "GOV-002: Single-Tenant Architecture -- multi-tenancy is a healthcare anti-pattern",
        "GOV-003: Public By Default (temporary)",
        "GOV-004: AI Governance -- The model may suggest. The system must decide.",
        "GOV-005: Three-Application Architecture -- Website (Cloudflare), Chat (HuggingFace), Pipelines (Azure). Strict boundaries.",
        "GOV-007: Production changes require Boss sign-off",
    ],
    "constraints": [
        "Host: HuggingFace Docker Space -- THINNEST POSSIBLE surface. No business logic. Entry point and static assets only.",
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


def _build_initial_prompt() -> str:
    main_py = (PROJECT_ROOT / "Code" / "ConversationalUX" / "FindCareChat" / "backend" / "main.py")
    main_lines = len(main_py.read_text(encoding="utf-8", errors="replace").splitlines()) if main_py.exists() else "?"

    audit_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "external_audit_v1.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}
    findings_summary = []
    for f in audit.get("findings", []):
        findings_summary.append(f"- {f['id']} [{f['severity'].upper()}]: {f['title']}")

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

1. **Find Care** -- help users find healthcare providers, specialties, navigate the system
2. **Evaluate Care Quality** -- help users evaluate care options (clinical trials, provider credentials, quality metrics)

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

## CURRENT STATE -- 13 UAT FEATURES

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
| 9 | URL Guardian (validate + defang) | url_guardian.py -- URLGuardian class |
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
- Tool router (_handle_tool_calls via globals().get(name) -- SECURITY RISK)
- URL Guardian (separate file -- only existing class)

## PERPLEXITY EXTERNAL AUDIT (EXT-AUDIT-001)

{chr(10).join(findings_summary)}

## PERPLEXITY BACKLOG (v0.1.4 -- acknowledge, don't implement)

{chr(10).join(backlog_summary)}

## REFERENCE DOCUMENTS

Available in brain/BusinessArtifacts/:
- external_audit_v1.docx -- full Perplexity audit
- staffing_cost_comparison.docx -- staffing/cost analysis

## DELIVERABLES

When we reach agreement, the final design must produce THREE artifacts:
1. **JSON** -- machine artifact (brain/machine_artifacts/document_type_json/refactor_design_arch001.json)
2. **PDF** -- business artifact (brain/BusinessArtifacts/refactor_design_arch001.pdf)
3. **Word document** -- with architecture DIAGRAMS (component diagram, dependency diagram, file tree)

Claude will generate these from your final agreed output.

## ITERATION PROTOCOL

- You propose, I respond with feedback, questions, or counter-proposals
- We revise until both agree
- Max 100 iterations. Goal: agreement in fewer than 100.
- When stable, either of us calls for final sign-off

Begin with your initial design proposal. Return JSON."""


CLAUDE_ITERATION_2 = """## Claude's Response to Iteration 1 -- Boss Sign-Off on Open Questions

Your initial proposal is strong. Clean layering, correct feature mapping, good security fixes.
Boss has reviewed and signed off on your three open questions:

### Q1: System prompt ownership -- Boss decision: START SIMPLE
Keep system prompt in ChatOrchestrator for now. Do NOT extract a PromptBuilder class yet.
We are at Framework 0.3 -- earn the complexity. Extract PromptBuilder when we have multiple
prompt strategies (e.g., different prompts per business component). Premature abstraction is
worse than refactoring later.

### Q2: Facade pattern -- Boss decision: FACADE-TO-FACADE
Yes, components must communicate, but FindCareFacade talks to EvaluateCareFacade -- never
directly to internal services. No internals of either component exposed to the other.
Example: if clinical trials needs provider location, it calls FindCareFacade.get_provider_location(),
not ProviderSearchService.search() directly.

### Q3: Pydantic -- Boss decision: YES, for tool boundaries
Pydantic for all tool inputs/outputs (the LLM boundary). Three reasons:
1. FastAPI already uses pydantic -- it's in the stack
2. Anthropic tool-use returns JSON that maps directly to pydantic models
3. When we go agentic (CrewAI), every tool must be typed. Retrofitting later is expensive.
4. Pydantic IS the enforcement layer for GOV-004 ("the system must decide")

Nuance: Pydantic for tool inputs/outputs ONLY (the boundary). Internal domain objects stay as
plain dicts/dataclasses until we prove we need more. Do not over-pydantic the internals.

### My engineering feedback on your proposal:

AGREED:
- 4-layer architecture (Host Adapter -> Application -> Domain -> Infrastructure)
- HuggingFace reduced to app.py + adapter
- ToolRouter replacing globals().get()
- Every UAT feature gets its own service class
- Clinical Trials in EvaluateCareQuality
- Provider Search + Specialty + Provider Detail in FindCare
- Dependency direction: Host -> Application -> Domain -> Infrastructure (no upward)

QUESTIONS:
1. Provider Detail (Feature 8) -- you put it in FindCare. I'd argue it belongs in
   EvaluateCareQuality. Looking up a provider's credentials, board certifications,
   and research links is EVALUATING quality, not finding care. Thoughts?

2. Your proposed_repo_structure puts everything under Code/ConversationalUX/FindCareChat/.
   But the refactor scope is the ENTIRE repo. Where do DataPipelines, Shared, brain, and
   Website fit in your layered model? They need to be addressed.

3. The facade pattern needs to be explicit in the file tree. I don't see FindCareFacade
   or EvaluateCareFacade in your proposed structure. Add them.

4. Where does the system prompt content live? The 7 rules reference specific tool names
   and behaviors. If tools move to service classes, the prompt must stay in sync.
   Propose a strategy.

5. EmergencyDetector (F-09) -- good call separating it. Should it be configurable from
   brain/machine_artifacts (keywords loaded from JSON) so Boss can update emergency
   keywords without a code deploy?

Please revise your proposal addressing these 5 points and the 3 Boss decisions. Return JSON."""


def call_gpt(messages: list) -> tuple:
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.3-chat-latest",
        messages=messages,
    )
    text = response.choices[0].message.content
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


def save_iteration(iteration: int, speaker: str, content: str, transcript: list,
                   total_in: int, total_out: int):
    """Save current state to disk."""
    transcript_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "refactor_collab_transcript.json"
    transcript_data = {
        "title": "ARCH-001 Refactor Design Collaboration Transcript",
        "model": "gpt-5.3-chat-latest",
        "updated": datetime.now(timezone.utc).isoformat(),
        "iterations": iteration,
        "total_tokens": {"input": total_in, "output": total_out},
        "transcript": transcript,
    }
    transcript_path.write_text(json.dumps(transcript_data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    # Save latest GPT proposal
    if speaker == "GPT" and content.strip().startswith("{"):
        proposal_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / f"refactor_proposal_iteration{iteration}.json"
        proposal_path.write_text(content, encoding="utf-8")


def main():
    print("=" * 70)
    print("ARCH-001: Full Codebase Refactor -- Claude + GPT Collaboration")
    print(f"Model: gpt-5.3-chat-latest  |  Max iterations: {MAX_ITERATIONS}")
    print("=" * 70)

    initial_prompt = _build_initial_prompt()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]

    total_in = total_out = 0
    transcript = []

    # Load iteration 1 from disk if it exists
    iter1_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "refactor_proposal_iteration1.json"
    if iter1_path.exists():
        iter1_content = iter1_path.read_text(encoding="utf-8")
        print("Loaded iteration 1 from disk (GPT's initial proposal).")
        messages.append({"role": "assistant", "content": iter1_content})
        transcript.append({
            "iteration": 1, "speaker": "GPT",
            "content": iter1_content,
            "timestamp": "loaded_from_disk",
        })
    else:
        # Get iteration 1 from GPT
        print("\nIteration 1/100 -- GPT proposing...")
        gpt_text, tin, tout = call_gpt(messages)
        total_in += tin
        total_out += tout
        messages.append({"role": "assistant", "content": gpt_text})
        transcript.append({
            "iteration": 1, "speaker": "GPT",
            "tokens_in": tin, "tokens_out": tout,
            "content": gpt_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        save_iteration(1, "GPT", gpt_text, transcript, total_in, total_out)
        print(f"GPT iteration 1: {tin:,} in / {tout:,} out")

    # Load previous iterations from disk
    iter3_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "refactor_proposal_iteration3.json"
    if iter3_path.exists():
        # Replay conversation history
        messages.append({"role": "user", "content": CLAUDE_ITERATION_2})
        transcript.append({"iteration": 2, "speaker": "Claude", "content": CLAUDE_ITERATION_2, "timestamp": "replayed"})

        iter3_content = iter3_path.read_text(encoding="utf-8")
        messages.append({"role": "assistant", "content": iter3_content})
        transcript.append({"iteration": 3, "speaker": "GPT", "content": iter3_content, "timestamp": "replayed"})
        print("Loaded iterations 2-3 from disk.")

    CLAUDE_ITERATION_4 = """## Claude's Response to Iteration 3

Strong revision. I accept your changes on Provider Detail placement, facade design,
emergency keyword configuration, and prompt consistency rule. We are converging.

Four gaps remain before I can sign off:

### 1. MIGRATION PLAN (missing)
You proposed the target state but no phased migration. We cannot refactor 1,712 lines
in one commit. Propose a phased plan:
- Which files/classes move first?
- What is the minimum viable first phase that compiles and runs?
- How do we avoid a broken intermediate state?
- How many phases and what is the risk per phase?

### 2. FRONTEND SCOPE (incomplete)
You said "FrontendOnly" for markdown rendering but the React frontend is in scope.
The frontend has: API client, chat components, state management, markdown rendering,
emergency UI. Propose the frontend file structure. Does it mirror the backend domains
(find_care/, evaluate_care/) or stay flat?

### 3. TESTING STRATEGY (missing)
Each service class needs tests. Propose:
- Test file structure (mirrors source?)
- Which tests are mocked vs integration?
- Do we keep existing tests or rewrite?
- How do we ensure no regression during migration?

### 4. MAIN.PY AFTER REFACTOR
What remains in main.py? Is it eliminated? Renamed? Becomes app.py?
Be explicit about the final state of the entry point.

Also one correction: your iteration counter says "iteration: 2" but this is iteration 3
in our sequence. Please fix in your next response.

Please revise with these 4 additions. If you address all 4 satisfactorily,
I will call for final sign-off. Return JSON."""

    print("\nIteration 4/100 -- Claude feedback (4 remaining gaps)...")
    messages.append({"role": "user", "content": CLAUDE_ITERATION_4})
    transcript.append({
        "iteration": 4, "speaker": "Claude",
        "content": CLAUDE_ITERATION_4,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Iteration 5: GPT revises
    print("Iteration 5/100 -- GPT revising (final gaps)...")
    gpt_text, tin, tout = call_gpt(messages)
    total_in += tin
    total_out += tout
    messages.append({"role": "assistant", "content": gpt_text})
    transcript.append({
        "iteration": 5, "speaker": "GPT",
        "tokens_in": tin, "tokens_out": tout,
        "content": gpt_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    save_iteration(5, "GPT", gpt_text, transcript, total_in, total_out)
    print(f"GPT iteration 5: {tin:,} in / {tout:,} out")
    print(f"Preview: {gpt_text[:600]}...")

    # Save full transcript
    save_iteration(5, "transcript", "", transcript, total_in, total_out)

    print(f"\n{'='*70}")
    print(f"Iteration 5 complete. Total tokens: {total_in:,} in / {total_out:,} out")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
