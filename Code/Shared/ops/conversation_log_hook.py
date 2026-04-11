#!/usr/bin/env python3
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# conversation_log_hook.py — Claude Code hook that appends utterances to conversation_log.json.
#
# Called automatically by Claude Code on UserPromptSubmit and Stop events.
# This is the enforcement mechanism for BR1 — Claude cannot forget to log.
#
# Events handled:
#   UserPromptSubmit — logs the user's prompt
#   Stop — reads transcript to capture Claude's final response

import json
import sys
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Resolve paths
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
PROMPT_LOG_PATH = PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"

MAX_CONTENT_LEN = 500
SYSTEM_REMINDER_PATTERN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def load_log() -> dict:
    if PROMPT_LOG_PATH.exists():
        return json.loads(PROMPT_LOG_PATH.read_text(encoding="utf-8"))
    return {
        "collection": "conversation_log",
        "path": "brain/machine_artifacts/content/conversation_log.json",
        "purpose": "Rolling 24h conversation log — every prompt and response. This is an operational log, not a compliance or regulatory audit log.",
        "produces_artifact": False,
        "retention": "24_hours",
        "utterances": [],
    }


def save_log(log: dict):
    PROMPT_LOG_PATH.write_text(
        json.dumps(log, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def next_utterance_num(log: dict) -> int:
    utterances = log.get("utterances", [])
    return max((u["utterance"] for u in utterances), default=0) + 1


def clean_content(content: str) -> str:
    """Apply content rules: strip system-reminders, truncate, no binary."""
    if not content:
        return ""

    # TR16: exclude system-reminder content
    content = SYSTEM_REMINDER_PATTERN.sub("", content).strip()

    # TR9: binary detection
    if content.startswith("data:") or "base64," in content:
        return "[binary content omitted]"

    # TR11: truncate at 500 chars
    if len(content) > MAX_CONTENT_LEN:
        content = content[:MAX_CONTENT_LEN] + f" [truncated — {len(content)} chars]"

    return content


def make_timestamps():
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + timedelta(hours=-7)
    return (
        now_pst.strftime("%Y-%m-%dT%H:%M:%S-07:00"),
        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def append_utterance(actor: str, role: str, content: str):
    content = clean_content(content)
    if not content:
        return

    log = load_log()
    pst, utc = make_timestamps()

    log["utterances"].append({
        "utterance": next_utterance_num(log),
        "timestamp_pst": pst,
        "timestamp_utc": utc,
        "actor": actor,
        "role": role,
        "content": content,
    })

    save_log(log)


def handle_user_prompt_submit(hook_input: dict):
    """UserPromptSubmit: log the user's prompt."""
    prompt = hook_input.get("prompt", "")
    if prompt:
        append_utterance("Skip", "user", prompt)


def handle_stop(hook_input: dict):
    """Stop: read transcript to get Claude's last response."""
    transcript_path = hook_input.get("transcript_path", "")
    if not transcript_path or not Path(transcript_path).exists():
        return

    # Read the JSONL transcript and find the last assistant message
    lines = Path(transcript_path).read_text(encoding="utf-8").strip().split("\n")
    last_assistant_content = None

    for line in reversed(lines):
        try:
            entry = json.loads(line)
            # Look for assistant message type
            if entry.get("type") == "assistant" and entry.get("message"):
                msg = entry["message"]
                # Extract text content from message
                if isinstance(msg.get("content"), list):
                    text_parts = [
                        block.get("text", "")
                        for block in msg["content"]
                        if block.get("type") == "text"
                    ]
                    last_assistant_content = " ".join(text_parts).strip()
                elif isinstance(msg.get("content"), str):
                    last_assistant_content = msg["content"]
                break
        except (json.JSONDecodeError, KeyError):
            continue

    if last_assistant_content:
        append_utterance("Claude", "assistant", last_assistant_content)


def main():
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    event = hook_input.get("hook_event_name", "")

    if event == "UserPromptSubmit":
        handle_user_prompt_submit(hook_input)
    elif event == "Stop":
        handle_stop(hook_input)

    # Always exit 0 — never block Claude
    sys.exit(0)


if __name__ == "__main__":
    main()
