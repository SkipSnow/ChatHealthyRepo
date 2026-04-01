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


def _validate_iteration(result: dict, iteration: int) -> list[str]:
    """Claude validates the iteration and returns a list of objections.

    Empty list = Claude agrees. Non-empty = GPT must address these in next iteration.
    """
    objections = []

    # Check all required top-level keys exist
    required_keys = ["epics", "features", "stories", "requirements", "sprint_capability_map", "risk_matrix"]
    for k in required_keys:
        if k not in result or not result[k]:
            objections.append(f"MISSING: '{k}' is empty or absent.")

    # Normalize features/stories/requirements — GPT may return dict or list
    def _to_id_map(val, id_key):
        """Convert list-of-dicts to {id: dict} or pass through dict."""
        if isinstance(val, dict):
            return val
        if isinstance(val, list):
            return {item.get(id_key, f"?_{i}"): item for i, item in enumerate(val) if isinstance(item, dict)}
        return {}

    features = _to_id_map(result.get("features", {}), "feature_id")
    stories = _to_id_map(result.get("stories", {}), "story_id")
    requirements = _to_id_map(result.get("requirements", {}), "req_id")
    sprint_map = result.get("sprint_capability_map", {})
    if isinstance(sprint_map, list):
        sprint_map = {str(s.get("sprint", i)): s for i, s in enumerate(sprint_map) if isinstance(s, dict)}

    # Check every story has at least one requirement
    story_ids_with_reqs = set()
    for rid, r in requirements.items():
        story_ids_with_reqs.add(r.get("story_id", ""))
    stories_without_reqs = [sid for sid in stories if sid not in story_ids_with_reqs]
    if stories_without_reqs:
        objections.append(
            f"REQUIREMENT COVERAGE: {len(stories_without_reqs)} stories have zero requirements: "
            f"{', '.join(stories_without_reqs[:10])}. Every story must have boolean requirements."
        )

    # Check requirement density
    req_count = len(requirements)
    story_count = len(stories)
    if story_count > 0 and req_count < story_count * 3:
        objections.append(
            f"THIN REQUIREMENTS: Only {req_count} requirements across {story_count} stories. "
            f"Procedural stories need all happy paths + 2 exception paths. LLM stories need 10."
        )

    # Check feature descriptions are substantive
    thin_features = [fid for fid, f in features.items() if len(str(f.get("description", ""))) < 30]
    if thin_features:
        objections.append(
            f"THIN FEATURES: {len(thin_features)} features have descriptions under 30 chars: "
            f"{', '.join(thin_features[:10])}. Specify data source, calculation, and why credible."
        )

    # Check EPIC-2 features aren't silently dropped
    expected_maint = ["MAINT-MAPS", "MAINT-DIST", "MAINT-DIST-UX", "MAINT-ARCH",
                      "MAINT-CONSENT", "MAINT-BRAIN", "BRAND-001"]
    all_content_str = json.dumps(result)
    truly_missing = [f for f in expected_maint if f not in all_content_str]
    if truly_missing:
        objections.append(
            f"DROPPED FEATURES: {', '.join(truly_missing)} disappeared without deferral rationale."
        )

    # Check stretch goal and Boss suggestion are addressed
    if "EVAL-WEIGHTS" not in all_content_str:
        objections.append("STRETCH GOAL: EVAL-WEIGHTS not present, deferred, or declined. Must be addressed.")
    if "rx" not in all_content_str.lower() and "prescription" not in all_content_str.lower():
        objections.append("BOSS SUGGESTION: Prescription behavior (EVAL-P-RX) not addressed.")

    # Check change_log for iteration 2+
    if iteration > 1 and not result.get("change_log"):
        objections.append("CHANGE LOG MISSING: Required from iteration 2+.")

    # Check sprint capacity
    for sid, s in sprint_map.items():
        shipped = s.get("features_shipped", [])
        if isinstance(shipped, list) and len(shipped) > 6:
            objections.append(f"OVERLOADED: Sprint {sid} has {len(shipped)} features.")
        if str(sid) in ("1", "2", "3", "4") and isinstance(shipped, list) and len(shipped) == 0:
            objections.append(f"EMPTY SPRINT: Sprint {sid} ships zero features.")

    return objections


