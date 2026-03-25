"""
Brain Loop — ChatHealthy Binary Operating Model.

Implements the Claude ↔ GPT autonomous review loop defined in the
ChatHealthy Operational Model Specification (framework_02, 2026-03-25).

Flow:
  0. Boss writes assignment to brain/assignment_queue.json (write_assignment)
  1. Claude picks up assignment (pick_up_assignment) → reads from assignment_queue.json
  2. Claude completes implementation work
  3. Claude calls write_review_pack() → writes to brain/review_queue.json
  4. GPT reads review_queue.json, writes to brain/assurance_results.json
  5. Claude calls read_assurance_results() → gets UAT scenarios + gate decision
  6. Claude calls run_uat() → executes all UAT scenarios
  7. Claude calls close_review() → marks complete or escalates

Gate Rules (from spec):
  Low       → auto-proceed
  Moderate  → proceed with warning logged
  High      → escalate to Boss
  Critical  → block + escalate to Boss
  Suicidal  → block + Boss required in-session

Files (repo root /brain/):
  assignment_queue.json  — Boss writes assignments here
  review_queue.json      — Claude writes Review Packs here
  assurance_results.json — GPT writes Assurance Output here
  execution_state.json   — live loop state
  uat_library.json       — all UAT scenarios (become regression on pass)

ADR: ADR-0007 (Machine Brain), MB-0001 (Boss Governance), MB-0010 (Release Manifest)
"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent
_BRAIN_DIR = _REPO_ROOT / "brain"

_ASSIGNMENT_QUEUE = _BRAIN_DIR / "assignment_queue.json"
_REVIEW_QUEUE     = _BRAIN_DIR / "review_queue.json"
_ASSURANCE        = _BRAIN_DIR / "assurance_results.json"
_EXEC_STATE       = _BRAIN_DIR / "execution_state.json"
_UAT_LIBRARY      = _BRAIN_DIR / "uat_library.json"


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------

GATE_RULES = {
    "Low":      "auto",
    "Moderate": "proceed_with_warning",
    "High":     "escalate",
    "Critical": "block_escalate",
    "Suicidal": "block_boss_required",
}

AUTO_GATES   = {"auto", "proceed_with_warning"}
BLOCK_GATES  = {"block_escalate", "block_boss_required"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write(path: Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _set_state(review_id: Optional[str], status: str, escalation_reason: Optional[str] = None) -> None:
    state = _read(_EXEC_STATE)
    state["current_review_id"] = review_id
    state["loop_status"] = status
    state["last_updated"] = _now()
    if escalation_reason is not None:
        state["escalation_reason"] = escalation_reason
    _write(_EXEC_STATE, state)


def _machine_brain_context(summary: str) -> list[dict]:
    """Pull relevant Machine Brain records to include in the Review Pack."""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from machine_brain import semantic_search
        results = semantic_search(summary, top_k=5)
        # Strip embeddings — they are large and not useful in the review pack
        return [{k: v for k, v in r.items() if k != "embedding"} for r in results]
    except Exception as e:
        print(f"[BrainLoop] WARNING: machine_brain query failed — {e}", flush=True)
        return []


# ---------------------------------------------------------------------------
# Step 0 — Boss writes assignment
# ---------------------------------------------------------------------------

def write_assignment(
    title: str,
    description: str,
    scope: list[str],
    assigned_to: str = "Claude",
    from_agent: str = "Boss",
    priority: str = "normal",
    estimated_risk: str = "Moderate",
) -> str:
    """
    Any agent (Boss, Claude, or GPT) assigns work to another agent.

    Boss assigns via the IDE or ChatGPT web interface.
    Claude and GPT can assign each other — no manual relay required.
    The assigned agent delivers a result or requests feedback when blocked.

    Args:
        title:          Short task title
        description:    Full description of what needs to be done
        scope:          List of file paths or components in scope
        assigned_to:    "Claude" | "GPT" | "Boss" (default: Claude)
        from_agent:     "Boss" | "Claude" | "GPT" (default: Boss)
        priority:       "low" | "normal" | "high" | "critical"
        estimated_risk: Risk estimate — Low | Moderate | High | Critical | Suicidal

    Returns:
        assignment_id

    Examples:
        # Boss assigns Claude
        write_assignment("ENV_PREFIX sweep", "...", ["Code/DataPipelines/"],
                         assigned_to="Claude", from_agent="Boss")

        # Claude assigns GPT for architectural review
        write_assignment("Review FastAPI port design", "...", ["Code/ConversationalUX/"],
                         assigned_to="GPT", from_agent="Claude")

        # GPT assigns Claude to implement approved design
        write_assignment("Implement safety gate FastAPI", "...", ["Code/ConversationalUX/FindCareChat/"],
                         assigned_to="Claude", from_agent="GPT")
    """
    assignment_id = f"ASN-{uuid.uuid4().hex[:6].upper()}"
    assignment = {
        "assignment_id":  assignment_id,
        "timestamp":      _now(),
        "from":           from_agent,
        "assigned_to":    assigned_to,
        "title":          title,
        "description":    description,
        "scope":          scope,
        "priority":       priority,
        "estimated_risk": estimated_risk,
        "status":         "pending",
        "result":         None,
        "feedback_needed": False,
        "feedback_question": None,
    }
    queue = _read(_ASSIGNMENT_QUEUE)
    queue["assignments"].append(assignment)
    _write(_ASSIGNMENT_QUEUE, queue)
    print(f"[BrainLoop] Assignment written: {assignment_id} — {title}", flush=True)
    return assignment_id


def pick_up_assignment(assignment_id: Optional[str] = None) -> Optional[dict]:
    """
    Claude calls this to pick up work from Boss.

    If assignment_id is None, picks up the highest-priority pending assignment.
    Marks the assignment as 'in_progress'.

    Returns the assignment dict, or None if nothing is pending.

    Example:
        assignment = pick_up_assignment()
        if assignment:
            # read assignment["description"], assignment["scope"]
            # do the work
            # then write_review_pack(...)
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    pending = [a for a in queue["assignments"] if a["status"] == "pending"]
    if not pending:
        print("[BrainLoop] No pending assignments.", flush=True)
        return None

    priority_order = {"critical": 0, "high": 1, "normal": 2, "low": 3}

    if assignment_id:
        matches = [a for a in pending if a["assignment_id"] == assignment_id]
        if not matches:
            print(f"[BrainLoop] Assignment {assignment_id} not found or not pending.", flush=True)
            return None
        assignment = matches[0]
    else:
        assignment = sorted(pending, key=lambda a: priority_order.get(a["priority"], 2))[0]

    # Budget check before starting work
    try:
        from cost_guard import check_budget
        budget = check_budget(assignment["assignment_id"])
        if not budget["ok"]:
            print(f"[BrainLoop] BUDGET BLOCKED: {budget['reason']}", flush=True)
            assignment["status"] = "budget_blocked"
            assignment["budget_block_reason"] = budget["reason"]
            _write(_ASSIGNMENT_QUEUE, queue)
            _set_state(assignment["assignment_id"], "budget_blocked", budget["reason"])
            return None
    except ImportError:
        pass  # cost_guard not available — proceed without check

    assignment["status"] = "in_progress"
    assignment["picked_up_at"] = _now()
    _write(_ASSIGNMENT_QUEUE, queue)
    _set_state(assignment["assignment_id"], "in_progress")
    print(f"[BrainLoop] Assignment picked up: {assignment['assignment_id']} — {assignment['title']}", flush=True)
    return assignment


