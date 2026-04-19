#!/usr/bin/env python3
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# riskWaiver_gate.py — PreToolUse hook.
#
# Consults GPT-4.1 with:
#   - the pending tool call (tool_name + tool_input)
#   - risk_acceptance.json (accepted risks, structured)
#   - the last 10 Boss↔Claude exchanges from the transcript
# and decides whether a recorded risk acceptance covers this pending
# action. Emits a Claude Code hook JSON response:
#
#   { "permissionDecision": "allow", "appliedRiskId": "RISK-NNN", "reason": "..." }
#   { "permissionDecision": "ask",                                   "reason": "..." }
#
# Fail-safe: any unparseable hook input, missing OPENAI_API_KEY,
# GPT timeout/error, or malformed GPT response -> "ask" (falls through
# to the normal user prompt). Never emits "deny" — this gate only
# speeds up approvals, it never blocks.
#
# Wiring: register in .claude/settings.json at hooks.PreToolUse, with
# a matcher appropriate to the tools you want gated (e.g. ".*" for all,
# or "Bash|Edit|Write" for the common destructive ones).

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()
RISK_ACCEPTANCE_PATH = PROJECT / "brain" / "machine_artifacts" / "content" / "risk_acceptance.json"
MODEL = "gpt-4.1"
EXCHANGES = 10                    # last N Boss↔Claude exchanges to include
GPT_TIMEOUT_SEC = 5.0             # hot-path budget


# ── helpers ──────────────────────────────────────────────────────────

def _emit(decision: str, reason: str = "", applied_risk_id: str | None = None) -> None:
    """Write a hook JSON response to stdout and exit 0."""
    payload: dict = {"permissionDecision": decision}
    if reason:
        payload["reason"] = reason
    if applied_risk_id:
        payload["appliedRiskId"] = applied_risk_id
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()
    sys.exit(0)


def _ask(reason: str = "") -> None:
    _emit("ask", reason)


def _load_hook_input() -> dict:
    try:
        return json.load(sys.stdin) if not sys.stdin.isatty() else {}
    except Exception:
        return {}


def _load_risk_acceptances() -> list[dict]:
    try:
        doc = json.loads(RISK_ACCEPTANCE_PATH.read_text(encoding="utf-8"))
        return list(doc.get("entries", []))
    except Exception:
        return []


def _load_recent_exchanges(transcript_path: str, n_exchanges: int) -> list[dict]:
    """Return the last n_exchanges * 2 (user+assistant) messages, oldest-first."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    target = n_exchanges * 2
    collected: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        role = rec.get("type") or rec.get("role")
        if role not in ("user", "assistant"):
            continue
        msg = rec.get("message") or rec
        content = msg.get("content") if isinstance(msg, dict) else rec.get("content")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"
            )
        if not isinstance(content, str):
            content = str(content)
        collected.append({"role": role, "content": content[:1200]})
        if len(collected) >= target:
            break
    collected.reverse()
    return collected


# ── GPT judge ────────────────────────────────────────────────────────

_SYSTEM = (
    "You are the ChatHealthy.LLC compliance officer. Every Claude Code "
    "tool call is gated through you. For each invocation you receive a "
    "context packet in the user message: "
    "(1) PENDING_TOOL_CALL — tool_name + tool_input the agent is about to run; "
    "(2) RISK_ACCEPTANCES — every formal acceptance on file (each entry "
    "is self-describing and carries a claude_stipulation that justifies "
    "or objects to the acceptance); "
    "(3) RECENT_EXCHANGES — the last 10 user↔agent exchanges for context. "
    "Be strict. "
    'Output JSON only: {"decision":"allow"|"ask","applied_risk_id":"<id or null>","reason":"<one sentence>"}. '
    "Allow ONLY when: (a) a risk acceptance plausibly covers this tool "
    "call by subject (file path, bash command, artifact), (b) the intent "
    "matches, (c) it is time-relevant (not a stale acceptance from a "
    "different arc), AND (d) the claude_stipulation on that acceptance "
    "does not object. Do NOT allow on vague coverage. If the stipulation "
    "raises any objection or caveat relevant to this call, ask. When in "
    "doubt, ask."
)


def _call_gpt(tool_name: str, tool_input: dict,
              acceptances: list[dict], exchanges: list[dict]) -> dict | None:
    try:
        from openai import OpenAI
    except ImportError:
        return None

    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT / "Code" / ".env")
    except ImportError:
        pass

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    client = OpenAI(api_key=api_key, timeout=GPT_TIMEOUT_SEC)

    # Structured context packet — assembled fresh every hook invocation.
    # The acceptance records define the roles (authors, stipulation); no
    # separate role block is needed.
    packet = {
        "PENDING_TOOL_CALL": {
            "tool_name": tool_name,
            "tool_input": tool_input,
        },
        "RISK_ACCEPTANCES": acceptances,
        "RECENT_EXCHANGES": exchanges,
    }
    user_payload = json.dumps(packet, ensure_ascii=False)
    if len(user_payload) > 60000:
        # Truncate exchanges first, then acceptances (preserve pending_call + roles).
        packet["RECENT_EXCHANGES"] = packet["RECENT_EXCHANGES"][-4:]
        user_payload = json.dumps(packet, ensure_ascii=False)
        if len(user_payload) > 60000:
            user_payload = user_payload[:60000]

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_payload},
            ],
            response_format={"type": "json_object"},
            timeout=GPT_TIMEOUT_SEC,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return None


# ── entry point ──────────────────────────────────────────────────────

def main() -> None:
    hook_input = _load_hook_input()
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {}) or {}
    transcript_path = hook_input.get("transcript_path", "")

    if not tool_name:
        _ask("no tool_name in hook input")

    acceptances = _load_risk_acceptances()
    if not acceptances:
        _ask("no risk acceptances on file")

    exchanges = _load_recent_exchanges(transcript_path, EXCHANGES)

    verdict = _call_gpt(tool_name, tool_input, acceptances, exchanges)
    if not verdict:
        _ask("gpt unavailable")

    decision = verdict.get("decision", "ask")
    reason = verdict.get("reason", "")
    applied = verdict.get("applied_risk_id")
    if decision == "allow":
        _emit("allow", reason=reason, applied_risk_id=applied)
    _ask(reason or "no applicable risk acceptance")


if __name__ == "__main__":
    main()
