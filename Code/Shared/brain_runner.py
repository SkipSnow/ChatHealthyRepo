"""
Brain Runner — drives the Claude ↔ GPT loop via OpenAI API.

Removes Boss as relay for the Claude ↔ GPT exchange.
Boss still approves High+ risk gates.

Usage:
    ENV_PREFIX=dev python brain_runner.py

Flow:
    1. Reads pending GPT assignments from brain/assignment_queue.json
    2. Fetches repo context files
    3. Calls GPT-4o with full context
    4. Parses Assurance Output JSON from response
    5. Commits to brain/assurance_results.json
    6. Logs usage to brain/usage_log.json via cost_guard
    7. Prints gate decision — Boss acts if High+

ADR: ADR-0007, MB-0001 (Boss Governance)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from cost_guard import log_usage, check_budget
from brain_loop import list_assignments, read_assurance_results
from machine_brain import semantic_search, get_decisions

_REPO_ROOT  = Path(__file__).parent.parent.parent
_BRAIN_DIR  = _REPO_ROOT / "brain"
_BASE_URL   = "https://raw.githubusercontent.com/SkipSnow/ChatHealthyRepo/dev/"

CONTEXT_FILES = [
    "brain/budget_config.json",
    "Code/Shared/brain_loop.py",
    "Code/Shared/brain_auth.py",
    "Code/Shared/cost_guard.py",
    "docs/machine-brain-claude-spec.md",
]


def _fetch_context() -> str:
    """Fetch repo context files for GPT's session."""
    import urllib.request
    parts = []
    for path in CONTEXT_FILES:
        url = _BASE_URL + path
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                content = r.read().decode("utf-8")
            parts.append(f"=== {path} ===\n{content}\n")
        except Exception as e:
            parts.append(f"=== {path} === [fetch failed: {e}]\n")
    return "\n".join(parts)


def _fetch_machine_brain_context(query: str) -> tuple[str, list[dict]]:
    """
    Query Machine Brain for records relevant to the assignment.
    Returns (formatted_text, raw_records).
    """
    # machine_brain.py reads MONGODB_CONNECTION_STRING; bridge to the frontend cluster key.
    if not os.getenv("MONGODB_CONNECTION_STRING"):
        frontend_conn = os.getenv("MONGO_FRONTEND_connectionString")
        if frontend_conn:
            os.environ["MONGODB_CONNECTION_STRING"] = frontend_conn
    try:
        records = semantic_search(query, top_k=10)
        # semantic_search() may return [] when Voyage is unavailable and the keyword
        # fallback finds no match on a long query string. In that case, load all records.
        if not records:
            records = get_decisions("")[:10]
    except Exception as e:
        print(f"[BrainRunner] WARNING: Machine Brain unavailable — {e}")
        return "(Machine Brain unavailable — proceeding without MB context.)", []

    if not records:
        return "(No Machine Brain records matched this query.)", []

    parts = []
    for r in records:
        score = r.get("score")
        score_str = f" score={score:.3f}" if isinstance(score, float) else ""
        record_id = r.get("adr_id") or r.get("mb_id", "?")
        narrative = r.get("narrative", "")
        narrative_snippet = (narrative[:400] + "…") if len(narrative) > 400 else narrative
        parts.append(
            f"[{record_id}] {r.get('topic', '?')}{score_str}\n"
            f"  Decision: {r.get('decision', '')}\n"
            f"  Rationale: {r.get('rationale', '')}\n"
            f"  Constraints: {r.get('constraints', '')}"
            + (f"\n  Narrative: {narrative_snippet}" if narrative_snippet else "")
        )
    return "\n\n".join(parts), records


SYSTEM_PROMPT = """You are the Enterprise Architect for ChatHealthy, operating under framework_02.
Your role: architectural assurance, UAT generation, risk classification, gate recommendation.
You do not write code. You design, validate, and approve.
You are precise, thorough, and never truncate required output."""


