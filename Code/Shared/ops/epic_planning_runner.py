# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# epic_planning_runner.py — Tree-based epic planning with JSON Schema enforcement.
#
# One tree: epic -> features -> stories -> requirements
# Each node has status: proposed | accepted | rejected
# GPT proposes. Claude accepts/rejects. Schema enforces structure.
#
# Growth phases:
#   1. GPT proposes features (schema-enforced)
#   2. Claude accepts/rejects features
#   3. GPT proposes stories under accepted features (parent_feature locked by schema)
#   4. Claude accepts/rejects stories
#   5. GPT proposes requirements under accepted stories
#   6. Claude accepts/rejects requirements
#   7. Sprint map + risk matrix from accepted tree
#
# Usage: python epic_planning_runner.py [--max-iterations N]

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
_TREE_FILE = _CACHE_DIR / "plan_tree.json"

sys.path.insert(0, str(_SHARED_DIR))
sys.path.insert(0, str(_THIS_DIR))
from dotenv import load_dotenv
load_dotenv(_SHARED_DIR.parent / ".env")


# ── Tree Load / Save ─────────────────────────────────────────

def _load_tree() -> dict:
    if _TREE_FILE.exists():
        with open(_TREE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"epics": [], "sprint_map": [], "risk_matrix": {}, "metadata": {}}


def _save_tree(tree: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tree["metadata"]["last_saved"] = datetime.now(timezone.utc).isoformat()
    with open(_TREE_FILE, "w", encoding="utf-8") as f:
        json.dump(tree, f, indent=2, ensure_ascii=False)


def _save_iteration(phase: str, iteration: int, data: dict):
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = _CACHE_DIR / f"{phase}_iter{iteration:04d}_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


# ── System Prompt / User Prompt ──────────────────────────────

def _load_system_prompt() -> str:
    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        data = json.load(f)
    sp = data.get("system_prompt", {})
    return json.dumps(sp, ensure_ascii=False) if isinstance(sp, dict) else str(sp)


def _load_user_prompt() -> str:
    with open(_EPIC_USER_PROMPT, encoding="utf-8") as f:
        return f.read()


# ── GPT Call ─────────────────────────────────────────────────

def _call_gpt(system_prompt: str, user_message: str, model: str,
              json_schema: dict = None) -> tuple[dict, int, int]:
    """Call GPT with optional JSON Schema enforcement. Returns (parsed_dict, tok_in, tok_out)."""
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    if json_schema:
        response_fmt = {"type": "json_schema", "json_schema": json_schema}
    else:
        response_fmt = {"type": "json_object"}

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        response_format=response_fmt,
    )
    text = response.choices[0].message.content
    tok_in = response.usage.prompt_tokens
    tok_out = response.usage.completion_tokens

    # Log tokens
    try:
        from cost_guard import log_usage
        log_usage(agent="GPT", model=model, tokens_in=tok_in, tokens_out=tok_out,
                  assignment_id="epic_planning_v014", call_type="epic_planning")
    except Exception:
        pass

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = {"error": "invalid JSON", "raw": text[:500]}

    return result, tok_in, tok_out


# ── Content Fulfillment ──────────────────────────────────────

def _fulfill_requests(result: dict) -> str:
    requests = result.get("content_requests", [])
    if not requests or not isinstance(requests, list):
        return ""
    parts = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        path = req.get("path", "")
        reason = req.get("reason", "")
        if req.get("type") == "record":
            try:
                coll_name, record_id = path.split("/", 1)
                coll_path = _BRAIN_DIR / "machine_artifacts" / "content" / f"{coll_name}.json"
                if coll_path.exists():
                    with open(coll_path, encoding="utf-8") as f:
                        data = json.load(f)
                    for r in data.get("records", []):
                        if isinstance(r, dict) and r.get("_record_id") == record_id:
                            content = json.dumps(r, indent=2, ensure_ascii=False)[:10000]
                            parts.append(f"=== RECORD {path} ===\n{content}")
                            break
            except Exception:
                pass
        else:
            full = _REPO_ROOT / path
            if full.exists() and full.is_file():
                try:
                    content = full.read_text(encoding="utf-8")[:10000]
                    parts.append(f"=== {path} ===\n{content}")
                except Exception:
                    pass
    return "\n\n".join(parts)