def _fulfill_content_requests(last_iteration: dict | None) -> str:
    """Read files/records GPT requested in its content_requests array.

    Returns formatted content string to inject in the next prompt.
    """
    if not last_iteration:
        return ""
    requests = last_iteration.get("content_requests", [])
    if not requests or not isinstance(requests, list):
        return ""

    parts = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        req_type = req.get("type", "file")
        path = req.get("path", "")
        reason = req.get("reason", "")

        if req_type == "file":
            full_path = _REPO_ROOT / path
            if full_path.exists() and full_path.is_file():
                try:
                    content = full_path.read_text(encoding="utf-8")
                    # Cap individual file at 10K chars
                    if len(content) > 10000:
                        content = content[:10000] + "\n... (truncated at 10K chars)"
                    parts.append(f"=== REQUESTED: {path} (reason: {reason}) ===\n{content}")
                except Exception as e:
                    parts.append(f"=== REQUESTED: {path} — read error: {e} ===")
            else:
                parts.append(f"=== REQUESTED: {path} — file not found ===")

        elif req_type == "record":
            # path format: collection_name/_record_id
            try:
                coll_name, record_id = path.split("/", 1)
                coll_path = _BRAIN_DIR / "machine_artifacts" / "content" / f"{coll_name}.json"
                if coll_path.exists():
                    with open(coll_path, encoding="utf-8") as f:
                        data = json.load(f)
                    for r in data.get("records", []):
                        if isinstance(r, dict) and r.get("_record_id") == record_id:
                            content = json.dumps(r, indent=2, ensure_ascii=False)
                            if len(content) > 10000:
                                content = content[:10000] + "\n... (truncated)"
                            parts.append(f"=== REQUESTED RECORD: {path} (reason: {reason}) ===\n{content}")
                            break
                    else:
                        parts.append(f"=== REQUESTED RECORD: {path} — record not found ===")
                else:
                    parts.append(f"=== REQUESTED RECORD: {path} — collection not found ===")
            except Exception as e:
                parts.append(f"=== REQUESTED RECORD: {path} — error: {e} ===")

    if not parts:
        return ""
    return "\n\n".join(parts)


