# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_biz_arch_collab.py — Claude + GPT overnight collaboration.
# Produce investor-grade business architecture for 3 components.
#
# Usage: python brain_biz_arch_collab.py

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
BRAIN_DIR = PROJECT_ROOT / "brain"
MAX_ITERATIONS = 150

# Load the Discuss Care component doc
discuss_care = json.loads((BRAIN_DIR / "machine_artifacts" / "document_type_json" / "component_discuss_care.json").read_text(encoding="utf-8"))

# Load external audit
audit = json.loads((BRAIN_DIR / "machine_artifacts" / "document_type_json" / "external_audit_v1.json").read_text(encoding="utf-8"))

SYSTEM_PROMPT = json.dumps({
    "role": "Chief Business Architect and Investor Communications Strategist",
    "identity": "GPT",
    "background": "30 years enterprise architecture, healthcare domain expert, investor pitch experience",
    "project": "ChatHealthy.ai — Business Architecture Document (Investor Artifact)",
    "rules": [
        "Think DEEPLY. Review your work TWICE before responding. Check consistency across every section.",
        "You are producing an INVESTOR-GRADE business architecture document for ChatHealthy.ai.",
        "This document will be shown to investors, partners, and patent attorneys.",
        "You are collaborating with Claude (engineer). Claude responds to each output. Goal: agreed document in fewer than 150 iterations.",
        "The document must be comprehensive, visually clear, and tell a compelling story.",
        "Three business components are FIXED — not negotiable: Find Care, Evaluate Care, Discuss Care.",
        "The revenue model, moat, patent concept, and governance are defined by Boss. Do not change them. Elaborate and strengthen them.",
        "Output must be structured JSON that Claude can render into PDF and Word with diagrams.",
        "DIAGRAMS: Describe each diagram precisely so Claude can generate it. Component diagram, value chain, revenue flow, participant model, care decision lifecycle.",
        "Be careful with language. This is a real company. Real investors. Real patients.",
        "Do NOT use hype words. Let the architecture speak for itself.",
    ],
    "governance_policies": [
        "GOV-001: No care-chain revenue. AI vendors = technology partners. Apple precedent.",
        "GOV-002: Single-tenant architecture. Each session isolated.",
        "GOV-004: AI suggests. Humans decide. Always.",
    ],
    "max_think_seconds": 300,
}, indent=2, default=str)