def _build_prompt(assignment: dict, context: str, mb_context: str) -> str:
    aid = assignment['assignment_id']
    uat_rule = assignment.get('uat_requirement', '')
    feature_context = assignment.get('feature_context', '')
    feature_block = (
        f"\nPRODUCT REQUIREMENTS (provided by Boss — use these as the source of truth for planning):\n{feature_context}\n"
        if feature_context else
        "\nPRODUCT REQUIREMENTS: None provided. Derive requirements from the repo context files and code.\n"
    )
    return f"""ASSIGNMENT {aid}
Title: {assignment['title']}
Priority: {assignment['priority']} | Estimated risk: {assignment['estimated_risk']}

TASK:
{assignment['description']}
{feature_block}
UAT RULES:
{uat_rule}

MACHINE BRAIN CONTEXT (architectural decisions relevant to this assignment — read these first):
{mb_context}

REPO CONTEXT (read before responding):
{context}

---
JSON SCHEMA — every field marked REQUIRED must be present and non-empty:

{{
  "review_id":           REQUIRED string = "{aid}",
  "assignment_id":       REQUIRED string = "{aid}",
  "timestamp":           REQUIRED string ISO 8601 UTC,
  "architecture_status": REQUIRED "pass" or "fail",
  "behavior_status":     REQUIRED "pass" or "fail",
  "risk":                REQUIRED one of: Low | Moderate | High | Critical | Suicidal,
  "issues":              REQUIRED array (empty if none),
  "gate_recommendation": REQUIRED one of: auto | proceed_with_warning | escalate | block_escalate | block_boss_required,
  "notes":               REQUIRED string,

  "requirements": REQUIRED array — derive product requirements from the code and context provided. Each requirement is a discrete, testable statement of what the system must do. MINIMUM 5 items. These become the permanent product record in Machine Brain, each {{
    "req_id":      REQUIRED string e.g. REQ-001,
    "feature":     REQUIRED string — which feature this belongs to,
    "requirement": REQUIRED string — specific, testable, written as 'The system must...',
    "source":      REQUIRED string — where you found this: 'code: <function_name>', 'assignment', or 'architecture constraint',
    "priority":    REQUIRED string: must-have | should-have | nice-to-have
  }},

  "feature_set": REQUIRED {{
    "ships_tuesday": REQUIRED array, MINIMUM 1 item, each {{
      "feature":     REQUIRED string,
      "description": REQUIRED string — specific, not generic,
      "risk":        REQUIRED Low|Moderate|High
    }},
    "deferred": REQUIRED array, each {{
      "feature": REQUIRED string,
      "reason":  REQUIRED string — specific reason
    }}
  }},

  "test_schedule": REQUIRED array, MINIMUM 3 phases, each {{
    "phase":       REQUIRED string,
    "timing":      REQUIRED string with date and time,
    "owner":       REQUIRED Claude|GPT|Boss,
    "description": REQUIRED string — what specifically is tested
  }},

  "acceptance_criteria": REQUIRED array, MINIMUM 3 per feature in ships_tuesday — cover distinct behavioral dimensions (e.g. happy path execution, error handling, budget enforcement, gate routing, schema conformance), each {{
    "criterion_id": REQUIRED string e.g. AC-001,
    "feature":      REQUIRED string matching a ships_tuesday feature,
    "criterion":    REQUIRED string — specific, measurable, unambiguous pass condition (not generic),
    "risk":         REQUIRED Low|Moderate|High
  }},

  "e2e_flows": REQUIRED array, MINIMUM 3 flows — one per major actor path, each {{
    "flow_id":        REQUIRED string e.g. E2E-001,
    "title":          REQUIRED string,
    "actor":          REQUIRED string who initiates the flow,
    "steps":          REQUIRED array MINIMUM 3 steps,
    "pass_condition": REQUIRED string — exact observable boolean true condition,
    "fail_condition": REQUIRED string — exact observable boolean false condition
  }},

  "uat_scenarios": REQUIRED array, MINIMUM 1 scenario per acceptance criterion — if the feature has 3 ACs you must produce 3+ scenarios, each {{
    "scenario_id":          REQUIRED string e.g. UAT-001,
    "description":          REQUIRED string,
    "component":            REQUIRED string,
    "scenario_type":        REQUIRED "llm" or "procedural",
    "acceptance_criterion": REQUIRED string matching a criterion_id,
    "test_cases": REQUIRED array — if llm: EXACTLY 10 semantically different items; if procedural: MINIMUM 3 items covering all happy paths and MINIMUM 2 exception paths, each {{
      "case_id":  REQUIRED string e.g. UAT-001-TC-01,
      "input":    REQUIRED string — exact input, action, or state,
      "expected": REQUIRED boolean true or false,
      "rationale": REQUIRED string — why this specific input produces this specific result
    }}
  }},

  "usage": REQUIRED {{
    "agent":         REQUIRED "GPT",
    "model":         REQUIRED "gpt-4o",
    "tokens_in":     REQUIRED integer — estimate if actual unavailable,
    "tokens_out":    REQUIRED integer — estimate if actual unavailable,
    "assignment_id": REQUIRED "{aid}"
  }},

  "machine_brain_context_used": REQUIRED array — list every Machine Brain record you read, each {{
    "mb_id":   REQUIRED string — the record ID from the MACHINE BRAIN CONTEXT block (e.g. MB-0003 or ADR-0001),
    "topic":   REQUIRED string — exact topic string from the record,
    "applied": REQUIRED string — one sentence: how this record influenced your output
  }}
}}

ABSOLUTE RULES:
1. Every field marked REQUIRED must be present — no omissions
2. requirements array must have MINIMUM 5 items — derive from code if not provided
3. Every acceptance criterion must have at least one UAT scenario — 3 ACs = minimum 3 scenarios
4. LLM scenarios must have exactly 10 test_cases — not 9, not 11
5. Procedural scenarios must have minimum 3 test_cases: all happy paths + minimum 2 exceptions
6. All test_cases.expected are boolean true or false — no strings, no nulls
7. Do NOT include gpt_api_key, bearer_token, or any secret field
8. Return raw JSON only — no markdown, no code fences, no explanation text
9. machine_brain_context_used must list every MB record from MACHINE BRAIN CONTEXT you considered
10. Thin outputs are rejected. Complete professional output required.
"""