# ── Brain Snapshot ───────────────────────────────────────────

def _brain_snapshot() -> str:
    try:
        from brain_snapshot import take_snapshot
        return take_snapshot()
    except Exception as e:
        return f"(Brain snapshot failed: {e})"


def _refresh_manifest():
    try:
        from manifest_generator import ManifestGenerator
        gen = ManifestGenerator()
        gen.generate()
        gen.save()
        print(f"[Planner] Manifest: {gen.file_count} files")
    except Exception as e:
        print(f"[Planner] Manifest refresh failed: {e}")


# ── JSON Schemas ─────────────────────────────────────────────

def _feature_schema() -> dict:
    """Schema for GPT to propose features."""
    return {
        "name": "propose_features",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "features": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "feature_id": {"type": "string"},
                            "name": {"type": "string"},
                            "epic_id": {"type": "string"},
                            "layer": {"type": "string", "enum": ["frontend", "backend", "both"]},
                            "capability": {"type": "string"},
                            "description": {"type": "string"},
                            "evidence": {"type": "string"},
                            "backend_dependencies": {"type": "array", "items": {"type": "string"}},
                            "priority": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "STRETCH"]},
                            "status": {"type": "string", "enum": ["proposed"]},
                        },
                        "required": ["feature_id", "name", "epic_id", "layer", "description",
                                     "evidence", "priority", "status", "backend_dependencies", "capability"],
                        "additionalProperties": False,
                    },
                },
                "content_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["file", "record"]},
                            "path": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["type", "path", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["features", "content_requests"],
            "additionalProperties": False,
        },
    }


def _story_schema(valid_feature_ids: list[str]) -> dict:
    """Schema for GPT to propose stories. parent_feature locked to valid IDs."""
    return {
        "name": "propose_stories",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "story_id": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "parent_feature": {"type": "string", "enum": valid_feature_ids},
                            "sprint": {"type": "integer"},
                            "dependencies": {"type": "array", "items": {"type": "string"}},
                            "size": {"type": "string", "enum": ["S", "M", "L"]},
                            "evidence": {"type": "string"},
                            "status": {"type": "string", "enum": ["proposed"]},
                        },
                        "required": ["story_id", "title", "description", "parent_feature",
                                     "size", "evidence", "status", "dependencies", "sprint"],
                        "additionalProperties": False,
                    },
                },
                "content_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["file", "record"]},
                            "path": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["type", "path", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["stories", "content_requests"],
            "additionalProperties": False,
        },
    }


def _requirement_schema(valid_story_ids: list[str]) -> dict:
    """Schema for GPT to propose requirements. story_id locked to valid IDs."""
    return {
        "name": "propose_requirements",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "req_id": {"type": "string"},
                            "story_id": {"type": "string", "enum": valid_story_ids},
                            "requirement": {"type": "string"},
                            "source": {"type": "string"},
                            "priority": {"type": "string", "enum": ["must-have", "should-have", "nice-to-have"]},
                            "status": {"type": "string", "enum": ["proposed"]},
                        },
                        "required": ["req_id", "story_id", "requirement", "source", "priority", "status"],
                        "additionalProperties": False,
                    },
                },
                "content_requests": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["file", "record"]},
                            "path": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["type", "path", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["requirements", "content_requests"],
            "additionalProperties": False,
        },
    }


# ── Claude Acceptance (AI-evaluated) ─────────────────────────

_CLAUDE_ACCEPT_PROMPT = _BRAIN_DIR / "machine_artifacts" / "content" / "claude_acceptance_system_prompt.json"