def _build_user_message(prompt_text: str, iteration: int, max_iter: int,
                        last_iteration: dict | None, claude_feedback: list[str] | None = None) -> str:
    """Build the user message for this iteration.

    Sends the full Boss-approved prompt text with iteration counter substituted.
    If Claude has feedback from validating the last iteration, it's injected.
    """
    # Substitute iteration placeholders
    message = prompt_text.replace("{iteration}", str(iteration)).replace("{max_iterations}", str(max_iter))

    # Inject Brain manifest — GPT sees the table of contents
    from brain_snapshot import take_snapshot
    manifest = take_snapshot()
    message += "\n\n---\n\nBRAIN MANIFEST (table of contents — request content via content_requests in your response):\n\n" + manifest

    # Fulfill content requests from last iteration
    fulfilled = _fulfill_content_requests(last_iteration)
    if fulfilled:
        message += "\n\n---\n\nREQUESTED CONTENT (fetched from disk by Claude):\n\n" + fulfilled

    if last_iteration:
        last_str = json.dumps(last_iteration, indent=2, ensure_ascii=False)
        if len(last_str) > 40000:
            last_str = last_str[:40000] + "\n... (truncated)"
        message += (
            "\n\n---\n\n"
            "LAST ITERATION (review as starting place — flag non-trivial changes per iteration protocol):\n\n"
            + last_str
        )

    if claude_feedback:
        feedback_text = "\n".join(f"- {obj}" for obj in claude_feedback)
        message += (
            "\n\n---\n\n"
            "CLAUDE VALIDATION FEEDBACK (Engineer review of last iteration — must address all items):\n\n"
            + feedback_text
            + "\n\nAddress every item above in this iteration. Do not ignore any."
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


def _is_converged(current: dict, previous: dict | None, iteration: int) -> bool:
    """Check if the plan has converged.

    Convergence requires:
    1. Claude has zero objections to the current iteration
    2. No material structural changes from previous iteration
    """
    if previous is None:
        return False

    # Claude must validate first — if there are objections, not converged
    objections = _validate_iteration(current, iteration)
    if objections:
        return False

    # Compare key structural elements — normalize to sorted JSON for comparison
    for key in ["features", "stories", "requirements", "sprint_capability_map", "risk_matrix"]:
        try:
            cur = json.dumps(current.get(key, {}), sort_keys=True)
            prev = json.dumps(previous.get(key, {}), sort_keys=True)
        except TypeError:
            cur = str(current.get(key, ""))
            prev = str(previous.get(key, ""))
        if cur != prev:
            return False

    return True


def refresh_manifest():
    """Regenerate the project manifest before running the planning loop.

    Called at Boss or Claude discretion to ensure GPT reads fresh Brain context.
    """
    try:
        sys.path.insert(0, str(_THIS_DIR))
        from manifest_generator import ManifestGenerator
        gen = ManifestGenerator()
        gen.generate()
        path = gen.save()
        print(f"[EpicPlanner] Manifest refreshed: {gen.file_count} files, {gen.total_entries} entries -> {path}")
    except Exception as e:
        print(f"[EpicPlanner] Manifest refresh failed: {e} — continuing with stale manifest")


def run(max_iterations: int = 1000, prompt_record_id: str = "epic_planning_user_prompt_v014"):
    """Run the epic planning iteration loop."""
    # Always refresh manifest before an assignment — GPT must read fresh context
    refresh_manifest()
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

        # Claude validates the last iteration and provides feedback for GPT
        claude_feedback = None
        if last_iteration and i > 1:
            claude_feedback = _validate_iteration(last_iteration, i - 1)
            if claude_feedback:
                print(f"[EpicPlanner] Claude feedback ({len(claude_feedback)} items):")
                for fb in claude_feedback:
                    print(f"  - {fb[:120]}")
            else:
                print(f"[EpicPlanner] Claude: no objections to last iteration")

        user_message = _build_user_message(prompt_text, i, max_iterations, last_iteration, claude_feedback)

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

        # Check convergence — both Claude and GPT must agree at 100%
        claude_objections = _validate_iteration(result, i)
        gpt_says_done = result.get("gate_recommendation") in ("auto", "proceed_with_warning")

        if not claude_objections and gpt_says_done and _is_converged(result, last_iteration, i):
            print(f"\n{'='*60}")
            print(f"  CONVERGED at iteration {i}")
            print(f"  Claude: 0 objections")
            print(f"  GPT: gate={result.get('gate_recommendation')}")
            print(f"  Both agree — plan is complete.")
            print(f"  Total tokens: {total_tokens_in} in / {total_tokens_out} out")
            print(f"{'='*60}")
            break
        elif not claude_objections and gpt_says_done:
            print(f"[EpicPlanner] Claude agrees, GPT agrees, but structure changed — iterating once more for stability")
        elif not claude_objections:
            print(f"[EpicPlanner] Claude agrees but GPT gate={result.get('gate_recommendation')} — continuing")
        else:
            print(f"[EpicPlanner] Claude has {len(claude_objections)} objections — continuing")

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
    parser.add_argument("--refresh-manifest", action="store_true", help="Regenerate project manifest before running")
    args = parser.parse_args()
    if args.refresh_manifest:
        refresh_manifest()
    run(max_iterations=args.max_iterations, prompt_record_id=args.prompt_record)