def _call_gpt(prompt: str) -> tuple[str, int, int]:
    """Call GPT-4o. Returns (response_text, tokens_in, tokens_out)."""
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content
    tokens_in  = response.usage.prompt_tokens
    tokens_out = response.usage.completion_tokens
    return text, tokens_in, tokens_out


def _commit_assurance(result: dict) -> None:
    path = _BRAIN_DIR / "assurance_results.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    # Strip any accidentally-included key fields
    result.pop("gpt_api_key", None)
    result.pop("bearer_token", None)
    data["results"].append(result)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run(assignment_id: str = None) -> dict:
    """
    Run the GPT assurance loop for a pending assignment.

    Args:
        assignment_id: Specific assignment, or None for highest-priority pending GPT assignment.

    Returns:
        The Assurance Output dict.
    """
    # Find the assignment
    pending = [a for a in list_assignments("pending") if a.get("assigned_to") == "GPT"]
    if not pending:
        print("[BrainRunner] No pending GPT assignments.")
        return {}

    if assignment_id:
        matches = [a for a in pending if a["assignment_id"] == assignment_id]
        if not matches:
            print(f"[BrainRunner] Assignment {assignment_id} not found or not pending for GPT.")
            return {}
        assignment = matches[0]
    else:
        priority_order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        assignment = sorted(pending, key=lambda a: priority_order.get(a["priority"], 2))[0]

    aid = assignment["assignment_id"]
    print(f"[BrainRunner] Running assignment: {aid} — {assignment['title']}")

    # Budget check
    budget = check_budget(aid)
    if not budget["ok"]:
        print(f"[BrainRunner] BUDGET BLOCKED: {budget['reason']}")
        return {}

    print(f"[BrainRunner] Budget OK — ${budget['per_assignment']['remaining']:.2f} remaining")

    # Query Machine Brain for relevant architectural context
    mb_query = f"{assignment['title']} {assignment.get('description', '')}"[:500]
    print("[BrainRunner] Querying Machine Brain...")
    mb_context, mb_records = _fetch_machine_brain_context(mb_query)
    print(f"[BrainRunner] Machine Brain: {len(mb_records)} records retrieved")

    print("[BrainRunner] Fetching repo context...")
    context = _fetch_context()

    print("[BrainRunner] Calling GPT-4o...")
    prompt = _build_prompt(assignment, context, mb_context)
    response_text, tokens_in, tokens_out = _call_gpt(prompt)

    # Log usage
    cost = log_usage(
        agent="GPT",
        model="gpt-4o",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        assignment_id=aid,
        call_type="review",
    )
    print(f"[BrainRunner] GPT call complete — {tokens_in} in / {tokens_out} out / ${cost:.4f}")

    # Parse response
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        print(f"[BrainRunner] ERROR: GPT response is not valid JSON — {e}")
        print(response_text[:500])
        return {}

    # Ensure required fields
    result.setdefault("review_id", aid)
    result.setdefault("assignment_id", aid)
    result.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

    # Commit to assurance_results.json
    _commit_assurance(result)
    print(f"[BrainRunner] Assurance Output committed.")

    # Gate decision
    risk = result.get("risk", "High")
    gate = result.get("gate_recommendation", "escalate")
    arch = result.get("architecture_status", "unknown")
    behav = result.get("behavior_status", "unknown")
    issues = result.get("issues", [])
    scenarios = result.get("uat_scenarios", [])
    requirements = result.get("requirements", [])
    mb_used = result.get("machine_brain_context_used", [])
    feature_set = result.get("feature_set", {})
    ships = feature_set.get("ships_tuesday", [])
    deferred = feature_set.get("deferred", [])
    criteria = result.get("acceptance_criteria", [])
    e2e = result.get("e2e_flows", [])

    print(f"\n{'='*60}")
    print(f"  Assignment:       {aid}")
    print(f"  Risk:             {risk}")
    print(f"  Gate:             {gate}")
    print(f"  Arch status:      {arch}")
    print(f"  Behavior:         {behav}")
    print(f"  Issues:           {len(issues)}")
    print(f"  Ships Tuesday:    {len(ships)} features")
    print(f"  Deferred:         {len(deferred)} features")
    print(f"  Accept. criteria: {len(criteria)}")
    print(f"  E2E flows:        {len(e2e)}")
    print(f"  UAT scenarios:    {len(scenarios)}")
    print(f"  Requirements:     {len(requirements)}")
    print(f"  MB records used:  {len(mb_used)}")
    print(f"{'='*60}")

    if mb_used:
        print("\nMachine Brain records GPT read:")
        for r in mb_used:
            rid = r.get('mb_id') or r.get('record_id', '?')
            print(f"  {rid} — {r.get('topic','?')}")
            print(f"    Applied: {r.get('applied','')}")

    if issues:
        print("\nIssues flagged by GPT:")
        for i, issue in enumerate(issues, 1):
            print(f"  {i}. {issue}")

    if gate in ("block_escalate", "block_boss_required"):
        print(f"\n[BrainRunner] BLOCKED — Boss sign-off required before Claude proceeds.")
        print(f"[BrainRunner] Gate: {gate} | Risk: {risk}")
    elif gate == "escalate":
        print(f"\n[BrainRunner] ESCALATED — Boss notification required. Risk: {risk}")
    elif gate == "proceed_with_warning":
        print(f"\n[BrainRunner] WARNING — Moderate risk. Claude may proceed with caution.")
    else:
        print(f"\n[BrainRunner] AUTO — Claude may proceed.")

    return result


if __name__ == "__main__":
    aid = sys.argv[1] if len(sys.argv) > 1 else None
    run(aid)