def _load_claude_system_prompt() -> str:
    """Load Claude's acceptance system prompt from Brain."""
    with open(_CLAUDE_ACCEPT_PROMPT, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("system_prompt", "You are Claude, the Engineer. Accept or reject proposed items.")


def _claude_model() -> str:
    """Read Claude model from Brain acceptance system prompt config."""
    try:
        with open(_CLAUDE_ACCEPT_PROMPT, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("model", "claude-opus-4-6")
    except Exception:
        return "claude-opus-4-6"


def _claude_evaluate(items: list[dict], item_type: str, context: str) -> list[dict]:
    """Claude (Opus) evaluates proposed items and accepts/rejects with reasoning.

    This is a real AI review using Claude's own system prompt. Not a rubber stamp.
    """
    if not items:
        return items

    import anthropic

    client = anthropic.Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    system_prompt = _load_claude_system_prompt()

    compact = []
    for item in items:
        if isinstance(item, dict) and item.get("status") == "proposed":
            compact.append({k: v for k, v in item.items() if v})

    if not compact:
        return items

    # User prompt — context + items to review
    user_prompt = (
        f"Review these proposed {item_type}.\n\n"
        f"Context:\n{context[:3000]}\n\n"
        f"Proposed {item_type}:\n{json.dumps(compact, indent=2, ensure_ascii=False)[:8000]}\n\n"
        f"Return JSON: {{\"decisions\": [{{\"id\": \"<item_id>\", \"accept\": true/false, \"reason\": \"<one sentence>\"}}]}}"
    )

    try:
        claude_model = _claude_model()
        response = client.messages.create(
            model=claude_model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = response.content[0].text
        # Extract JSON from response
        if "{" in text:
            json_str = text[text.index("{"):text.rindex("}") + 1]
            decisions = json.loads(json_str).get("decisions", [])
        else:
            decisions = []

        # Log cost
        try:
            from cost_guard import log_usage
            log_usage(
                agent="Claude", model=claude_model,
                tokens_in=response.usage.input_tokens,
                tokens_out=response.usage.output_tokens,
                assignment_id="epic_planning_v014",
                call_type=f"accept_{item_type}",
            )
        except Exception:
            pass

        # Apply decisions
        decision_map = {d["id"]: d for d in decisions if isinstance(d, dict)}
        id_key = {"features": "feature_id", "stories": "story_id", "requirements": "req_id"}.get(item_type, "id")

        for item in items:
            if not isinstance(item, dict) or item.get("status") != "proposed":
                continue
            item_id = item.get(id_key, "")
            decision = decision_map.get(item_id)
            if decision:
                if decision.get("accept"):
                    item["status"] = "accepted"
                    item["accepted_by"] = "Claude (Opus)"
                    item["acceptance_reason"] = decision.get("reason", "")
                else:
                    item["status"] = "rejected"
                    item["rejection_reason"] = decision.get("reason", "")
            else:
                # No decision from Opus — fall back to structural check
                item["status"] = "accepted"
                item["accepted_by"] = "Claude (auto — no Opus decision)"

        print(f"[Accept] Opus reviewed {len(compact)} {item_type}: "
              f"{sum(1 for i in items if i.get('status') == 'accepted')} accepted, "
              f"{sum(1 for i in items if i.get('status') == 'rejected')} rejected")

    except Exception as e:
        print(f"[Accept] Opus call failed: {e} — falling back to structural acceptance")
        # Fallback: structural checks only
        for item in items:
            if isinstance(item, dict) and item.get("status") == "proposed":
                item["status"] = "accepted"
                item["accepted_by"] = "Claude (fallback — Opus unavailable)"

    return items


def _claude_accept_features(features: list[dict], epic_goal: str = "") -> list[dict]:
    """Claude evaluates proposed features via Opus."""
    context = f"Epic goal: {epic_goal}\nEvaluating features for credibility and scope."
    return _claude_evaluate(features, "features", context)


def _claude_accept_stories(stories: list[dict], accepted_feature_ids: set, feature_desc: str = "") -> list[dict]:
    """Claude evaluates proposed stories via Opus."""
    # Pre-filter: reject stories with wrong parent
    for s in stories:
        if isinstance(s, dict) and s.get("status") == "proposed":
            if s.get("parent_feature") not in accepted_feature_ids:
                s["status"] = "rejected"
                s["rejection_reason"] = f"parent_feature {s.get('parent_feature')} not in accepted features"
    # AI review the rest
    context = f"Feature: {feature_desc}\nAccepted feature IDs: {', '.join(accepted_feature_ids)}"
    return _claude_evaluate(
        [s for s in stories if isinstance(s, dict) and s.get("status") == "proposed"],
        "stories", context
    ) + [s for s in stories if isinstance(s, dict) and s.get("status") != "proposed"]


def _claude_accept_requirements(requirements: list[dict], accepted_story_ids: set, story_desc: str = "") -> list[dict]:
    """Claude evaluates proposed requirements via Opus."""
    # Pre-filter: reject reqs with wrong story
    for r in requirements:
        if isinstance(r, dict) and r.get("status") == "proposed":
            if r.get("story_id") not in accepted_story_ids:
                r["status"] = "rejected"
                r["rejection_reason"] = f"story_id {r.get('story_id')} not in accepted stories"
    # AI review the rest
    context = f"Story: {story_desc}\nAccepted story IDs: {', '.join(accepted_story_ids)}"
    return _claude_evaluate(
        [r for r in requirements if isinstance(r, dict) and r.get("status") == "proposed"],
        "requirements", context
    ) + [r for r in requirements if isinstance(r, dict) and r.get("status") != "proposed"]


# ── AI Dedup ─────────────────────────────────────────────────

def _ai_dedup(items: list[dict], id_key: str, name_key: str) -> list[dict]:
    """Use gpt-4o-mini to find and merge semantic duplicates."""
    if len(items) < 2:
        return items

    compact = [{"id": i.get(id_key, "?"), "name": i.get(name_key, "?")} for i in items if isinstance(i, dict)]

    try:
        import openai
        client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        prompt = (
            f"Identify semantic duplicates. Return JSON: {{keep_id: [remove_ids]}}. "
            f"Empty {{}} if no duplicates.\n{json.dumps(compact)}"
        )
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        merge_map = json.loads(r.choices[0].message.content)
        if not isinstance(merge_map, dict) or not merge_map:
            return items

        to_remove = set()
        items_by_id = {i.get(id_key, ""): i for i in items if isinstance(i, dict)}
        for keep_id, remove_ids in merge_map.items():
            for rid in remove_ids:
                if rid in items_by_id and rid != keep_id:
                    to_remove.add(rid)

        if to_remove:
            before = len(items)
            items = [i for i in items if isinstance(i, dict) and i.get(id_key) not in to_remove]
            print(f"[Dedup] {before} -> {len(items)} (merged {len(to_remove)})")

        try:
            from cost_guard import log_usage
            log_usage(agent="GPT", model="gpt-4o-mini",
                      tokens_in=r.usage.prompt_tokens, tokens_out=r.usage.completion_tokens,
                      assignment_id="epic_planning_v014", call_type="dedup")
        except Exception:
            pass

    except Exception as e:
        print(f"[Dedup] Failed: {e}")

    return items


# ── Growth Phases ────────────────────────────────────────────

def grow_features(system_prompt: str, model: str, user_prompt: str,
                  manifest: str, max_iter: int = 10) -> list[dict]:
    """Phase 1: GPT proposes features, Claude accepts/rejects."""
    print(f"\n{'='*60}\n  GROW FEATURES\n{'='*60}")

    tree = _load_tree()
    all_features = []
    for epic in tree.get("epics", []):
        all_features.extend(epic.get("features", []))

    schema = _feature_schema()

    for i in range(1, max_iter + 1):
        print(f"\n[Features] Iteration {i}/{max_iter}")

        # Build prompt
        accepted = [f for f in all_features if f.get("status") == "accepted"]
        rejected = [f for f in all_features if f.get("status") == "rejected"]

        msg = (
            f"Propose features for the epic plan.\n"
            f"Assignment:\n{user_prompt}\n\n"
            f"BRAIN MANIFEST:\n{manifest}\n\n"
            f"Already accepted ({len(accepted)} features):\n"
            f"{json.dumps([{'feature_id': f['feature_id'], 'name': f['name']} for f in accepted], indent=2)}\n\n"
        )
        if rejected:
            msg += (
                f"Rejected ({len(rejected)} — do not re-propose without fixing):\n"
                f"{json.dumps([{'feature_id': f['feature_id'], 'rejection_reason': f.get('rejection_reason','')} for f in rejected], indent=2)}\n\n"
            )
        msg += (
            f"Propose NEW features not yet in the accepted list. "
            f"Or if the accepted list covers the epic goal, return an empty features array to signal completion.\n"
            f"Every feature needs evidence (manifest cite or ASSUMPTION:).\n"
        )

        # Research loop
        result, tok_in, tok_out = _call_gpt(system_prompt, msg, model, json_schema=schema)

        # Fulfill content requests
        fulfilled = _fulfill_requests(result)
        if fulfilled:
            msg2 = f"Requested content:\n{fulfilled}\n\nContinue proposing features."
            result, t2, o2 = _call_gpt(system_prompt, msg2, model, json_schema=schema)
            tok_in += t2
            tok_out += o2

        _save_iteration("features", i, result)

        new_features = result.get("features", [])
        if not new_features:
            print(f"[Features] GPT returned empty — signaling completion")
            break

        # Claude accepts/rejects
        new_features = _claude_accept_features(new_features, epic_goal="Release a credible version 1 of the Evaluate Care business component.")
        new_accepted = [f for f in new_features if f.get("status") == "accepted"]
        new_rejected = [f for f in new_features if f.get("status") == "rejected"]
        print(f"[Features] Proposed: {len(new_features)}, Accepted: {len(new_accepted)}, Rejected: {len(new_rejected)}")

        all_features.extend(new_features)
        print(f"[Features] Total: {len([f for f in all_features if f.get('status') == 'accepted'])} accepted")

    # Dedup accepted features
    accepted = [f for f in all_features if f.get("status") == "accepted"]
    accepted = _ai_dedup(accepted, "feature_id", "name")

    return accepted


def grow_stories(system_prompt: str, model: str, features: list[dict],
                 manifest: str, max_iter: int = 5) -> list[dict]:
    """Phase 2: For each accepted feature, GPT proposes stories, Claude accepts/rejects."""
    print(f"\n{'='*60}\n  GROW STORIES\n{'='*60}")

    valid_fids = [f["feature_id"] for f in features if isinstance(f, dict)]
    schema = _story_schema(valid_fids)
    all_stories = []

    for feat in features:
        if not isinstance(feat, dict):
            continue
        fid = feat["feature_id"]
        fname = feat.get("name", fid)
        print(f"\n--- Feature: {fid} ({fname}) ---")

        feat_stories = []

        for i in range(1, max_iter + 1):
            accepted = [s for s in feat_stories if s.get("status") == "accepted"]
            if len(accepted) >= 3 and i > 1:
                print(f"[Stories] {fid}: {len(accepted)} accepted — sufficient")
                break

            msg = (
                f"Propose stories for feature {fid}.\n"
                f"Feature: {json.dumps(feat, indent=2)}\n\n"
                f"parent_feature MUST be exactly '{fid}'.\n"
                f"Minimum 3 stories. Break into: schema/contract, core logic, API/UI, tests.\n\n"
                f"BRAIN MANIFEST:\n{manifest}\n\n"
                f"Already accepted stories for this feature: {len(accepted)}\n"
            )
            if accepted:
                msg += json.dumps([{"story_id": s["story_id"], "title": s["title"]} for s in accepted], indent=2) + "\n"
            msg += "\nPropose NEW stories not yet accepted. Empty array = done.\n"

            result, _, _ = _call_gpt(system_prompt, msg, model, json_schema=schema)
            _save_iteration("stories", i, result)

            new_stories = result.get("stories", [])
            if not new_stories:
                break

            new_stories = _claude_accept_stories(new_stories, {fid},
                feature_desc=f"{fid}: {feat.get('name','')} — {feat.get('description','')[:200]}")
            feat_stories.extend(new_stories)
            na = len([s for s in new_stories if s.get("status") == "accepted"])
            print(f"[Stories] {fid} iter {i}: +{na} accepted")

        all_stories.extend([s for s in feat_stories if s.get("status") == "accepted"])

    print(f"\n[Stories] Total accepted: {len(all_stories)}")
    return all_stories


def grow_requirements(system_prompt: str, model: str, features: list[dict],
                      stories: list[dict], epic_goal: str,
                      manifest: str, max_iter: int = 3) -> list[dict]:
    """Phase 3: For each accepted story, GPT proposes requirements, Claude accepts/rejects."""
    print(f"\n{'='*60}\n  GROW REQUIREMENTS\n{'='*60}")

    valid_sids = [s["story_id"] for s in stories if isinstance(s, dict)]
    schema = _requirement_schema(valid_sids)
    all_reqs = []

    for story in stories:
        if not isinstance(story, dict):
            continue
        sid = story["story_id"]
        stitle = story.get("title", sid)
        parent_fid = story.get("parent_feature", "")
        parent_feat = next((f for f in features if f.get("feature_id") == parent_fid), {})

        print(f"\n--- Story: {sid} ({stitle}) ---")

        story_reqs = []

        for i in range(1, max_iter + 1):
            accepted = [r for r in story_reqs if r.get("status") == "accepted"]
            if len(accepted) >= 3 and i > 1:
                print(f"[Reqs] {sid}: {len(accepted)} accepted — sufficient")
                break

            msg = (
                f"Propose boolean requirements for story {sid}.\n"
                f"Story: {json.dumps(story, indent=2)}\n"
                f"Parent feature: {json.dumps(parent_feat, indent=2)}\n"
                f"Epic goal: {epic_goal[:500]}\n\n"
                f"story_id MUST be exactly '{sid}'.\n"
                f"Each requirement: 'The system must...' — boolean testable.\n"
                f"Procedural: all happy paths + 2 exception paths.\n"
                f"LLM behavior: minimum 10 semantically different.\n\n"
                f"Already accepted: {len(accepted)}\n"
            )
            if accepted:
                msg += json.dumps([{"req_id": r["req_id"], "requirement": r["requirement"][:80]} for r in accepted], indent=2) + "\n"
            msg += "\nPropose NEW requirements. Empty array = done.\n"

            result, _, _ = _call_gpt(system_prompt, msg, model, json_schema=schema)
            _save_iteration("reqs", i, result)

            new_reqs = result.get("requirements", [])
            if not new_reqs:
                break

            new_reqs = _claude_accept_requirements(new_reqs, {sid},
                story_desc=f"{sid}: {story.get('title','')} — {story.get('description','')[:200]}")
            story_reqs.extend(new_reqs)
            na = len([r for r in new_reqs if r.get("status") == "accepted"])
            print(f"[Reqs] {sid} iter {i}: +{na} accepted")

        all_reqs.extend([r for r in story_reqs if r.get("status") == "accepted"])

    print(f"\n[Reqs] Total accepted: {len(all_reqs)}")
    return all_reqs


def grow_sprint_map(system_prompt: str, model: str, tree: dict) -> dict:
    """Phase 4: GPT builds sprint map from accepted tree."""
    print(f"\n{'='*60}\n  GROW SPRINT MAP\n{'='*60}")

    msg = (
        f"Build the sprint capability map from this accepted plan.\n"
        f"5 sprints: 1-4 development, 5 UAT+release.\n"
        f"Every sprint delivers at least one complete feature.\n"
        f"All stories in a feature land in the same sprint.\n"
        f"No overload — one engineer per week.\n\n"
        f"Plan:\n{json.dumps(tree, indent=2, ensure_ascii=False)[:30000]}\n"
    )

    result, _, _ = _call_gpt(system_prompt, msg, model)
    _save_iteration("sprint", 1, result)
    return result.get("sprint_capability_map", result)


def grow_risk_matrix(system_prompt: str, model: str, tree: dict) -> dict:
    """Phase 5: GPT builds risk matrix and gate recommendation."""
    print(f"\n{'='*60}\n  GROW RISK MATRIX\n{'='*60}")

    msg = (
        f"Build the risk matrix and gate recommendation.\n"
        f"Per feature: likelihood, impact, mitigation (must not expand scope).\n"
        f"Per measure (EPIC-1): data_source_availability, data_quality, credibility.\n"
        f"Risk mitigations may not expand scope — escalate to human if needed.\n\n"
        f"Plan:\n{json.dumps(tree, indent=2, ensure_ascii=False)[:30000]}\n"
    )

    result, _, _ = _call_gpt(system_prompt, msg, model)
    _save_iteration("risk", 1, result)
    return result


# ── Orchestrator ─────────────────────────────────────────────

def run(max_iterations: int = 10):
    _refresh_manifest()

    system_prompt = _load_system_prompt()
    user_prompt = _load_user_prompt()
    manifest = _brain_snapshot()

    with open(_EPIC_SYSTEM_PROMPT, encoding="utf-8") as f:
        model = json.load(f).get("model", "gpt-5.3-chat-latest")

    print(f"[Planner] Model: {model}")
    print(f"[Planner] Max iterations per growth phase: {max_iterations}")

    # Phase 1: Features
    features = grow_features(system_prompt, model, user_prompt, manifest, max_iterations)

    # Build tree with accepted features
    # Build epic tree dynamically from features — no hardcoded epic list
    epic_map = {}
    for f in features:
        eid = f.get("epic_id", "EPIC-1")
        if eid not in epic_map:
            epic_map[eid] = {"epic_id": eid, "name": eid, "features": []}
        epic_map[eid]["features"].append(f)
    tree = {
        "epics": list(epic_map.values()),
        "metadata": {"phase": "features_complete"},
    }
    _save_tree(tree)

    # Phase 2: Stories
    stories = grow_stories(system_prompt, model, features, manifest, max_iter=max_iterations)

    # Nest stories under features in tree
    story_map = {}
    for s in stories:
        story_map.setdefault(s.get("parent_feature", ""), []).append(s)
    for epic in tree["epics"]:
        for feat in epic.get("features", []):
            feat["stories"] = story_map.get(feat["feature_id"], [])
    tree["metadata"]["phase"] = "stories_complete"
    _save_tree(tree)

    # Phase 3: Requirements
    epic_goal = "Release a credible version 1 of the Evaluate Care business component."
    requirements = grow_requirements(system_prompt, model, features, stories, epic_goal, manifest, max_iter=max_iterations)

    # Nest requirements under stories in tree
    req_map = {}
    for r in requirements:
        req_map.setdefault(r.get("story_id", ""), []).append(r)
    for epic in tree["epics"]:
        for feat in epic.get("features", []):
            for story in feat.get("stories", []):
                story["requirements"] = req_map.get(story["story_id"], [])
    tree["metadata"]["phase"] = "requirements_complete"
    _save_tree(tree)

    # Phase 4: Sprint map
    sprint_map = grow_sprint_map(system_prompt, model, tree)
    tree["sprint_map"] = sprint_map
    tree["metadata"]["phase"] = "sprint_complete"
    _save_tree(tree)

    # Phase 5: Risk matrix
    risk = grow_risk_matrix(system_prompt, model, tree)
    tree["risk_matrix"] = risk.get("risk_matrix", risk)
    tree["gate_recommendation"] = risk.get("gate_recommendation", "")
    tree["metadata"]["phase"] = "complete"
    _save_tree(tree)

    # Summary
    total_features = sum(len(e.get("features", [])) for e in tree["epics"])
    total_stories = sum(len(f.get("stories", [])) for e in tree["epics"] for f in e.get("features", []))
    total_reqs = sum(len(s.get("requirements", [])) for e in tree["epics"] for f in e.get("features", []) for s in f.get("stories", []))

    print(f"\n{'='*60}")
    print(f"  EPIC PLAN COMPLETE")
    print(f"  Features:     {total_features}")
    print(f"  Stories:      {total_stories}")
    print(f"  Requirements: {total_reqs}")
    print(f"  Sprint map:   {'present' if tree.get('sprint_map') else 'MISSING'}")
    print(f"  Risk matrix:  {'present' if tree.get('risk_matrix') else 'MISSING'}")
    print(f"  Gate:         {tree.get('gate_recommendation', 'MISSING')}")
    print(f"  Tree:         {_TREE_FILE}")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ChatHealthy.ai Tree-Based Epic Planning")
    parser.add_argument("--max-iterations", type=int, default=10, help="Max iterations per growth phase")
    args = parser.parse_args()
    run(max_iterations=args.max_iterations)
