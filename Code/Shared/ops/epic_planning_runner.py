# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# epic_planning_runner.py — Iterative epic planning loop.
# Drives GPT (Enterprise Architect) through up to 1000 iterations
# to produce an agreed epic plan (Word + JSON).
#
# Usage: python epic_planning_runner.py [--max-iterations N] [--prompt-record RECORD_ID]

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Setup paths
_THIS_DIR = Path(__file__).parent
_SHARED_DIR = _THIS_DIR.parent
_REPO_ROOT = _SHARED_DIR.parent.parent
_BRAIN_DIR = _REPO_ROOT / "brain"
_CACHE_DIR = _BRAIN_DIR / "machine_artifacts" / ".iteration_cache"
_AI_OPS = _BRAIN_DIR / "machine_artifacts" / "content" / "ai_operations.json"
_EPIC_SYSTEM_PROMPT = _BRAIN_DIR / "machine_artifacts" / "content" / "epic_planning_system_prompt.json"
_EPIC_USER_PROMPT = _BRAIN_DIR / "machine_artifacts" / "content" / "epic_planning_user_prompt_v014.txt"

sys.path.insert(0, str(_SHARED_DIR))
from dotenv import load_dotenv
load_dotenv(_SHARED_DIR.parent / ".env")


def _load_system_prompt() -> str:
    """Load the epic planning system prompt from brain."""
    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        data = json.load(f)
    sp = data.get("system_prompt", {})
    return json.dumps(sp, ensure_ascii=False) if isinstance(sp, dict) else str(sp)


def _load_user_prompt_text() -> str:
    """Load the full user prompt text from brain."""
    with open(_EPIC_USER_PROMPT, encoding="utf-8") as f:
        return f.read()


def _load_last_iteration() -> dict | None:
    """Load the last cached iteration if it exists."""
    if not _CACHE_DIR.exists():
        return None
    jsons = sorted(_CACHE_DIR.glob("iteration_*.json"), reverse=True)
    if not jsons:
        return None
    with open(jsons[0], encoding="utf-8") as f:
        return json.load(f)


def _build_user_message(prompt_text: str, iteration: int, max_iter: int, last_iteration: dict | None) -> str:
    """Build the user message for this iteration.

    Sends the full Boss-approved prompt text with iteration counter substituted.
    """
    # Substitute iteration placeholders
    message = prompt_text.replace("{iteration}", str(iteration)).replace("{max_iterations}", str(max_iter))

    if last_iteration:
        last_str = json.dumps(last_iteration, indent=2, ensure_ascii=False)
        if len(last_str) > 40000:
            last_str = last_str[:40000] + "\n... (truncated)"
        message += (
            "\n\n---\n\n"
            "LAST ITERATION (review as starting place — flag non-trivial changes per iteration protocol):\n\n"
            + last_str
        )

    return message


def _call_gpt(system_prompt: str, user_message: str, model: str) -> tuple[str, int, int]:
    """Call GPT. Returns (response_text, tokens_in, tokens_out)."""
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    # GPT-5.3 only supports temperature=1 (default). Do not override.
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format={"type": "json_object"},
    )
    text = response.choices[0].message.content
    tokens_in = response.usage.prompt_tokens
    tokens_out = response.usage.completion_tokens
    return text, tokens_in, tokens_out


