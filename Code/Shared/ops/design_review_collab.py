# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# design_review_collab.py — GPT design review for BIZOPS-CHATLOG-001.
#
# Claude Code calls GPT, reviews GPT's response, responds, repeats.
# Claude IS the engineer — no Anthropic API call needed.
#
# Usage: Called iteratively by Claude Code. Each call = one GPT turn.
#   python design_review_collab.py --iteration N --claude-response "..."

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
BRAIN_DIR = PROJECT_ROOT / "brain"
BRAIN_CONTENT = BRAIN_DIR / "machine_artifacts" / "content"
OUTPUT_DIR = BRAIN_DIR / "machine_artifacts" / ".iteration_cache"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TRANSCRIPT_PATH = OUTPUT_DIR / "design_review_transcript.json"


def load_brain_context() -> str:
    context_parts = []
    for json_file in sorted(BRAIN_CONTENT.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            context_parts.append(f"=== {json_file.name} ===\n{json.dumps(data, indent=2, default=str, ensure_ascii=False)[:8000]}")
        except Exception as e:
            context_parts.append(f"=== {json_file.name} === ERROR: {e}")
    return "\n\n".join(context_parts)


def load_transcript() -> dict:
    if TRANSCRIPT_PATH.exists():
        return json.loads(TRANSCRIPT_PATH.read_text(encoding="utf-8"))
    return {
        "title": "BIZOPS-CHATLOG-001 Design Review Transcript",
        "feature": "OPS-AGENT-COMMS",
        "story": "BIZOPS-CHATLOG-001",
        "gpt_model": "gpt-5.3-chat-latest",
        "max_iterations": 15,
        "completed_iterations": 0,
        "status": "in_progress",
        "total_tokens": {"input": 0, "output": 0},
        "parking_lot": [],
        "transcript": [],
    }


def save_transcript(data: dict):
    data["updated"] = datetime.now(timezone.utc).isoformat()
    TRANSCRIPT_PATH.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")


def call_gpt(messages: list) -> tuple:
    import openai
    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    response = client.chat.completions.create(
        model="gpt-5.3-chat-latest",
        messages=messages,
    )
    text = response.choices[0].message.content
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


def build_gpt_messages(transcript_data: dict, claude_response: str = None) -> list:
    """Build GPT message history from transcript + optional new Claude response."""
    system_path = BRAIN_CONTENT / "gpt_design_review_system_prompt.json"
    system_data = json.loads(system_path.read_text(encoding="utf-8"))
    gpt_system = system_data["system_prompt"]

    user_path = BRAIN_CONTENT / "gpt_design_review_user_prompt.json"
    user_data = json.loads(user_path.read_text(encoding="utf-8"))
    user_prompt = user_data["user_prompt"]

    brain_context = load_brain_context()
    full_user_prompt = f"## Brain Context\n\n{brain_context}\n\n---\n\n{user_prompt}"

    messages = [
        {"role": "system", "content": gpt_system},
        {"role": "user", "content": full_user_prompt},
    ]

    # Replay transcript
    for entry in transcript_data.get("transcript", []):
        if entry["speaker"] == "GPT":
            messages.append({"role": "assistant", "content": entry["content"]})
        elif entry["speaker"] == "Claude":
            messages.append({"role": "user", "content": entry["content"]})

    # Add new Claude response if provided
    if claude_response:
        messages.append({"role": "user", "content": claude_response})

    return messages


def run_gpt_iteration(claude_response: str = None) -> str:
    """Run one GPT iteration. Returns GPT's response text."""
    transcript = load_transcript()
    iteration = transcript["completed_iterations"] + 1

    if claude_response:
        transcript["transcript"].append({
            "iteration": iteration,
            "speaker": "Claude",
            "content": claude_response,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    messages = build_gpt_messages(transcript, claude_response if not claude_response else None)

    # If we already added claude_response to transcript, rebuild from transcript
    if claude_response:
        messages = build_gpt_messages(transcript)

    print(f"Calling GPT — iteration {iteration}...")
    gpt_text, tin, tout = call_gpt(messages)

    transcript["transcript"].append({
        "iteration": iteration,
        "speaker": "GPT",
        "tokens_in": tin,
        "tokens_out": tout,
        "content": gpt_text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    transcript["completed_iterations"] = iteration
    transcript["total_tokens"]["input"] += tin
    transcript["total_tokens"]["output"] += tout

    save_transcript(transcript)
    print(f"GPT iteration {iteration}: {tin:,} in / {tout:,} out")
    return gpt_text


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-response", type=str, default=None,
                        help="Claude's response to feed to GPT (reads from stdin if '-')")
    args = parser.parse_args()

    claude_resp = args.claude_response
    if claude_resp == "-":
        claude_resp = sys.stdin.read()

    result = run_gpt_iteration(claude_resp)
    print("\n" + "=" * 70)
    print(result)