def list_assignments(status: Optional[str] = None) -> list[dict]:
    """
    Return assignments, optionally filtered by status.
    Status values: pending | in_progress | complete | escalated
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    assignments = queue.get("assignments", [])
    if status:
        assignments = [a for a in assignments if a.get("status") == status]
    return assignments


# ---------------------------------------------------------------------------
# Agent → Boss: deliver result or request feedback
# ---------------------------------------------------------------------------

def deliver_result(assignment_id: str, result: str) -> None:
    """
    Agent (Claude or GPT) calls this when work is complete.

    Marks the assignment complete and records the result summary.
    Boss can read brain/assignment_queue.json to see what was delivered.

    Args:
        assignment_id: The assignment being completed
        result:        Plain English summary of what was done and outcome
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    for a in queue["assignments"]:
        if a["assignment_id"] == assignment_id:
            a["status"] = "complete"
            a["completed_at"] = _now()
            a["result"] = result
            break
    _write(_ASSIGNMENT_QUEUE, queue)
    _set_state(None, "idle")
    print(f"[BrainLoop] Assignment {assignment_id} delivered.", flush=True)
    print(f"[BrainLoop] Result: {result}", flush=True)


def request_feedback(assignment_id: str, question: str) -> None:
    """
    Agent (Claude or GPT) calls this when they need Boss guidance to proceed.

    Pauses the assignment and surfaces the question to Boss.
    Boss answers in chat or in the IDE — agent reads the response and resumes.

    Args:
        assignment_id: The assignment requiring guidance
        question:      Specific question for Boss — what decision or info is needed
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    for a in queue["assignments"]:
        if a["assignment_id"] == assignment_id:
            a["feedback_needed"] = True
            a["feedback_question"] = question
            a["feedback_requested_at"] = _now()
            a["status"] = "awaiting_feedback"
            break
    _write(_ASSIGNMENT_QUEUE, queue)
    _set_state(assignment_id, "awaiting_feedback")
    print(f"[BrainLoop] Feedback requested on {assignment_id}:", flush=True)
    print(f"[BrainLoop] Question: {question}", flush=True)


def provide_feedback(assignment_id: str, answer: str) -> None:
    """
    Boss calls this (or it is called on Boss's behalf) to answer a feedback request.

    Resumes the assignment with the provided answer.

    Args:
        assignment_id: The assignment to resume
        answer:        Boss's answer to the agent's question
    """
    queue = _read(_ASSIGNMENT_QUEUE)
    for a in queue["assignments"]:
        if a["assignment_id"] == assignment_id:
            a["feedback_needed"] = False
            a["feedback_answer"] = answer
            a["feedback_answered_at"] = _now()
            a["status"] = "in_progress"
            break
    _write(_ASSIGNMENT_QUEUE, queue)
    _set_state(assignment_id, "in_progress")
    print(f"[BrainLoop] Feedback provided on {assignment_id} — agent resuming.", flush=True)


# ---------------------------------------------------------------------------
# Step 1 — Claude writes Review Pack
# ---------------------------------------------------------------------------

def write_review_pack(
    summary: str,
    files: list[dict],
    expected_behavior: str,
    declared_risk: str,
) -> str:
    """
    Claude calls this after completing implementation work.

    Args:
        summary:           What changed and why (plain English, 2–5 sentences)
        files:             List of {"path": str, "content": str} for all changed files
        expected_behavior: What the system should do after this change
        declared_risk:     Low | Moderate | High | Critical | Suicidal

    Returns:
        review_id — use this to retrieve assurance results later

    Example:
        review_id = write_review_pack(
            summary="Added ENV_PREFIX routing to all DataPipelines database references.",
            files=[{"path": "Code/DataPipelines/county_enrichment_job.py", "content": open(...).read()}],
            expected_behavior="All DataPipelines jobs read from {ENV_PREFIX}_PublicHealthData.",
            declared_risk="Moderate",
        )
    """
    valid_risks = {"Low", "Moderate", "High", "Critical", "Suicidal"}
    if declared_risk not in valid_risks:
        raise ValueError(f"declared_risk must be one of {valid_risks}")

    review_id = f"REV-{uuid.uuid4().hex[:8].upper()}"

    pack = {
        "review_id":           review_id,
        "timestamp":           _now(),
        "summary":             summary,
        "files":               files,
        "machine_brain_context": _machine_brain_context(summary),
        "expected_behavior":   expected_behavior,
        "declared_risk":       declared_risk,
        "status":              "pending",
    }

    queue = _read(_REVIEW_QUEUE)
    queue["reviews"].append(pack)
    _write(_REVIEW_QUEUE, queue)

    _set_state(review_id, "awaiting_gpt_review")
    print(f"[BrainLoop] Review Pack written: {review_id} (risk: {declared_risk})", flush=True)
    print(f"[BrainLoop] Waiting for GPT assurance on brain/assurance_results.json", flush=True)
    return review_id


# ---------------------------------------------------------------------------
# Step 4 — Claude reads GPT's Assurance Output
# ---------------------------------------------------------------------------

def read_assurance_results(review_id: str) -> Optional[dict]:
    """
    Claude calls this to retrieve GPT's assurance output for a review.

    Returns None if GPT has not yet written results for this review_id.
    Returns the assurance result dict if available.

    Assurance result fields:
        architecture_status:  pass | fail
        behavior_status:      pass | fail
        risk:                 Low | Moderate | High | Critical | Suicidal
        issues:               list of issue descriptions
        uat_scenarios:        list of UAT scenario dicts
        gate_recommendation:  auto | proceed_with_warning | escalate |
                              block_escalate | block_boss_required
    """
    data = _read(_ASSURANCE)
    for result in data.get("results", []):
        if result.get("review_id") == review_id:
            risk = result.get("risk", "High")
            gate = result.get("gate_recommendation", GATE_RULES.get(risk, "escalate"))

            if gate in BLOCK_GATES:
                reason = f"Gate '{gate}' for review {review_id} — risk: {risk}"
                _set_state(review_id, "blocked", reason)
                print(f"[BrainLoop] BLOCKED: {reason}", flush=True)
                if gate == "block_boss_required":
                    print(f"[BrainLoop] Boss sign-off required before proceeding.", flush=True)
            elif gate == "proceed_with_warning":
                _set_state(review_id, "testing")
                print(f"[BrainLoop] WARNING: Moderate risk — proceeding with caution.", flush=True)
            elif gate == "escalate":
                reason = f"High risk escalation for review {review_id}"
                _set_state(review_id, "escalated", reason)
                print(f"[BrainLoop] ESCALATED to Boss: {reason}", flush=True)
            else:
                _set_state(review_id, "testing")

            return result

    return None


# ---------------------------------------------------------------------------
# Step 5 — Claude runs UAT
# ---------------------------------------------------------------------------

def run_uat(review_id: str, runner: callable) -> dict:
    """
    Execute all UAT scenarios for a review and persist results.

    Args:
        review_id: The review being tested
        runner:    callable(scenario: dict) -> bool
                   Your test function. Return True = pass, False = fail.

    Returns:
        {"passed": int, "failed": int, "scenarios": list}

    Example:
        def my_runner(scenario):
            # implement scenario["steps"], return True/False
            ...

        results = run_uat("REV-ABC123", my_runner)
    """
    result = read_assurance_results(review_id)
    if result is None:
        raise RuntimeError(f"No assurance results found for {review_id}. GPT has not reviewed yet.")

    scenarios = result.get("uat_scenarios", [])
    if not scenarios:
        print(f"[BrainLoop] No UAT scenarios for {review_id} — nothing to run.", flush=True)
        return {"passed": 0, "failed": 0, "scenarios": []}

    library = _read(_UAT_LIBRARY)
    state = _read(_EXEC_STATE)
    passed = 0
    failed = 0
    run_results = []

    for s in scenarios:
        scenario_id = s.get("scenario_id", f"UAT-{uuid.uuid4().hex[:6].upper()}")
        try:
            ok = runner(s)
        except Exception as e:
            print(f"[BrainLoop] UAT runner raised exception on {scenario_id}: {e}", flush=True)
            ok = False

        status = "pass" if ok else "fail"
        if ok:
            passed += 1
        else:
            failed += 1

        record = {
            **s,
            "scenario_id": scenario_id,
            "review_id":   review_id,
            "status":      status,
            "last_run":    _now(),
        }
        run_results.append(record)

        # Upsert into UAT library (all UAT becomes regression)
        existing_ids = [sc["scenario_id"] for sc in library["scenarios"]]
        if scenario_id in existing_ids:
            library["scenarios"] = [
                record if sc["scenario_id"] == scenario_id else sc
                for sc in library["scenarios"]
            ]
        else:
            library["scenarios"].append(record)

        print(f"  [{status.upper()}] {scenario_id}: {s.get('description', '')}", flush=True)

    _write(_UAT_LIBRARY, library)

    # Update execution state counters
    state["pass_count"] += passed
    state["fail_count"] += failed
    state["last_updated"] = _now()
    _write(_EXEC_STATE, state)

    if failed > 0:
        _set_state(review_id, "fixing")
        print(f"[BrainLoop] {failed} UAT scenario(s) failed — fix and re-run.", flush=True)
    else:
        _set_state(review_id, "complete")
        print(f"[BrainLoop] All {passed} UAT scenario(s) passed.", flush=True)

    return {"passed": passed, "failed": failed, "scenarios": run_results}


# ---------------------------------------------------------------------------
# Step 6 — Close review
# ---------------------------------------------------------------------------

def close_review(review_id: str, outcome: str, notes: str = "") -> None:
    """
    Mark a review as complete (pass) or escalated (block/fail).

    Args:
        review_id: The review to close
        outcome:   "pass" | "escalated" | "blocked"
        notes:     Optional notes for the record
    """
    queue = _read(_REVIEW_QUEUE)
    for review in queue["reviews"]:
        if review["review_id"] == review_id:
            review["status"]  = outcome
            review["closed_at"] = _now()
            if notes:
                review["close_notes"] = notes
            break
    _write(_REVIEW_QUEUE, queue)
    _set_state(None, "idle")
    print(f"[BrainLoop] Review {review_id} closed: {outcome}", flush=True)


# ---------------------------------------------------------------------------
# Utility — regression run
# ---------------------------------------------------------------------------

def run_regression(runner: callable) -> dict:
    """
    Run all passing UAT scenarios in the library as regression.

    Args:
        runner: callable(scenario: dict) -> bool

    Returns:
        {"passed": int, "failed": int, "regressions": list[scenario_id]}
    """
    library = _read(_UAT_LIBRARY)
    passed = failed = 0
    regressions = []

    for s in library["scenarios"]:
        if s.get("status") not in ("pass",):
            continue  # only regress previously passing scenarios
        try:
            ok = runner(s)
        except Exception:
            ok = False

        if ok:
            passed += 1
        else:
            failed += 1
            regressions.append(s["scenario_id"])
            s["status"] = "regression_fail"
            s["last_run"] = _now()

    _write(_UAT_LIBRARY, library)
    print(f"[BrainLoop] Regression: {passed} pass, {failed} fail. Regressions: {regressions}", flush=True)
    return {"passed": passed, "failed": failed, "regressions": regressions}


# ---------------------------------------------------------------------------
# Utility — current state
# ---------------------------------------------------------------------------

def get_state() -> dict:
    """Return current execution state."""
    return _read(_EXEC_STATE)


def get_pending_reviews() -> list[dict]:
    """Return all reviews with status=pending."""
    queue = _read(_REVIEW_QUEUE)
    return [r for r in queue.get("reviews", []) if r.get("status") == "pending"]
