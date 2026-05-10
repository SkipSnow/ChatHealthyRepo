"""One-shot migration: send each existing engineering rule to GPT-4.1
with structured output, and write a NEW file that conforms to the
EngineeringRulesSchema.

Per directive 2026-04-19. Per Idiot Mode plan approval. Output
goes to Code/_pending/engineering_rules_redesign/output/engineering_rules.NEW.json.
The live brain/machine_artifacts/content/engineering_rules.json is NOT
overwritten by this script.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "Code" / "Shared"))

# Load Code/.env so OPENAI_API_KEY is available before llm_client imports.
try:
    from dotenv import load_dotenv  # noqa: E402
    load_dotenv(REPO / "Code" / ".env")
except ImportError:
    pass

from llm_client import chat  # noqa: E402

OLD_FILE = REPO / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
OUT_DIR = REPO / "Code" / "_pending" / "engineering_rules_redesign" / "output"
OUT_FILE = OUT_DIR / "engineering_rules.NEW.json"
MAPPING_FILE = OUT_DIR / "id_mapping.json"
SCHEMA_FILE = REPO / "Website" / "schemas" / "dev" / "EngineeringRulesSchema.json"

MODEL = "gpt-4.1"  # not mini

# Strict-mode response_format schema for ONE rule (without id/req_id —
# we assign id sequentially; req_id placeholder added post-hoc since
# GPT cannot invent real backlog IDs).
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "description", "rule_statements"],
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 80,
            "description": "Unique short label for the rule (<=80 chars).",
        },
        "description": {
            "type": "string",
            "description": "Non-binding prose description of the rule.",
        },
        "rule_statements": {
            "type": "object",
            "additionalProperties": False,
            "required": ["rule_statement"],
            "properties": {
                "rule_statement": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["statement_number", "statement_body"],
                        "properties": {
                            "statement_number": {
                                "type": "integer",
                                "minimum": 1,
                            },
                            "statement_body": {
                                "type": "string",
                                "minLength": 1,
                            },
                        },
                    },
                }
            },
        },
    },
}


def _system_prompt(target_schema: dict) -> str:
    return (
        "You are migrating ChatHealthy engineering rules from an old prose "
        "shape to a structured shape that MUST conform exactly to the JSON "
        "Schema below. The target structure has three fields per rule:\n"
        "  - name: <=80 chars, unique, human-readable label\n"
        "  - description: non-binding prose summary of the rule\n"
        "  - rule_statements.rule_statement[]: each statement is a "
        "BOOLEAN-TESTABLE assertion derived from the original rule body.\n\n"
        "YOU DECIDE how to decompose the prose body into rule_statements: "
        "how many statements, where to split, and exactly what each "
        "statement says. Use your judgment, with two constraints:\n"
        "  (1) every statement_body must be boolean-testable (answerable "
        "true/false against the codebase or runtime behavior);\n"
        "  (2) every binding obligation present in the source must appear "
        "in some statement; do not drop or summarize away any constraint.\n\n"
        "Number statements sequentially starting at 1.\n\n"
        "DO NOT think about enforcement. The 'enforcement' field on the "
        "input rule is preserved EXACTLY AS IS by code outside this prompt; "
        "you must ignore it and do not produce any 'enforcement' field in "
        "your output.\n\n"
        "TARGET SCHEMA (response_format will enforce this strictly):\n"
        + json.dumps(target_schema, indent=2)
    )


def _user_prompt(old_rule: dict) -> str:
    # Send only the fields GPT should reason about. Strip enforcement so
    # GPT cannot accidentally restructure it; the carrier code reattaches
    # the original verbatim after the call.
    payload = {k: v for k, v in old_rule.items() if k != "enforcement"}
    return (
        "Migrate this single engineering rule. The 'rule' field is the old "
        "prose body to decompose into rule_statements. The 'title' is the "
        "old short name. Return only the structured object — do not include "
        "an 'enforcement' field; that is preserved separately.\n\n"
        + json.dumps(payload, indent=2)
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with OLD_FILE.open(encoding="utf-8") as f:
        old = json.load(f)
    old_rules = old.get("rules", [])
    print(f"Loaded {len(old_rules)} old rules")

    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "EngineeringRule",
            "strict": True,
            "schema": RESPONSE_SCHEMA,
        },
    }

    new_rules = []
    mapping = []
    for idx, old_rule in enumerate(old_rules, start=1):
        new_id = f"Rule-{idx:03d}"
        old_id = old_rule.get("id", "")
        try:
            resp = chat(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _system_prompt(RESPONSE_SCHEMA)},
                    {"role": "user", "content": _user_prompt(old_rule)},
                ],
                max_tokens=2000,
                response_format=response_format,
                temperature=0,
            )
        except Exception as e:
            print(f"  {old_id} -> {new_id}: ERROR {e}")
            continue

        # Extract content from the chat response
        content = resp.get("content")
        if isinstance(content, list):
            text = "".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        else:
            text = content if isinstance(content, str) else ""

        try:
            parsed = json.loads(text)
        except Exception as e:
            print(f"  {old_id} -> {new_id}: JSON parse ERROR {e}; raw: {text[:200]}")
            continue

        # Backfill req_id placeholder per statement (GPT cannot invent real IDs)
        for st in parsed["rule_statements"]["rule_statement"]:
            st["req_id"] = "TBD-MIGRATE"

        # Carry forward enforcement(s) from old rule
        old_enf = old_rule.get("enforcement")
        if isinstance(old_enf, dict):
            enforcements = {"enforcement": [old_enf]}
        elif isinstance(old_enf, list) and old_enf:
            enforcements = {"enforcement": old_enf}
        else:
            enforcements = {"enforcement": []}

        new_rule = {
            "id": new_id,
            "name": parsed["name"],
            "description": parsed["description"],
            "rule_statements": parsed["rule_statements"],
            "enforcements": enforcements,
        }
        new_rules.append(new_rule)
        mapping.append({"old_id": old_id, "new_id": new_id, "name": new_rule["name"]})
        print(f"  {old_id} -> {new_id}: {new_rule['name'][:60]}")

    new_doc = {
        "$schema": "https://dev.chathealthy.ai/schemas/dev/EngineeringRulesSchema.json",
        "rules": {"rule": new_rules},
    }

    with OUT_FILE.open("w", encoding="utf-8") as f:
        json.dump(new_doc, f, indent=2)
    with MAPPING_FILE.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2)
    print(f"\nWrote {OUT_FILE}")
    print(f"Wrote {MAPPING_FILE}")
    print(f"Migrated {len(new_rules)}/{len(old_rules)} rules")


if __name__ == "__main__":
    main()