USER_PROMPT = f"""# ASSIGNMENT: ChatHealthy.ai Business Architecture — Investor Artifact

Produce a comprehensive business architecture document covering all three components.
Claude will render your output into JSON + PDF + Word with diagrams.

## THE THREE COMPONENTS

### 1. Find Care
- Help users find healthcare providers, specialties, navigate the system
- Vector search + taxonomy matching across DE, MS, VA (expanding to all states)
- Free forever. This is the funnel.

### 2. Evaluate Care
- Help users evaluate care options: clinical trials, provider credentials, quality metrics
- NPI Registry, ClinicalTrials.gov, state medical boards, quality signals
- Free now. Paywall when trusted brand established.
- PATENT CONCEPT: Method for synthesizing healthcare provider quality signals using LLMs

### 3. Discuss Care
- Multi-participant AI-assisted healthcare collaboration
- Consumers invite family, friends, caregivers into shared session
- Multiple AI models participate (Choose Your AI)
- Floor control: one speaker at a time, queue, humans take precedence, bot moderator
- Voice input, camera, avatars for bots
- Premium token markup on AI usage
- AI vendors pay for distribution in healthcare decisions

## DISCUSS CARE FULL SPECIFICATION
{json.dumps(discuss_care, indent=2, default=str)}

## EXTERNAL AUDIT SUMMARY (Perplexity, March 2026)
Executive summary: {audit.get('executive_summary', '')}
Strengths identified: {', '.join(f['title'] for f in audit.get('findings', []) if f.get('severity') == 'strength')}

## REVENUE MODEL
1. Find Care: FREE forever (funnel)
2. Evaluate Care: FREE now, paywall post-trust (premium quality data)
3. Discuss Care: Premium token markup + AI vendor partnerships (Choose Your AI)
4. AI vendors pay for distribution — NOT care-chain revenue (GOV-001 clean, Apple precedent)

## MOAT
- Regulatory willingness: AI vendors avoid FDA/HIPAA. They NEED someone else to own the room.
- Trust: consumers chose ChatHealthy for care decisions
- Data: provider + trials + quality signals, takes years
- Governance: multi-AI safety framework (floor control, consent, emergency detection)
- Network: every session adds participants + signal
- Patent: LLM quality signal synthesis method

## VALUE CHAIN
Find (discover) -> Evaluate (assess) -> Discuss (decide together)
Solo user -> Solo user -> Multi-user + multi-AI

## TARGET TIMELINE
- April 2026: Quality month (harden Find Care + Evaluate Care)
- April 30: Alpha launch (M-002)
- May 2026: Beta Sprint 1 — Discuss Care first deliverable
- July 31: Seed funding close (M-004)
- August 25: Production launch (M-005)
- September 20: Beta launch (M-003)

## REQUIRED OUTPUT

Return a SINGLE JSON object with this structure. Be thorough.

{{
  "title": "ChatHealthy.ai — Business Architecture",
  "subtitle": "Find Care. Evaluate Care. Discuss Care.",
  "version": "1.0",
  "date": "2026-03-31",
  "executive_summary": "2-3 paragraphs for investors",
  "mission": "one sentence",
  "components": {{
    "find_care": {{
      "name": "Find Care",
      "tagline": "short",
      "description": "detailed",
      "capabilities": ["list"],
      "data_sources": ["list"],
      "pricing": "free forever",
      "role_in_value_chain": "the funnel"
    }},
    "evaluate_care": {{
      "name": "Evaluate Care",
      "tagline": "short",
      "description": "detailed",
      "capabilities": ["list"],
      "data_sources": ["list"],
      "pricing": "free now, paywall post-trust",
      "patent_concept": {{...}},
      "role_in_value_chain": "trust builder"
    }},
    "discuss_care": {{
      "name": "Discuss Care",
      "tagline": "short",
      "description": "detailed",
      "features": [DC-01 through DC-13],
      "floor_control": {{...}},
      "choose_your_ai": {{...}},
      "pricing": "premium tokens + vendor partnerships",
      "role_in_value_chain": "revenue engine"
    }}
  }},
  "value_chain": {{
    "flow": "Find -> Evaluate -> Discuss",
    "description": "how the three components connect"
  }},
  "revenue_model": {{
    "streams": [list of revenue streams with details],
    "gov_001_compliance": "why this is clean"
  }},
  "moat": {{
    "barriers": [list with explanations]
  }},
  "governance": {{
    "policies": [list],
    "safety": "how safety works across all components"
  }},
  "timeline": {{
    "milestones": [list with dates]
  }},
  "diagrams": {{
    "component_diagram": {{
      "description": "precise layout description for rendering",
      "elements": [list of boxes, arrows, labels]
    }},
    "value_chain_diagram": {{...}},
    "revenue_flow_diagram": {{...}},
    "participant_model_diagram": {{...}},
    "care_decision_lifecycle": {{...}}
  }},
  "competitive_landscape": "who else is in this space and why they can't do what we do",
  "investment_thesis": "why an investor should fund this",
  "sign_off": "..."
}}

Begin. Return JSON. Think deeply. Double-check consistency."""


