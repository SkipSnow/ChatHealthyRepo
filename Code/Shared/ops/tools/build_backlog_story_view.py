#!/usr/bin/env python3
# Derived view of agile_backlog.json pruned at story level.
# Non-schema-compliant by design: omits requirements/pytests/realized_by
# arrays. Intended as a lightweight context artifact so Claude can see
# the Epic→Feature→Story shape of the backlog without the full 521K
# tokens of requirement text.
#
# Output: test_output/backlog_stories.json
#
# Canonical source is untouched: brain/machine_artifacts/content/agile_backlog.json
import json
import os
import time
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", r"c:/chatHealthy/findCare"))
SRC = PROJECT / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
OUT = PROJECT / "test_output" / "backlog_stories.json"

STORY_KEYS = ("story_id", "title", "description", "parent_feature", "sprint",
              "size", "status", "accepted_by", "orphan", "approval")
FEATURE_KEYS = ("feature_id", "name", "epic_id", "layer", "capability",
                "description", "priority", "status", "accepted_by",
                "orphan", "approval")
EPIC_KEYS = ("epic_id", "name", "capacity", "goal", "definition_of_done",
             "status", "type", "description", "orphan", "approval")


def _pick(obj: dict, keys) -> dict:
    return {k: obj[k] for k in keys if k in obj}


def _children(obj, parent_key: str, singular: str):
    # Backlog uses the v4-032 plural/singular convention: the parent value
    # is either {singular: [...]} or a bare list. Handle both.
    val = obj.get(parent_key)
    if isinstance(val, dict):
        return val.get(singular, []) or []
    if isinstance(val, list):
        return val
    return []


def build():
    data = json.loads(SRC.read_text(encoding="utf-8"))
    epics_in = _children(data, "epics", "epic")
    view = {
        "_schema_compliant": False,
        "_source": "brain/machine_artifacts/content/agile_backlog.json",
        "_view": "story-level pruned view — no requirements, pytests, realized_by",
        "_generated_at": time.time(),
        "epics": [],
    }
    for epic in epics_in:
        e = _pick(epic, EPIC_KEYS)
        e["features"] = []
        for feat in _children(epic, "features", "feature"):
            f = _pick(feat, FEATURE_KEYS)
            f["stories"] = []
            for story in _children(feat, "stories", "story"):
                s = _pick(story, STORY_KEYS)
                # Count requirements without embedding them
                req_list = _children(story, "requirements", "requirement")
                s["requirement_count"] = len(req_list)
                f["stories"].append(s)
            e["features"].append(f)
        view["epics"].append(e)

    # Orphan catchall if present
    if "orphan_catchall" in data:
        oc = data["orphan_catchall"]
        view["orphan_catchall"] = _pick(oc, EPIC_KEYS)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(view, indent=2), encoding="utf-8")

    size = OUT.stat().st_size
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        toks = len(enc.encode(OUT.read_text(encoding="utf-8"), disallowed_special=()))
        method = "tiktoken cl100k_base"
    except Exception:
        toks = round(size / 4)
        method = "char/4 fallback"

    print(f"wrote {OUT}")
    print(f"  bytes : {size:,}")
    print(f"  tokens: {toks:,}  ({method})")
    print(f"  epics : {len(view['epics'])}")
    f_count = sum(len(e.get('features', [])) for e in view['epics'])
    s_count = sum(len(f.get('stories', []))
                  for e in view['epics'] for f in e.get('features', []))
    print(f"  feats : {f_count}")
    print(f"  stories: {s_count}")


if __name__ == "__main__":
    build()
