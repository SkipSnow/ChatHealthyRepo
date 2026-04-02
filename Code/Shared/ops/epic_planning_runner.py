# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# epic_planning_runner.py — Phased epic planning loop.
# Five phases, each converges before advancing:
#   Phase 1: Feature list with evidence
#   Phase 2: Stories per feature (one at a time)
#   Phase 3: Requirements per story (one at a time)
#   Phase 4: Sprint map and capacity
#   Phase 5: Risk matrix and gate recommendation
#
# Usage: python epic_planning_runner.py [--max-iterations N] [--phase N]

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_THIS_DIR = Path(__file__).parent
_SHARED_DIR = _THIS_DIR.parent
_REPO_ROOT = _SHARED_DIR.parent.parent
_BRAIN_DIR = _REPO_ROOT / "brain"
_CACHE_DIR = _BRAIN_DIR / "machine_artifacts" / ".iteration_cache"
_EPIC_SYSTEM_PROMPT = _BRAIN_DIR / "machine_artifacts" / "content" / "epic_planning_system_prompt.json"
_EPIC_USER_PROMPT = _BRAIN_DIR / "machine_artifacts" / "content" / "epic_planning_user_prompt_v014.txt"
_PLAN_FILE = _CACHE_DIR / "cumulative_plan.json"

sys.path.insert(0, str(_SHARED_DIR))
sys.path.insert(0, str(_THIS_DIR))
from dotenv import load_dotenv
load_dotenv(_SHARED_DIR.parent / ".env")


# ── Load / Save ──────────────────────────────────────────────

def _load_system_prompt() -> str:
    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        data = json.load(f)
    sp = data.get("system_prompt", {})
    return json.dumps(sp, ensure_ascii=False) if isinstance(sp, dict) else str(sp)


def _load_user_prompt_text() -> str:
    with open(_EPIC_USER_PROMPT, encoding="utf-8") as f:
        return f.read()


