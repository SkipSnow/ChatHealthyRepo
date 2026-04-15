# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# test_brain_questions.py — NOT A PYTEST.
#
# This is a manual GPT exploration script that calls the OpenAI API in a loop
# to validate brain data retrieval. It is not a deterministic test:
#   - Requires live OPENAI_API_KEY
#   - Calls GPT-5.3 (non-deterministic)
#   - No assertions — prints results and checks for expected values
#   - Costs real money per run
#
# AUDIT FIX 2026-04-15: Renamed all functions/classes to avoid pytest
# collection. Added __all__ = [] and removed any test-like naming.
# Original script preserved below for manual use.
#
# Usage: python Code/Shared/ops/test_brain_questions.py

__all__ = []  # Prevent pytest from importing this module's contents

import json
import os
import sys


def _run_brain_question_exploration():
    """Manual exploration script — NOT a test."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
    import openai

    content_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "brain", "machine_artifacts", "content")

    # Load system prompt
    with open(os.path.join(content_dir, "ai_operations.json"), "r", encoding="utf-8") as f:
        ops = json.load(f)
    sp = None
    for r in ops["records"]:
        if r.get("_record_id") == "gpt_assignment_system_prompt":
            sp = json.dumps(r["system_prompt"])
            break

    # Load relevant brain data
    brain_data = []
    for coll_name in ["business_plan", "architecture", "sprint_plan"]:
        fpath = os.path.join(content_dir, coll_name + ".json")
        with open(fpath, "r", encoding="utf-8") as f:
            coll = json.load(f)
        for r in coll.get("records", []):
            rid = r.get("_record_id", "")
            keywords = ["fund", "invest", "discuss", "sprint_plan", "business_model", "cost", "staffing"]
            if any(kw in rid for kw in keywords):
                entry = {"_record_id": rid, "collection": coll_name, "description": r.get("description", "")}
                for k in ["funding_model", "funding_milestone", "pricing_model", "seed_amount", "seed_date",
                           "amount", "target_date", "beta_sprint_1", "evaluate_care_epic"]:
                    if k in r:
                        entry[k] = r[k]
                brain_data.append(json.dumps(entry, default=str)[:600])

    user_prompt = (
        "TWO QUESTIONS. Answer both in one JSON response.\n\n"
        "Q1: How much money must ChatHealthy.ai raise and when? "
        "Research the brain data below. Find every reference to funding amount and date. "
        "If conflicts exist, list all values with source.\n\n"
        "Q2: How many tokens did this system prompt cost you? Report exact number.\n\n"
        "BRAIN DATA:\n" + "\n".join(brain_data) + "\n\n"
        "Return JSON: {\"funding_answer\": {\"amount\": <int>, \"date\": \"<str>\", "
        "\"sources_found\": [{\"collection\": \"\", \"record_id\": \"\", \"value\": \"\"}], "
        "\"conflicts\": []}, \"system_prompt_token_count\": <int>}"
    )

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    for i in range(5):
        response = client.chat.completions.create(
            model="gpt-5.3-chat-latest",
            messages=[{"role": "system", "content": sp}, {"role": "user", "content": user_prompt}],
        )
        text = response.choices[0].message.content
        tin = response.usage.prompt_tokens
        tout = response.usage.completion_tokens

        try:
            result = json.loads(text)
            fa = result.get("funding_answer", {})
            amt = fa.get("amount", "?")
            dt = fa.get("date", "?")
            sources = fa.get("sources_found", [])
            conflicts = fa.get("conflicts", [])
            sptc = result.get("system_prompt_token_count", "?")

            amt_str = f"${amt:,}" if isinstance(amt, (int, float)) else str(amt)
            print(f"=== Iteration {i+1} ({tin} in / {tout} out) ===")
            print(f"Amount: {amt_str}")
            print(f"Date: {dt}")
            print(f"Sources: {len(sources)}")
            for s in sources:
                print(f"  - {s.get('collection', '?')}/{s.get('record_id', '?')} = {s.get('value', '?')}")
            print(f"Conflicts: {conflicts if conflicts else 'none'}")
            print(f"System prompt tokens (GPT says): {sptc}")
            print()

            if isinstance(amt, (int, float)) and amt == 1500000 and "07-31" in str(dt):
                print(f"CORRECT on iteration {i+1}.")
                break
            elif i < 4:
                print("Wrong. Retrying...")
        except Exception as e:
            print(f"Iteration {i+1}: parse error - {e}")
            print(text[:300])

    with open(os.path.join(content_dir, "funding_test_result.json"), "w", encoding="utf-8") as f:
        f.write(text)
    print("Result saved")


if __name__ == "__main__":
    _run_brain_question_exploration()
