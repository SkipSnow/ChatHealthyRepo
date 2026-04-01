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


def _synthesize(cumulative: dict, incremental: dict) -> dict:
    """Claude synthesizes: merge incremental GPT output into cumulative plan.

    GPT builds incrementally — adds, never replaces. Claude merges.
    """
    if not cumulative:
        return incremental

    def _merge_collection(cum_val, inc_val, id_key):
        """Merge list-of-dicts or dict-of-dicts by ID, preferring incremental for updates."""
        # Normalize to dict
        def _to_dict(val):
            if isinstance(val, dict):
                return dict(val)
            if isinstance(val, list):
                return {item.get(id_key, f"_anon_{i}"): item for i, item in enumerate(val) if isinstance(item, dict)}
            return {}

        cum_map = _to_dict(cum_val)
        inc_map = _to_dict(inc_val)

        # Merge — incremental wins on conflict
        merged = dict(cum_map)
        for k, v in inc_map.items():
            merged[k] = v

        # Return as list (GPT may output either format)
        return list(merged.values())

    result = dict(cumulative)

    # Merge collections by their ID keys
    merge_keys = {
        "features": "feature_id",
        "stories": "story_id",
        "requirements": "req_id",
    }
    for key, id_key in merge_keys.items():
        if key in incremental and incremental[key]:
            result[key] = _merge_collection(
                cumulative.get(key, []),
                incremental[key],
                id_key
            )

    # Sprint map — replace by sprint number
    if "sprint_capability_map" in incremental and incremental["sprint_capability_map"]:
        inc_map = incremental["sprint_capability_map"]
        cum_map = cumulative.get("sprint_capability_map", {})
        if isinstance(inc_map, list):
            inc_map = {str(s.get("sprint", i)): s for i, s in enumerate(inc_map) if isinstance(s, dict)}
        if isinstance(cum_map, list):
            cum_map = {str(s.get("sprint", i)): s for i, s in enumerate(cum_map) if isinstance(s, dict)}
        merged_map = dict(cum_map) if isinstance(cum_map, dict) else {}
        if isinstance(inc_map, dict):
            merged_map.update(inc_map)
        result["sprint_capability_map"] = list(merged_map.values()) if merged_map else inc_map

    # Risk matrix — deep merge
    if "risk_matrix" in incremental and incremental["risk_matrix"]:
        cum_rm = cumulative.get("risk_matrix", {})
        inc_rm = incremental["risk_matrix"]
        if isinstance(cum_rm, dict) and isinstance(inc_rm, dict):
            merged_rm = dict(cum_rm)
            for k, v in inc_rm.items():
                if isinstance(v, list) and isinstance(merged_rm.get(k), list):
                    # Merge lists by feature_id
                    existing = {item.get("feature_id", ""): item for item in merged_rm[k] if isinstance(item, dict)}
                    for item in v:
                        if isinstance(item, dict):
                            existing[item.get("feature_id", "")] = item
                    merged_rm[k] = list(existing.values())
                else:
                    merged_rm[k] = v
            result["risk_matrix"] = merged_rm

    # Scalar fields — take incremental
    for key in ["epics", "issues", "notes", "risk", "gate_recommendation", "change_log", "content_requests"]:
        if key in incremental and incremental[key]:
            result[key] = incremental[key]

    return result