def _load_plan() -> dict:
    if _PLAN_FILE.exists():
        with open(_PLAN_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_plan(plan: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_PLAN_FILE, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)


def _save_iteration(phase: int, iteration: int, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _CACHE_DIR / f"phase{phase}_iter{iteration:04d}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ── GPT Call ─────────────────────────────────────────────────

def _call_gpt(system_prompt: str, user_message: str, model: str) -> tuple[str, int, int]:
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
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

    # Log to Brain pseudo collection via cost_guard
    try:
        from cost_guard import log_usage
        log_usage(
            agent="GPT",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            assignment_id="epic_planning_v014",
            call_type="epic_planning",
        )
    except Exception:
        pass  # Don't block planning on logging failure

    return text, tokens_in, tokens_out


# ── Content Fulfillment ──────────────────────────────────────

def _fulfill_content_requests(result: dict) -> str:
    requests = result.get("content_requests", [])
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
                    if len(content) > 10000:
                        content = content[:10000] + "\n... (truncated at 10K)"
                    parts.append(f"=== {path} (reason: {reason}) ===\n{content}")
                except Exception as e:
                    parts.append(f"=== {path} — read error: {e} ===")
            else:
                parts.append(f"=== {path} — not found ===")
        elif req_type == "record":
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
                            parts.append(f"=== RECORD {path} ===\n{content}")
                            break
                    else:
                        parts.append(f"=== RECORD {path} — not found ===")
                else:
                    parts.append(f"=== RECORD {path} — collection not found ===")
            except Exception as e:
                parts.append(f"=== RECORD {path} — error: {e} ===")
    return "\n\n".join(parts) if parts else ""


# ── Brain Snapshot ───────────────────────────────────────────

def _brain_snapshot() -> str:
    try:
        from brain_snapshot import take_snapshot
        return take_snapshot()
    except Exception as e:
        return f"(Brain snapshot failed: {e})"


# ── Manifest Refresh ─────────────────────────────────────────

def _refresh_manifest():
    try:
        from manifest_generator import ManifestGenerator
        gen = ManifestGenerator()
        gen.generate()
        gen.save()
        print(f"[Planner] Manifest: {gen.file_count} files")
    except Exception as e:
        print(f"[Planner] Manifest refresh failed: {e}")


# ── Deduplication ────────────────────────────────────────────

def dedup_plan(plan: dict = None) -> tuple[dict, list[str]]:
    """AI-assisted deduplication of the cumulative plan.

    Uses gpt-4o-mini to semantically identify duplicates that string
    matching would miss. Cheap, fast, accurate enough for dedup.
    Returns (plan, warnings) — warnings are fed back to GPT as discipline.
    """
    if plan is None:
        plan = _load_plan()
    warnings = []

    for collection_key, id_key, name_key in [
        ("features", "feature_id", "name"),
        ("stories", "story_id", "title"),
        ("requirements", "req_id", "requirement"),
    ]:
        items = plan.get(collection_key, [])
        if isinstance(items, dict):
            items = list(items.values())
        if not items or len(items) < 2:
            continue

        # Build a compact list for the LLM
        compact = []
        for item in items:
            if isinstance(item, dict):
                compact.append({
                    "id": item.get(id_key, "?"),
                    "name": item.get(name_key, "?"),
                    "description": str(item.get("description", ""))[:100],
                })

        if len(compact) < 2:
            continue

        # Ask gpt-4o-mini to find duplicates
        merge_map = _ai_dedup(collection_key, compact)

        if merge_map:
            # Apply the merge map
            items_by_id = {item.get(id_key, ""): item for item in items if isinstance(item, dict)}
            to_remove = set()

            for keep_id, remove_ids in merge_map.items():
                if keep_id not in items_by_id:
                    continue
                keeper = items_by_id[keep_id]
                for rid in remove_ids:
                    if rid in items_by_id and rid != keep_id:
                        # Merge evidence from removed into keeper
                        removed = items_by_id[rid]
                        removed_ev = removed.get("evidence", "")
                        keeper_ev = keeper.get("evidence", "")
                        if removed_ev and removed_ev not in keeper_ev:
                            keeper["evidence"] = keeper_ev + "; " + removed_ev if keeper_ev else removed_ev
                        # Merge description if richer
                        if len(str(removed.get("description", ""))) > len(str(keeper.get("description", ""))):
                            keeper["description"] = removed["description"]
                        to_remove.add(rid)

            if to_remove:
                before = len(items)
                removed_names = [items_by_id[rid].get(name_key, rid) for rid in to_remove if rid in items_by_id]
                items = [i for i in items if isinstance(i, dict) and i.get(id_key, "") not in to_remove]
                msg = (f"DUPLICATE WARNING: You created {len(to_remove)} duplicate {collection_key} that were merged: "
                       f"{', '.join(removed_names[:10])}. Do not create new IDs for existing concepts. "
                       f"Edit the existing entry instead.")
                warnings.append(msg)
                print(f"[Dedup] {collection_key}: {before} -> {len(items)} (merged {len(to_remove)} duplicates)")

        plan[collection_key] = items

    _save_plan(plan)
    if warnings:
        print(f"[Dedup] {len(warnings)} warnings to feed back to GPT")
    return plan, warnings


def _ai_dedup(collection_name: str, items: list[dict]) -> dict:
    """Call gpt-4o-mini to identify semantic duplicates.

    Returns {keep_id: [remove_id1, remove_id2, ...]} merge map.
    Returns empty dict on failure.
    """
    import openai

    prompt = (
        f"You are a deduplication engine. Below is a list of {collection_name} from a software plan.\n"
        f"Identify items that are duplicates or near-duplicates (same concept, different wording or ID).\n"
        f"Return a JSON object where each key is the ID to KEEP (the richest/best version) and the value "
        f"is an array of IDs to REMOVE (the duplicates that should be merged into the keeper).\n"
        f"If there are no duplicates, return an empty object {{}}.\n"
        f"Only flag true semantic duplicates — items that describe the same capability.\n\n"
        f"Items:\n{json.dumps(items, indent=2)}\n\n"
        f"Return JSON only. No explanation."
    )

    try:
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        result = json.loads(response.choices[0].message.content)
        tok_in = response.usage.prompt_tokens
        tok_out = response.usage.completion_tokens
        print(f"[Dedup] AI dedup for {collection_name}: {tok_in + tok_out} tokens, {len(result)} merge groups")

        # Log dedup cost
        try:
            from cost_guard import log_usage
            log_usage(
                agent="GPT",
                model="gpt-4o-mini",
                tokens_in=tok_in,
                tokens_out=tok_out,
                assignment_id="epic_planning_v014",
                call_type="dedup",
            )
        except Exception:
            pass
        return result if isinstance(result, dict) else {}
    except Exception as e:
        print(f"[Dedup] AI dedup failed for {collection_name}: {e} — skipping")
        return {}


# ── Duplicate Detection ──────────────────────────────────────

def _detect_duplicates(plan: dict) -> list[str]:
    """Scan the cumulative plan for duplicate features, stories, and requirements.

    Duplicates are detected by:
    1. Exact ID collision (same feature_id, story_id, or req_id)
    2. Name similarity (same name under different IDs)
    Returns list of objection strings. Empty = clean.
    """
    objections = []

    for collection_key, id_key, name_key in [
        ("features", "feature_id", "name"),
        ("stories", "story_id", "title"),
        ("requirements", "req_id", "requirement"),
    ]:
        items = plan.get(collection_key, [])
        if isinstance(items, dict):
            items = list(items.values())

        # Check ID duplicates
        seen_ids = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get(id_key, "")
            if item_id in seen_ids:
                objections.append(
                    f"DUPLICATE ID in {collection_key}: '{item_id}' appears more than once. "
                    f"Edit the existing entry, do not create duplicates."
                )
            seen_ids[item_id] = item

        # Check name duplicates (fuzzy — lowercase stripped comparison)
        seen_names = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get(id_key, "")
            name = item.get(name_key, "").lower().strip()
            if not name:
                continue
            # Normalize common variations
            normalized = name.replace("-", " ").replace("_", " ").replace("  ", " ")
            if normalized in seen_names and seen_names[normalized] != item_id:
                objections.append(
                    f"DUPLICATE CONCEPT in {collection_key}: '{item.get(name_key)}' (ID: {item_id}) "
                    f"duplicates '{seen_names[normalized]}'. Edit the existing entry."
                )
            seen_names[normalized] = item_id

    return objections


# ── Iterative Phase Runner ───────────────────────────────────

def _run_phase(phase: int, phase_name: str, system_prompt: str, model: str,
               build_prompt_fn, validate_fn, merge_fn,
               max_iterations: int = 50) -> dict:
    """Run one phase to convergence.

    Args:
        build_prompt_fn(iteration, last_result, fulfilled_content) -> str
        validate_fn(result) -> list[str]  (objections, empty = pass)
        merge_fn(cumulative, incremental) -> dict
    """
    print(f"\n{'='*60}")
    print(f"  PHASE {phase}: {phase_name}")
    print(f"{'='*60}")

    plan = _load_plan()
    last_result = plan
    total_in = 0
    total_out = 0

    for i in range(1, max_iterations + 1):
        print(f"\n[Phase {phase}] Iteration {i}/{max_iterations}")

        # Build prompt
        fulfilled = ""
        prompt = build_prompt_fn(i, last_result, fulfilled)

        # Inner research loop
        research_round = 0
        result = None
        while research_round < 10:
            research_round += 1
            try:
                raw, tok_in, tok_out = _call_gpt(system_prompt, prompt, model)
                total_in += tok_in
                total_out += tok_out
            except Exception as e:
                print(f"[Phase {phase}] GPT error: {e}")
                break

            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[Phase {phase}] Invalid JSON")
                break

            # Check for content requests
            cr = result.get("content_requests", [])
            if cr and isinstance(cr, list) and len(cr) > 0:
                print(f"[Phase {phase}] GPT requesting {len(cr)} resources")
                for req in cr:
                    if isinstance(req, dict):
                        print(f"  -> {req.get('type','?')}: {req.get('path','?')}")
                fulfilled = _fulfill_content_requests(result)
                prompt = (
                    f"Phase {phase}, research round {research_round + 1}.\n"
                    f"Requested content:\n\n{fulfilled}\n\n"
                    f"Continue. Do not re-request what was already provided."
                )
                continue
            break

        if not isinstance(result, dict):
            continue

        # Save raw iteration
        _save_iteration(phase, i, result)

        # Validate
        # Phase-specific validation
        objections = validate_fn(result)

        # Universal duplicate detection on cumulative plan (all phases)
        dup_objections = _detect_duplicates(plan)
        if dup_objections:
            objections.extend(dup_objections)
        if objections:
            print(f"[Phase {phase}] Claude: {len(objections)} objections")
            for obj in objections[:5]:
                print(f"  - {obj[:120]}")
        else:
            print(f"[Phase {phase}] Claude: no objections")

        # Merge into cumulative plan
        plan = merge_fn(plan, result)
        _save_plan(plan)

        # Report
        print(f"[Phase {phase}] Tokens: {tok_in} in / {tok_out} out")

        # Converge if no objections and structure stable
        if not objections:
            prev_key = json.dumps(last_result.get(phase_name, {}), sort_keys=True) if last_result else ""
            curr_key = json.dumps(plan.get(phase_name, {}), sort_keys=True) if plan else ""
            if prev_key == curr_key and i > 1:
                print(f"[Phase {phase}] CONVERGED at iteration {i}")
                break
            elif i > 1:
                print(f"[Phase {phase}] No objections but structure changed — one more")

        # Inject Claude feedback for next iteration
        last_result = result
        last_result["_claude_feedback"] = objections

    print(f"[Phase {phase}] Total: {total_in} in / {total_out} out")
    return plan


# ── Phase 1: Features ───────────────────────────────────────

def run_phase1(system_prompt: str, model: str, max_iter: int = 50) -> dict:
    user_prompt = _load_user_prompt_text()
    manifest = _brain_snapshot()

    def build_prompt(iteration, last_result, fulfilled):
        feedback = last_result.get("_claude_feedback", []) if isinstance(last_result, dict) else []
        msg = (
            f"PHASE 1: Define the feature list with evidence.\n"
            f"Iteration {iteration}. Do not write stories or requirements yet — features only.\n"
            f"Every feature must have: feature_id, name, layer, capability, description, evidence "
            f"(manifest cite or ASSUMPTION: with justification), backend_dependencies, priority.\n\n"
            f"ASSIGNMENT:\n{user_prompt}\n\n"
            f"BRAIN MANIFEST:\n{manifest}\n"
        )
        if fulfilled:
            msg += f"\nREQUESTED CONTENT:\n{fulfilled}\n"
        if last_result and isinstance(last_result, dict) and last_result.get("features"):
            msg += f"\nLAST FEATURES:\n{json.dumps(last_result.get('features', []), indent=2, ensure_ascii=False)[:20000]}\n"
        if feedback:
            msg += "\nCLAUDE FEEDBACK (address all):\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
        return msg

    def validate(result):
        objections = []
        features = result.get("features", [])
        if isinstance(features, dict):
            features = list(features.values())
        if not features:
            objections.append("No features produced.")
            return objections
        for f in features:
            if not isinstance(f, dict):
                continue
            fid = f.get("feature_id", "?")
            if not f.get("evidence"):
                objections.append(f"{fid}: missing evidence field.")
            if not f.get("description") or len(str(f.get("description", ""))) < 20:
                objections.append(f"{fid}: description too thin.")
            if not f.get("layer"):
                objections.append(f"{fid}: missing layer (frontend/backend).")
        if not result.get("content_requests") and not any(
            "manifest" in str(f.get("evidence", "")).lower() for f in features if isinstance(f, dict)
        ):
            objections.append("RESEARCH: No content_requests and no manifest citations in evidence. Read the manifest.")
        return objections

    def merge(plan, result):
        plan = dict(plan)
        new_features = result.get("features", [])
        if isinstance(new_features, dict):
            new_features = list(new_features.values())
        if isinstance(plan.get("features"), dict):
            plan["features"] = list(plan["features"].values())
        existing = {f.get("feature_id", ""): f for f in plan.get("features", []) if isinstance(f, dict)}
        for f in new_features:
            if isinstance(f, dict):
                existing[f.get("feature_id", "")] = f
        plan["features"] = list(existing.values())
        for key in ["epics", "issues", "notes", "risk", "gate_recommendation", "content_requests"]:
            if key in result and result[key]:
                plan[key] = result[key]
        return plan

    return _run_phase(1, "features", system_prompt, model, build_prompt, validate, merge, max_iter)


# ── Phase 2: Stories per Feature ─────────────────────────────

def run_phase2(system_prompt: str, model: str, max_iter: int = 20, dedup_warnings: list[str] = None) -> dict:
    plan = _load_plan()
    features = plan.get("features", [])
    if isinstance(features, dict):
        features = list(features.values())
    manifest = _brain_snapshot()

    for feat in features:
        if not isinstance(feat, dict):
            continue
        fid = feat.get("feature_id", "?")
        fname = feat.get("name", fid)

        # Collect dependencies
        deps = feat.get("backend_dependencies", [])
        dep_features = [f for f in features if isinstance(f, dict) and f.get("feature_id") in deps]

        def build_prompt(iteration, last_result, fulfilled, _feat=feat, _deps=dep_features, _dw=dedup_warnings):
            feedback = last_result.get("_claude_feedback", []) if isinstance(last_result, dict) else []
            fid = _feat.get('feature_id')
            msg = (
                f"PHASE 2: Write stories for feature {fid}.\n"
                f"Feature: {json.dumps(_feat, indent=2, ensure_ascii=False)}\n"
                f"Iteration {iteration}. Write 3-5 stories for this feature.\n"
                f"Each story: story_id, title, description, parent_feature, sprint, dependencies, size, evidence.\n"
                f"CRITICAL: parent_feature MUST be exactly '{fid}'. Do not invent a different ID.\n"
                f"Minimum 3 stories. Break into: schema/contract, core logic, API/UI, tests, integration.\n\n"
            )
            if _dw:
                msg += "DEDUP WARNINGS FROM PRIOR PHASE (do not repeat these mistakes):\n"
                msg += "\n".join(f"- {w}" for w in _dw) + "\n\n"
            if _deps:
                msg += f"DEPENDENCIES:\n{json.dumps(_deps, indent=2, ensure_ascii=False)[:5000]}\n\n"
            msg += f"BRAIN MANIFEST:\n{manifest}\n"
            if fulfilled:
                msg += f"\nREQUESTED CONTENT:\n{fulfilled}\n"
            if last_result and isinstance(last_result, dict) and last_result.get("stories"):
                msg += f"\nLAST STORIES:\n{json.dumps(last_result.get('stories', []), indent=2, ensure_ascii=False)[:10000]}\n"
            if feedback:
                msg += "\nCLAUDE FEEDBACK:\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
            return msg

        def validate(result, _fid=fid):
            objections = []
            stories = result.get("stories", [])
            if isinstance(stories, dict):
                stories = list(stories.values())
            # Reject stories with wrong parent_feature
            wrong_parent = [s for s in stories if isinstance(s, dict) and s.get("parent_feature") != _fid]
            if wrong_parent:
                bad_ids = [s.get("story_id", "?") for s in wrong_parent[:5]]
                objections.append(
                    f"WRONG PARENT: {len(wrong_parent)} stories have parent_feature != '{_fid}': "
                    f"{', '.join(bad_ids)}. parent_feature MUST be exactly '{_fid}'."
                )
            mine = [s for s in stories if isinstance(s, dict) and s.get("parent_feature") == _fid]
            if len(mine) < 3:
                objections.append(f"Only {len(mine)} stories for {_fid}. Minimum 3.")
            for s in mine:
                sid = s.get("story_id", "?")
                if not s.get("evidence"):
                    objections.append(f"{sid}: missing evidence.")
                if not s.get("size"):
                    objections.append(f"{sid}: missing size (S/M/L).")
            return objections

        def merge(plan_dict, result, _fid=fid):
            plan_dict = dict(plan_dict)
            new_stories = result.get("stories", [])
            if isinstance(new_stories, dict):
                new_stories = list(new_stories.values())
            existing = {s.get("story_id", ""): s for s in plan_dict.get("stories", []) if isinstance(s, dict)}
            for s in new_stories:
                if isinstance(s, dict):
                    existing[s.get("story_id", "")] = s
            plan_dict["stories"] = list(existing.values())
            return plan_dict

        print(f"\n--- Feature: {fid} ({fname}) ---")
        _run_phase(2, "stories", system_prompt, model, build_prompt, validate, merge, max_iter)

    return _load_plan()


# ── Phase 3: Requirements per Story ──────────────────────────

def run_phase3(system_prompt: str, model: str, max_iter: int = 10, dedup_warnings: list[str] = None) -> dict:
    plan = _load_plan()
    stories = plan.get("stories", [])
    if isinstance(stories, dict):
        stories = list(stories.values())
    features = plan.get("features", [])
    if isinstance(features, dict):
        features = list(features.values())
    manifest = _brain_snapshot()

    for story in stories:
        if not isinstance(story, dict):
            continue
        sid = story.get("story_id", "?")
        stitle = story.get("title", sid)
        parent_fid = story.get("parent_feature", "")
        parent_feat = next((f for f in features if isinstance(f, dict) and f.get("feature_id") == parent_fid), {})
        epic_goal = ""
        for e in plan.get("epics", []):
            if isinstance(e, dict):
                epic_goal = e.get("goal", "")
                break

        def build_prompt(iteration, last_result, fulfilled, _story=story, _feat=parent_feat, _goal=epic_goal, _dw=dedup_warnings):
            feedback = last_result.get("_claude_feedback", []) if isinstance(last_result, dict) else []
            msg = (
                f"PHASE 3: Write boolean requirements for story {_story.get('story_id')}.\n"
                f"Story: {json.dumps(_story, indent=2, ensure_ascii=False)}\n"
                f"Parent feature: {json.dumps(_feat, indent=2, ensure_ascii=False)}\n"
                f"Epic goal: {_goal[:500]}\n\n"
                f"Iteration {iteration}. Write boolean testable requirements.\n"
                f"Each: req_id, story_id, requirement ('The system must...'), source, priority, label (blank until UAT).\n"
                f"Every requirement must trace to the epic goal.\n"
                f"Procedural stories: all happy paths + 2 exception paths minimum.\n"
                f"LLM behavior stories: minimum 10 semantically different requirements.\n\n"
            )
            if _dw:
                msg += "DEDUP WARNINGS FROM PRIOR PHASE (do not repeat these mistakes):\n"
                msg += "\n".join(f"- {w}" for w in _dw) + "\n\n"
            msg += f"BRAIN MANIFEST:\n{manifest}\n"
            if fulfilled:
                msg += f"\nREQUESTED CONTENT:\n{fulfilled}\n"
            if feedback:
                msg += "\nCLAUDE FEEDBACK:\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
            return msg

        def validate(result, _sid=sid):
            objections = []
            reqs = result.get("requirements", [])
            if isinstance(reqs, dict):
                reqs = list(reqs.values())
            mine = [r for r in reqs if isinstance(r, dict) and r.get("story_id") == _sid]
            if len(mine) < 3:
                objections.append(f"Only {len(mine)} requirements for {_sid}. Need at least 3.")
            for r in mine:
                rid = r.get("req_id", "?")
                req_text = r.get("requirement", "")
                if not req_text.lower().startswith("the system must"):
                    objections.append(f"{rid}: must start with 'The system must...'")
                if not r.get("priority"):
                    objections.append(f"{rid}: missing priority.")
            return objections

        def merge(plan_dict, result):
            plan_dict = dict(plan_dict)
            new_reqs = result.get("requirements", [])
            if isinstance(new_reqs, dict):
                new_reqs = list(new_reqs.values())
            existing = {r.get("req_id", ""): r for r in plan_dict.get("requirements", []) if isinstance(r, dict)}
            for r in new_reqs:
                if isinstance(r, dict):
                    existing[r.get("req_id", "")] = r
            plan_dict["requirements"] = list(existing.values())
            return plan_dict

        print(f"\n--- Story: {sid} ({stitle}) ---")
        _run_phase(3, "requirements", system_prompt, model, build_prompt, validate, merge, max_iter)

    return _load_plan()


# ── Phase 4: Sprint Map ──────────────────────────────────────

def run_phase4(system_prompt: str, model: str, max_iter: int = 20) -> dict:
    plan = _load_plan()
    manifest = _brain_snapshot()

    def build_prompt(iteration, last_result, fulfilled):
        feedback = last_result.get("_claude_feedback", []) if isinstance(last_result, dict) else []
        msg = (
            f"PHASE 4: Build the sprint capability map.\n"
            f"Iteration {iteration}.\n"
            f"Features: {json.dumps(plan.get('features', []), indent=2, ensure_ascii=False)[:15000]}\n\n"
            f"Stories: {json.dumps(plan.get('stories', []), indent=2, ensure_ascii=False)[:15000]}\n\n"
            f"5 sprints: 1-4 development, 5 UAT+release.\n"
            f"Every sprint delivers at least one complete feature.\n"
            f"All stories in a feature land in the same sprint.\n"
            f"No sprint overloaded beyond one engineer per week.\n"
            f"Account for pipeline execution time.\n\n"
        )
        if fulfilled:
            msg += f"REQUESTED CONTENT:\n{fulfilled}\n"
        if last_result and isinstance(last_result, dict) and last_result.get("sprint_capability_map"):
            msg += f"\nLAST MAP:\n{json.dumps(last_result.get('sprint_capability_map'), indent=2, ensure_ascii=False)[:10000]}\n"
        if feedback:
            msg += "\nCLAUDE FEEDBACK:\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
        return msg

    def validate(result):
        objections = []
        smap = result.get("sprint_capability_map", [])
        if isinstance(smap, dict):
            smap = list(smap.values())
        if len(smap) < 5:
            objections.append(f"Only {len(smap)} sprints. Need 5.")
        for s in smap:
            if isinstance(s, dict):
                shipped = s.get("features_shipped", [])
                sprint_num = s.get("sprint", "?")
                if str(sprint_num) in ("1", "2", "3", "4") and isinstance(shipped, list) and len(shipped) == 0:
                    objections.append(f"Sprint {sprint_num} ships zero features.")
        return objections

    def merge(plan_dict, result):
        plan_dict = dict(plan_dict)
        if "sprint_capability_map" in result:
            plan_dict["sprint_capability_map"] = result["sprint_capability_map"]
        return plan_dict

    return _run_phase(4, "sprint_capability_map", system_prompt, model, build_prompt, validate, merge, max_iter)


# ── Phase 5: Risk Matrix + Gate ──────────────────────────────

def run_phase5(system_prompt: str, model: str, max_iter: int = 20) -> dict:
    plan = _load_plan()

    def build_prompt(iteration, last_result, fulfilled):
        feedback = last_result.get("_claude_feedback", []) if isinstance(last_result, dict) else []
        msg = (
            f"PHASE 5: Risk matrix and gate recommendation.\n"
            f"Iteration {iteration}.\n"
            f"Features: {json.dumps(plan.get('features', []), indent=2, ensure_ascii=False)[:10000]}\n\n"
            f"Risk matrix has two dimensions:\n"
            f"- Per feature (both epics): likelihood, impact, mitigation (must not expand scope)\n"
            f"- Per measure (EPIC-1 only): data_source_availability, data_quality, credibility, mitigation\n\n"
            f"Gate recommendation: overall risk + recommendation + issues array.\n"
            f"Risk mitigations may not expand scope. If mitigation needs unauthorized scope, mark for Boss escalation.\n"
        )
        if fulfilled:
            msg += f"REQUESTED CONTENT:\n{fulfilled}\n"
        if feedback:
            msg += "\nCLAUDE FEEDBACK:\n" + "\n".join(f"- {f}" for f in feedback) + "\n"
        return msg

    def validate(result):
        objections = []
        rm = result.get("risk_matrix", {})
        if not isinstance(rm, dict) or not rm:
            objections.append("No risk matrix.")
            return objections
        if "per_feature" not in rm:
            objections.append("Missing per_feature risk dimension.")
        if "per_measure" not in rm:
            objections.append("Missing per_measure risk dimension.")
        if not result.get("gate_recommendation"):
            objections.append("Missing gate_recommendation.")
        return objections

    def merge(plan_dict, result):
        plan_dict = dict(plan_dict)
        if "risk_matrix" in result:
            plan_dict["risk_matrix"] = result["risk_matrix"]
        if "gate_recommendation" in result:
            plan_dict["gate_recommendation"] = result["gate_recommendation"]
        if "issues" in result:
            plan_dict["issues"] = result["issues"]
        if "risk" in result:
            plan_dict["risk"] = result["risk"]
        return plan_dict

    return _run_phase(5, "risk_matrix", system_prompt, model, build_prompt, validate, merge, max_iter)


# ── Orchestrator ─────────────────────────────────────────────

def run(max_iterations: int = 50, start_phase: int = 1):
    _refresh_manifest()

    system_prompt = _load_system_prompt()
    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        model = json.load(f).get("model", "gpt-5.3-chat-latest")

    print(f"[Planner] Model: {model}")
    print(f"[Planner] Max iterations per phase: {max_iterations}")
    print(f"[Planner] Starting from phase: {start_phase}")

    dedup_warnings = []

    if start_phase <= 1:
        run_phase1(system_prompt, model, max_iterations)
        print("[Planner] Dedup after Phase 1...")
        _, warnings = dedup_plan()
        dedup_warnings.extend(warnings)
    if start_phase <= 2:
        run_phase2(system_prompt, model, max_iterations, dedup_warnings)
        print("[Planner] Dedup after Phase 2...")
        _, warnings = dedup_plan()
        dedup_warnings = warnings  # reset — only carry forward latest warnings
    if start_phase <= 3:
        run_phase3(system_prompt, model, max_iterations, dedup_warnings)
        print("[Planner] Dedup after Phase 3...")
        _, warnings = dedup_plan()
        dedup_warnings = warnings
    if start_phase <= 4:
        run_phase4(system_prompt, model, max_iterations)
    if start_phase <= 5:
        run_phase5(system_prompt, model, max_iterations)

    plan = _load_plan()
    def ct(v):
        if isinstance(v, (dict, list)): return len(v)
        return 0

    print(f"\n{'='*60}")
    print(f"  EPIC PLAN COMPLETE")
    print(f"  Features:     {ct(plan.get('features', []))}")
    print(f"  Stories:      {ct(plan.get('stories', []))}")
    print(f"  Requirements: {ct(plan.get('requirements', []))}")
    print(f"  Sprint map:   {ct(plan.get('sprint_capability_map', []))}")
    print(f"  Risk matrix:  {'present' if plan.get('risk_matrix') else 'MISSING'}")
    print(f"  Gate:         {plan.get('gate_recommendation', 'MISSING')}")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatHealthy.ai Phased Epic Planning")
    parser.add_argument("--max-iterations", type=int, default=50, help="Max iterations per phase")
    parser.add_argument("--phase", type=int, default=1, help="Start from phase (1-5)")
    args = parser.parse_args()
    run(max_iterations=args.max_iterations, start_phase=args.phase)
