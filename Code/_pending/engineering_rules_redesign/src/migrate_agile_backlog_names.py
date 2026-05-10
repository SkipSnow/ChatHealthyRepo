"""Migrate agile_backlog.json to satisfy ChatHealthyAgileBacklogSchema.

Mechanical: rename every story.title -> story.name.
GPT-4.1 (structured output): derive a <=80 char name for every requirement
that lacks one.

Writes to a TEMP file (not the live file). Saves incrementally after every
single requirement so progress is visible by inspecting the temp file.
Prints flush per requirement so stdout is live.

Per directive 2026-04-19. Final promotion (rename temp -> live) is a
separate decision after the migrated file is reviewed.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "Code" / "Shared"))
try:
    from dotenv import load_dotenv
    load_dotenv(REPO / "Code" / ".env")
except ImportError:
    pass
from llm_client import chat  # noqa: E402

LIVE = REPO / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
TEMP = REPO / "Code" / "_pending" / "engineering_rules_redesign" / "output" / "agile_backlog.NEW.json"
MODEL = "gpt-4.1"

RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
        }
    },
}

SYS = (
    "You generate a concise, human-readable label (<= 80 characters) for a "
    "single agile-backlog requirement, derived from its requirement text. "
    "Do not invent obligations. Do not summarize away constraints. Output "
    "only the structured object with the 'name' field. Strict mode JSON Schema:\n"
    + json.dumps(RESPONSE_SCHEMA, indent=2)
)


def derive_name(req_text: str) -> str | None:
    resp = chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYS},
            {"role": "user", "content": req_text or "(empty)"},
        ],
        max_tokens=200,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "RequirementName",
                "strict": True,
                "schema": RESPONSE_SCHEMA,
            },
        },
        temperature=0,
    )
    content = resp.get("content")
    if isinstance(content, list):
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    else:
        text = content if isinstance(content, str) else ""
    try:
        return json.loads(text)["name"]
    except Exception:
        return None


def save(bl):
    TEMP.parent.mkdir(parents=True, exist_ok=True)
    with TEMP.open("w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2)


def main():
    with LIVE.open(encoding="utf-8") as f:
        bl = json.load(f)

    stories_renamed = 0
    reqs_named = 0
    reqs_failed = []

    for epic in bl["epics"]["epic"]:
        for feature in epic.get("features", {}).get("feature", []):
            for story in feature.get("stories", {}).get("story", []):
                if not story.get("name") and story.get("title"):
                    story["name"] = story.pop("title")
                    stories_renamed += 1
                for req in story.get("requirements", {}).get("requirement", []):
                    if req.get("name"):
                        continue
                    rid = req.get("req_id", "?")
                    name = derive_name(req.get("requirement", ""))
                    if name:
                        req["name"] = name
                        reqs_named += 1
                    else:
                        reqs_failed.append(rid)
                    # Save + print after EVERY requirement
                    save(bl)
                    print(f"  named={reqs_named}  stories_renamed={stories_renamed}  failed={len(reqs_failed)}  last={rid}",
                          flush=True)

    save(bl)
    print(f"\nDONE. Stories renamed: {stories_renamed}, Requirements named: {reqs_named}, Failed: {len(reqs_failed)}",
          flush=True)
    if reqs_failed:
        print("Failed req_ids (first 30):", flush=True)
        for rid in reqs_failed[:30]:
            print(f"  {rid}", flush=True)


if __name__ == "__main__":
    main()