def _recommend_stories(feature_id: str, feature: dict) -> list[str]:
    """Claude recommends story breakdowns for thin features."""
    if not isinstance(feature, dict):
        return ["define schema", "implement logic", "write tests"]

    fname = feature.get("name", "").lower()
    layer = feature.get("layer", "").lower()
    capability = feature.get("capability", "").lower()

    # Measure features (backend data extraction)
    if "measure" in feature_id.lower() or "measure" in fname:
        return [
            "Define measure schema (input fields, score range, weight default)",
            "Implement data extraction from source (NPI/ClinicalTrials.gov/etc)",
            "Implement normalization and scoring logic",
            "Write unit tests with edge cases (missing data, invalid values)",
        ]

    # Scoring engine
    if "score" in fname or "scoring" in fname:
        return [
            "Define composite score schema (measures array, weights, total)",
            "Implement weighted aggregation with deterministic output",
            "Handle missing measures gracefully (degrade, don't fail)",
            "Write unit tests (all measures present, partial, none)",
            "Integration test with real measure outputs",
        ]

    # Explainability
    if "explain" in fname:
        if "ui" in fname or "ux" in fname or "frontend" in layer:
            return [
                "Design score card component layout",
                "Implement score rendering with measure breakdown",
                "Implement visual weight indicators",
                "Handle loading/error states",
                "Write component tests",
            ]
        return [
            "Define explainability response schema",
            "Implement measure contribution calculation",
            "Generate human-readable rationale per measure",
            "Write unit tests for explanation accuracy",
        ]

    # Maps
    if "map" in fname:
        return [
            "Integrate Google Maps JS API",
            "Render provider markers from lat/lng",
            "Implement click-to-detail on markers",
            "Handle map loading/error states",
            "Write component tests",
        ]

    # Distance
    if "distance" in fname or "dist" in feature_id.lower():
        if "frontend" in layer or "ux" in fname or "display" in fname:
            return [
                "Display distance in provider cards",
                "Display distance in search results list",
                "Handle missing distance gracefully",
                "Write component tests",
            ]
        return [
            "Implement geolocation from user browser",
            "Calculate distance from user to provider (Haversine or Google Routes)",
            "Add distance field to all address-returning API responses",
            "Write unit tests (same city, cross-state, missing coords)",
        ]

    # Architecture refactor
    if "arch" in fname or "refactor" in fname:
        return [
            "Extract domain services from monolith (phase 1: facades)",
            "Extract domain services (phase 2: individual services)",
            "Wire ToolRouter to new service layer",
            "Delete monolith main.py business logic",
            "Regression test all existing tools against new architecture",
        ]

    # Consent
    if "consent" in fname:
        return [
            "Fix consent_verbatim flag inversion",
            "Fix PII not scrubbed when de-identify requested",
            "Write regression tests for both consent tiers",
        ]

    # Prescription behavior
    if "rx" in feature_id.lower() or "prescription" in fname:
        return [
            "Identify prescription data source (CMS Open Payments, Medicare Part D, etc)",
            "Design prescription-to-condition mapping schema",
            "Implement data extraction pipeline",
            "Implement AI analysis of prescription patterns",
            "Write tests with known prescription profiles",
        ]

    # Brand audit
    if "brand" in fname:
        return [
            "Scan all code for 'ChatHealthy' without '.ai' suffix",
            "Scan all brain artifacts and business docs",
            "Fix all instances",
            "Write automated check to prevent regression",
        ]

    # Brain enhancements
    if "brain" in fname:
        return [
            "Identify specific Brain improvements for v0.1.4 delivery",
            "Implement improvements",
            "Write tests",
        ]

    # Weights (stretch)
    if "weight" in fname:
        return [
            "Design weight adjustment UI (sliders or inputs per measure)",
            "Implement weight persistence per session",
            "Implement score recalculation on weight change",
            "Write tests (default weights, custom weights, edge values)",
        ]

    # Generic fallback
    return [
        "Define schema and data contract",
        "Implement core logic",
        "Write API endpoint or UI component",
        "Write unit tests with edge cases",
    ]


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

    # Check story granularity — minimum 3 stories per feature
    feat_story_count = {}
    for sid, s in stories.items():
        parent = s.get("parent_feature", "")
        feat_story_count[parent] = feat_story_count.get(parent, 0) + 1

    thin_story_features = [fid for fid in features if feat_story_count.get(fid, 0) < 3]
    if thin_story_features:
        # Build Claude's story recommendations for thin features
        story_recs = []
        for fid in thin_story_features:
            f = features[fid]
            fname = f.get("name", fid) if isinstance(f, dict) else fid
            current = feat_story_count.get(fid, 0)
            recs = _recommend_stories(fid, f)
            story_recs.append(f"{fid} ({fname}): has {current} story, needs minimum 3. "
                              f"Claude recommends: {'; '.join(recs)}")
        objections.append(
            f"STORY GRANULARITY: {len(thin_story_features)} features have fewer than 3 stories. "
            f"Break each feature into implementation stories (schema, logic, API, tests, integration). "
            f"Recommendations:\n" + "\n".join(f"  - {r}" for r in story_recs)
        )

    # Check evidence on every feature and story
    features_no_evidence = []
    for fid, f in features.items():
        if isinstance(f, dict) and not f.get("evidence"):
            features_no_evidence.append(fid)
    if features_no_evidence:
        objections.append(
            f"EVIDENCE MISSING: {len(features_no_evidence)} features have no evidence field: "
            f"{', '.join(features_no_evidence[:10])}. Every feature must cite a manifest entry or state GAP."
        )

    stories_no_evidence = []
    for sid, s in stories.items():
        if isinstance(s, dict) and not s.get("evidence"):
            stories_no_evidence.append(sid)
    if stories_no_evidence:
        objections.append(
            f"EVIDENCE MISSING: {len(stories_no_evidence)} stories have no evidence field: "
            f"{', '.join(stories_no_evidence[:10])}. Every story must cite evidence or state GAP."
        )

    # If there are GAPs, GPT should be requesting content to verify them
    all_evidence = json.dumps([f.get("evidence", "") for f in features.values() if isinstance(f, dict)]
                              + [s.get("evidence", "") for s in stories.values() if isinstance(s, dict)])
    gap_count = all_evidence.lower().count("gap:")
    has_requests = bool(result.get("content_requests"))
    if gap_count > 3 and not has_requests:
        objections.append(
            f"RESEARCH NEEDED: {gap_count} GAPs identified but no content_requests made. "
            f"Use content_requests to verify GAPs against the manifest and Brain before proceeding."
        )

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
    cumulative_plan = last_iteration  # Claude's synthesized plan
    if last_iteration:
        print(f"[EpicPlanner] Found prior iteration in cache — GPT will review as starting place")
    else:
        print(f"[EpicPlanner] No prior iteration — starting fresh")

    total_tokens_in = 0
    total_tokens_out = 0
    iterations_since_content_request = 0

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

        # Inner loop: research phase — GPT asks for content, Claude fulfills, repeat
        result = None
        research_round = 0
        max_research = 10  # cap research rounds per iteration
        current_message = user_message

        while True:
            research_round += 1
            print(f"[EpicPlanner] Calling {model} (research round {research_round})...")
            try:
                raw_text, tokens_in, tokens_out = _call_gpt(system_prompt, current_message, model)
            except Exception as e:
                error_str = str(e)
                print(f"[EpicPlanner] ERROR: GPT call failed — {error_str}")
                if not hasattr(run, '_last_error'):
                    run._last_error = error_str
                    run._error_count = 1
                elif run._last_error == error_str:
                    run._error_count += 1
                    if run._error_count >= 3:
                        print(f"[EpicPlanner] Same error 3 times — stopping.")
                        break
                else:
                    run._last_error = error_str
                    run._error_count = 1
                print(f"[EpicPlanner] Retrying ({run._error_count}/3)...")
                break

            total_tokens_in += tokens_in
            total_tokens_out += tokens_out
            print(f"[EpicPlanner] Tokens: {tokens_in} in / {tokens_out} out")

            try:
                from cost_guard import log_usage
                log_usage(agent="GPT", model=model, tokens_in=tokens_in, tokens_out=tokens_out,
                          assignment_id="epic_planning_v014", call_type="epic_planning")
            except Exception:
                pass

            try:
                result = json.loads(raw_text)
            except json.JSONDecodeError as e:
                print(f"[EpicPlanner] ERROR: Not valid JSON — {e}")
                print(f"[EpicPlanner] Raw (first 500): {raw_text[:500]}")
                break

            # Check if GPT is asking for more content
            content_requests = result.get("content_requests", [])
            if content_requests and isinstance(content_requests, list) and len(content_requests) > 0 and research_round < max_research:
                print(f"[EpicPlanner] GPT requesting {len(content_requests)} resources — fulfilling...")
                for req in content_requests:
                    if isinstance(req, dict):
                        print(f"  -> {req.get('type','?')}: {req.get('path','?')} ({req.get('reason','?')})")
                fulfilled = _fulfill_content_requests(result)
                # Send fulfilled content back as a follow-up message
                current_message = (
                    f"Iteration {i}, research round {research_round + 1}.\n"
                    f"Here is the content you requested:\n\n{fulfilled}\n\n"
                    f"Continue building the plan. Do not repeat content_requests for resources already provided."
                )
                continue
            else:
                if content_requests and research_round >= max_research:
                    print(f"[EpicPlanner] Research cap ({max_research}) reached — proceeding with what we have")
                break

        # If we broke out due to error or no result, continue to next iteration
        if not isinstance(result, dict):
            continue
        # Reset error tracking on success
        run._last_error = None
        run._error_count = 0

        # Track content_request usage — ding GPT if 5 iterations without research
        content_reqs = result.get("content_requests", [])
        if content_reqs and isinstance(content_reqs, list) and len(content_reqs) > 0:
            iterations_since_content_request = 0
        else:
            iterations_since_content_request += 1

        # Claude synthesizes: merge incremental GPT output into cumulative plan
        cumulative_plan = _synthesize(cumulative_plan, result)
        cumulative_plan["_iteration"] = i
        cumulative_plan["_timestamp"] = datetime.now(timezone.utc).isoformat()
        cumulative_plan["_tokens_in"] = total_tokens_in
        cumulative_plan["_tokens_out"] = total_tokens_out
        cumulative_plan["_research_rounds"] = research_round

        # Cache the synthesized plan (not raw GPT output)
        cache_path = _cache_iteration(i, cumulative_plan, "")
        print(f"[EpicPlanner] Synthesized and cached: {cache_path.name}")

        # Report cumulative stats
        def _count(val):
            if isinstance(val, (dict, list)): return len(val)
            return 0
        print(f"[EpicPlanner] Cumulative: feat={_count(cumulative_plan.get('features',[]))} "
              f"stories={_count(cumulative_plan.get('stories',[]))} "
              f"reqs={_count(cumulative_plan.get('requirements',[]))}")

        # Claude validates the CUMULATIVE plan, not just the latest GPT output
        claude_objections = _validate_iteration(cumulative_plan, i)

        # Ding GPT if 5 iterations without content_request
        if iterations_since_content_request >= 5:
            claude_objections.append(
                f"RESEARCH: You have not requested any Brain content in {iterations_since_content_request} iterations. "
                f"Do not work from assumptions. Use content_requests to read the Brain before proceeding."
            )

        gpt_says_done = result.get("gate_recommendation") in ("auto", "proceed_with_warning")

        if not claude_objections and gpt_says_done and _is_converged(cumulative_plan, last_iteration, i):
            print(f"\n{'='*60}")
            print(f"  CONVERGED at iteration {i}")
            print(f"  Claude: 0 objections on cumulative plan")
            print(f"  GPT: gate={result.get('gate_recommendation')}")
            print(f"  Both agree — plan is complete.")
            print(f"  Total tokens: {total_tokens_in} in / {total_tokens_out} out")
            print(f"{'='*60}")
            break
        elif not claude_objections and gpt_says_done:
            print(f"[EpicPlanner] Claude agrees, GPT agrees, but structure changed — one more for stability")
        elif not claude_objections:
            print(f"[EpicPlanner] Claude agrees but GPT gate={result.get('gate_recommendation')} — continuing")
        else:
            print(f"[EpicPlanner] Claude has {len(claude_objections)} objections — continuing")

        last_iteration = cumulative_plan

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