def call_gpt(messages: list) -> tuple:
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
    print("Business Architecture — Overnight Claude + GPT Collaboration")
    print(f"Model: gpt-5.3-chat-latest  |  Max iterations: {MAX_ITERATIONS}")
    print("=" * 70)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]

    total_in = total_out = 0
    transcript = []

    # Iteration 1: GPT drafts
    print("\nIteration 1 — GPT drafting business architecture...")
    gpt_text, tin, tout = call_gpt(messages)
    total_in += tin
    total_out += tout
    print(f"GPT: {tin:,} in / {tout:,} out")

    transcript.append({
        "iteration": 1, "speaker": "GPT",
        "tokens_in": tin, "tokens_out": tout,
        "content": gpt_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Save GPT draft
    draft_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "biz_arch_draft_v1.json"
    draft_path.write_text(gpt_text if gpt_text.strip().startswith("{") else json.dumps({"raw": gpt_text}, default=str), encoding="utf-8")
    print(f"Draft saved: {draft_path}")

    # Claude reviews and responds
    claude_review = """## Claude Review of Business Architecture Draft

    I've reviewed your draft. Here are my feedback items:

    1. CONSISTENCY CHECK: Verify the Discuss Care features match DC-01 through DC-13 exactly as Boss defined them. Missing any is a failure.

    2. FLOOR CONTROL (DC-11): Must include precedence rules — humans default above bots, demotion on time-wasting, restoration on contribution. Bot moderator nudges, never forces (GOV-004).

    3. PRESENCE (DC-12): Bots must have avatars (brand image, NOT human face). Humans may use still image or live camera + microphone.

    4. VOICE (DC-13): Speech-to-text mode. Same floor rules apply. Accessibility is the mission — "Mom is tired. Daughter is driving."

    5. PATENT: The claim must be precise: "Method for synthesizing healthcare provider quality signals using large language models to produce consumer-comprehensible provider assessments." List all 9 signal types.

    6. MOAT: The key insight is regulatory willingness. AI vendors avoid FDA classification and HIPAA compliance like the plague. They NEED someone else to own the healthcare room. ChatHealthy is that someone.

    7. APPLE PRECEDENT: The pricing model follows Apple's platform model. Both sides pay. Good lawyers. Not a conflict — a marketplace.

    8. DIAGRAMS: Each diagram description must be precise enough for me to render programmatically. Specify: boxes, labels, arrows, colors, layout direction.

    9. COMPETITIVE LANDSCAPE: Nobody has multi-human multi-AI collaboration in healthcare with shared tools, floor control, consent framework, and governance. It doesn't exist.

    10. INVESTMENT THESIS: The pitch is NOT "we built a chatbot." The pitch is "we own the healthcare decision room and AI companies pay to be in it."

    Please revise your draft addressing all 10 points. Return the complete revised JSON.
    Think deeply. Check every section for consistency with the others."""

    messages.append({"role": "assistant", "content": gpt_text})
    messages.append({"role": "user", "content": claude_review})
    transcript.append({
        "iteration": 2, "speaker": "Claude",
        "content": claude_review,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Iteration 3: GPT revises
    print("\nIteration 3 — GPT revising based on Claude feedback...")
    gpt_text, tin, tout = call_gpt(messages)
    total_in += tin
    total_out += tout
    print(f"GPT: {tin:,} in / {tout:,} out")

    transcript.append({
        "iteration": 3, "speaker": "GPT",
        "tokens_in": tin, "tokens_out": tout,
        "content": gpt_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Save revised draft
    final_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "biz_arch_investor.json"
    final_path.write_text(gpt_text if gpt_text.strip().startswith("{") else json.dumps({"raw": gpt_text}, default=str), encoding="utf-8")
    print(f"Revised draft saved: {final_path}")

    # Save transcript
    transcript_path = BRAIN_DIR / "machine_artifacts" / "document_type_json" / "biz_arch_collab_transcript.json"
    transcript_data = {
        "title": "Business Architecture Collaboration Transcript",
        "model": "gpt-5.3-chat-latest",
        "iterations": 3,
        "total_tokens": {"input": total_in, "output": total_out},
        "transcript": transcript,
    }
    transcript_path.write_text(json.dumps(transcript_data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"Complete. {total_in:,} in / {total_out:,} out")
    print(f"Draft: {final_path}")
    print(f"{'='*70}")

    return gpt_text


if __name__ == "__main__":
    main()