def _cache_iteration(iteration: int, result: dict, raw_text: str) -> Path:
    """Cache this iteration's JSON to the iteration cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _CACHE_DIR / f"iteration_{iteration:04d}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return path


def _is_converged(current: dict, previous: dict | None) -> bool:
    """Check if the plan has converged (no substantive changes)."""
    if previous is None:
        return False

    # Compare key structural elements
    for key in ["feature_set", "requirements", "sprint_capability_map", "risk_matrix"]:
        cur = json.dumps(current.get(key, {}), sort_keys=True)
        prev = json.dumps(previous.get(key, {}), sort_keys=True)
        if cur != prev:
            return False

    return True


def run(max_iterations: int = 1000, prompt_record_id: str = "epic_planning_user_prompt_v014"):
    """Run the epic planning iteration loop."""
    print(f"[EpicPlanner] Loading prompts...")
    system_prompt = _load_system_prompt()
    prompt_text = _load_user_prompt_text()

    # Read model from system prompt file
    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        sp_data = json.load(f)
    model = sp_data.get("model", "gpt-5.3-chat-latest")

    print(f"[EpicPlanner] Model: {model}")
    print(f"[EpicPlanner] Max iterations: {max_iterations}")
    print(f"[EpicPlanner] Cache: {_CACHE_DIR}")

    last_iteration = _load_last_iteration()
    if last_iteration:
        print(f"[EpicPlanner] Found prior iteration in cache — GPT will review as starting place")
    else:
        print(f"[EpicPlanner] No prior iteration — starting fresh")

    total_tokens_in = 0
    total_tokens_out = 0

    for i in range(1, max_iterations + 1):
        print(f"\n{'='*60}")
        print(f"  ITERATION {i} of {max_iterations}")
        print(f"{'='*60}")

        user_message = _build_user_message(prompt_text, i, max_iterations, last_iteration)

        try:
            from cost_guard import check_budget
            budget = check_budget(f"epic_planning_v014_iter_{i}")
            if not budget["ok"]:
                print(f"[EpicPlanner] BUDGET BLOCKED: {budget['reason']}")
                print(f"[EpicPlanner] Stopping at iteration {i}")
                break
        except (ImportError, FileNotFoundError):
            pass  # cost_guard or budget config not available, proceed

        print(f"[EpicPlanner] Calling {model}...")
        try:
            raw_text, tokens_in, tokens_out = _call_gpt(system_prompt, user_message, model)
        except Exception as e:
            error_str = str(e)
            print(f"[EpicPlanner] ERROR: GPT call failed — {error_str}")
            # Don't retry the same error endlessly
            if not hasattr(run, '_last_error'):
                run._last_error = error_str
                run._error_count = 1
            elif run._last_error == error_str:
                run._error_count += 1
                if run._error_count >= 3:
                    print(f"[EpicPlanner] Same error 3 times — stopping. Fix the issue and rerun.")
                    break
            else:
                run._last_error = error_str
                run._error_count = 1
            print(f"[EpicPlanner] Retrying ({run._error_count}/3)...")
            continue

        total_tokens_in += tokens_in
        total_tokens_out += tokens_out
        print(f"[EpicPlanner] Tokens: {tokens_in} in / {tokens_out} out")

        try:
            from cost_guard import log_usage
            cost = log_usage(
                agent="GPT",
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                assignment_id=f"epic_planning_v014",
                call_type="epic_planning",
            )
            print(f"[EpicPlanner] Cost this call: ${cost:.4f}")
        except Exception as e:
            print(f"[EpicPlanner] Cost logging skipped: {e}")

        # Parse response
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError as e:
            print(f"[EpicPlanner] ERROR: Response is not valid JSON — {e}")
            print(f"[EpicPlanner] Raw (first 500): {raw_text[:500]}")
            continue

        # Add metadata
        result["_iteration"] = i
        result["_timestamp"] = datetime.now(timezone.utc).isoformat()
        result["_tokens_in"] = tokens_in
        result["_tokens_out"] = tokens_out

        # Check for non-trivial changes flagged by GPT
        if result.get("non_trivial_changes_from_last"):
            print(f"[EpicPlanner] GPT flagged non-trivial changes:")
            for change in result["non_trivial_changes_from_last"]:
                print(f"  - {change}")

        # Cache
        cache_path = _cache_iteration(i, result, raw_text)
        print(f"[EpicPlanner] Cached: {cache_path.name}")

        # Check convergence
        if _is_converged(result, last_iteration):
            print(f"\n{'='*60}")
            print(f"  CONVERGED at iteration {i}")
            print(f"  Total tokens: {total_tokens_in} in / {total_tokens_out} out")
            print(f"{'='*60}")
            break

        # Check gate recommendation
        gate = result.get("gate_recommendation", "")
        risk = result.get("risk", "")
        print(f"[EpicPlanner] Risk: {risk} | Gate: {gate}")

        if gate in ("auto", "proceed_with_warning"):
            print(f"[EpicPlanner] GPT recommends proceeding — checking if plan is complete...")
            # Check assignment DoD
            has_features = bool(result.get("feature_set", {}).get("ships"))
            has_requirements = bool(result.get("requirements"))
            has_sprint_map = bool(result.get("sprint_capability_map"))
            has_risk_matrix = bool(result.get("risk_matrix"))
            if all([has_features, has_requirements, has_sprint_map, has_risk_matrix]):
                print(f"[EpicPlanner] Plan appears complete at iteration {i} — stopping for Claude validation")
                break
            else:
                missing = []
                if not has_features: missing.append("features")
                if not has_requirements: missing.append("requirements")
                if not has_sprint_map: missing.append("sprint_capability_map")
                if not has_risk_matrix: missing.append("risk_matrix")
                print(f"[EpicPlanner] Plan incomplete — missing: {', '.join(missing)}")

        last_iteration = result

    else:
        print(f"\n[EpicPlanner] Reached max iterations ({max_iterations}) without convergence")

    print(f"\n[EpicPlanner] Total tokens: {total_tokens_in} in / {total_tokens_out} out")
    print(f"[EpicPlanner] Cache directory: {_CACHE_DIR}")
    print(f"[EpicPlanner] Claude: validate the last cached iteration and produce the Word doc")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatHealthy.ai Epic Planning Runner")
    parser.add_argument("--max-iterations", type=int, default=1000)
    parser.add_argument("--prompt-record", default="epic_planning_user_prompt_v014")
    args = parser.parse_args()
    run(max_iterations=args.max_iterations, prompt_record_id=args.prompt_record)
