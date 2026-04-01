# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

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


def _fetch_context(additional_files: list[str] = None) -> str:
    """Fetch repo context files for GPT's session.

    additional_files: extra paths (e.g. from assignment scope) to include
    beyond the base CONTEXT_FILES list. Duplicates are skipped.
    """
    import urllib.request
    seen = set(CONTEXT_FILES)
    files = list(CONTEXT_FILES)
    for path in (additional_files or []):
        if path not in seen and path.endswith((".py", ".ts", ".tsx", ".json", ".md")):
            seen.add(path)
            files.append(path)
    parts = []
    for path in files:
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


# System prompt loaded from brain: ai_operations.json -> gpt_assignment_system_prompt
# Static JSON — prompt cacheable. Dynamic parts go in user prompt only.
def _load_system_prompt() -> str:
    import json as _json
    try:
        from prompt_system_maker import PromptSystemMaker
        maker = PromptSystemMaker()
        record = maker.read_record("ai_operations", "gpt_assignment_system_prompt")
        if record and record.get("system_prompt"):
            sp = record["system_prompt"]
            return _json.dumps(sp) if isinstance(sp, dict) else str(sp)
    except Exception:
        pass
    return ("You are the Enterprise Architect for ChatHealthy.ai. "
            "You design, validate, and approve. You do not write code.")

SYSTEM_PROMPT = _load_system_prompt()


def _build_prompt(assignment: dict, context: str, mb_context: str) -> str:
    aid = assignment['assignment_id']
    uat_rule = assignment.get('uat_requirement', '')
    feature_context = assignment.get('feature_context', '')
    boss_scope = assignment.get('boss_scope', [])

    feature_block = (
        f"\nPRODUCT REQUIREMENTS (provided by Boss — use these as the source of truth for planning):\n{feature_context}\n"
        if feature_context else
        "\nPRODUCT REQUIREMENTS: None provided. Derive requirements from the repo context files and code.\n"
    )

    if boss_scope:
        scope_lines = "\n".join(f"  - {item}" for item in boss_scope)
        scope_block = (
            f"\nMANDATORY SCOPE (Boss directive — non-negotiable):\n{scope_lines}\n"
            f"These items MUST appear in ships_tuesday and scope_assessment. "
            f"You may assess their risk but you may NOT defer them. "
            f"If you see tradeoffs, put them in tradeoff_suggestions — clearly labeled, Boss decides.\n"
        )
    else:
        scope_block = ""

    from datetime import datetime, timezone, timedelta
    utc_now = datetime.now(timezone.utc)
    pst_now = utc_now.astimezone(timezone(timedelta(hours=-7)))
    now = f"{utc_now.strftime('%Y-%m-%dT%H:%M:%SZ')} (PST: {pst_now.strftime('%Y-%m-%d %I:%M %p')})"

    return f"""current_datetime: {now}

ASSIGNMENT {aid}
Title: {assignment['title']}
Priority: {assignment['priority']} | Estimated risk: {assignment['estimated_risk']}

TASK:
{assignment['description']}
{feature_block}{scope_block}
UAT RULES:
{uat_rule}

MACHINE BRAIN CONTEXT:
{mb_context}

REPO CONTEXT:
{context}

---
assignment_id: {aid}
Respond per the output_schema in the system prompt. Return raw JSON only.
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
    scope_files = assignment.get("scope", [])
    context = _fetch_context(additional_files=scope_files)

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
