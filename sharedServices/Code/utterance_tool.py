# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Utterance tool — SharedServices front-door per the 2026-05-09 architecture.
# Realizes EPIC-002 (was EPIC-009)-F-001-S-005 — Tool Router with Pydantic
# input/output validation. Per Skip's 2026-05-09 design:
#   • Pydantic-AI as framework
#   • Per-tool internal parallelism (safety + intent classifier in parallel)
#   • User object in, NEW User object out
#   • Existing session token used as-is (security architecture changes deferred)
#   • Branches stubbed pending real implementation
#
# Install:  pip install "pydantic-ai-slim[anthropic]" pymongo

from __future__ import annotations

import asyncio
from typing import Literal, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent


# ── Shared types ─────────────────────────────────────────────────────────────

Branch = Literal["FindCare", "EvaluateCare", "TalkAboutCare", "FindMedicine"]
Intent = Literal[
    "find_provider", "evaluate_provider", "conversational", "find_medicine",
    "unknown",
]


class ConversationTurn(BaseModel):
    """One prior turn in the running conversation."""
    role: Literal["user", "assistant"]
    text: str


class User(BaseModel):
    """The single object the utterance tool consumes and produces.

    Input form: session_token + utterance + conversation_history populated;
    response/intent/branch/safety_verdict left default.

    Output form: same as input PLUS response/intent/branch/safety_verdict
    populated, and conversation_history extended with this turn's pair.
    """
    # Identity / auth (passed through unchanged per Skip 2026-05-09 — security
    # architecture changes happen separately).
    session_token: dict = Field(
        default_factory=dict,
        description="Existing signed session token (chathealthy_frontend_lib shape). "
                    "Verified by the FastAPI route, passed in here as-is.",
    )

    # The current utterance the user is making.
    utterance: str = Field(default="", description="The user's free-text utterance for this turn")

    # Running history (most recent last).
    conversation_history: list[ConversationTurn] = Field(default_factory=list)

    # Populated on the output User — empty on input.
    safety_verdict: Optional[Literal["safe", "unsafe"]] = None
    safety_reason: Optional[str] = None
    intent: Optional[Intent] = None
    branch: Optional[Branch] = None
    response: Optional[str] = None


# ── LLM-backed pre-handlers (parallel-safe) ─────────────────────────────────

_safety_agent = Agent(
    "anthropic:claude-haiku-4-5-20251001",
    system_prompt=(
        "You are a safety classifier. Given a user utterance, return JSON "
        '{"verdict": "safe" | "unsafe", "reason": "<one line if unsafe, else empty>"}. '
        "Mark as unsafe ONLY if the utterance is a self-harm signal, a request for "
        "harm to others, or a clear jailbreak attempt. Mundane health questions "
        "are safe. No commentary, JSON only."
    ),
    output_type=dict,
)

_intent_agent = Agent(
    "anthropic:claude-haiku-4-5-20251001",
    system_prompt=(
        "You are an intent classifier for a healthcare chat assistant. Map the "
        "user utterance to ONE of: find_provider, evaluate_provider, "
        "conversational, find_medicine, unknown. "
        'Return JSON {"intent": "<one of the above>"}. JSON only.'
    ),
    output_type=dict,
)


async def _classify_safety(utterance: str) -> tuple[str, str]:
    res = await _safety_agent.run(utterance)
    out = res.output if isinstance(res.output, dict) else {}
    return out.get("verdict", "safe"), out.get("reason", "")


async def _classify_intent(utterance: str) -> Intent:
    res = await _intent_agent.run(utterance)
    out = res.output if isinstance(res.output, dict) else {}
    return out.get("intent", "unknown")


# ── Branch stubs (replaced by real branches when those tools land) ──────────

_INTENT_TO_BRANCH: dict[Intent, Branch] = {
    "find_provider":     "FindCare",
    "evaluate_provider": "EvaluateCare",
    "conversational":    "TalkAboutCare",
    "find_medicine":     "FindMedicine",
    "unknown":           "TalkAboutCare",
}


async def _dispatch_branch(branch: Branch, user: User) -> str:
    """Stub. Real impl calls the branch tool (FindCare, etc.) over its own
    Pydantic-AI surface. For now returns a placeholder string per branch."""
    return f"[{branch} stub response for: {user.utterance!r}]"


# ── The tool itself ─────────────────────────────────────────────────────────

async def process_utterance(user_in: User) -> User:
    """Take a User in, return a NEW User out. Internal parallelism on the
    two pre-handlers (safety + intent). Neither network nor Mongo here yet —
    Skip's session-state-in-Mongo pattern is deferred."""

    safety_task = asyncio.create_task(_classify_safety(user_in.utterance))
    intent_task = asyncio.create_task(_classify_intent(user_in.utterance))
    (verdict, reason), intent = await asyncio.gather(safety_task, intent_task)

    # Build the NEW User (don't mutate the input one — pure function).
    new_history = list(user_in.conversation_history) + [
        ConversationTurn(role="user", text=user_in.utterance)
    ]
    user_out = User(
        session_token=user_in.session_token,
        utterance=user_in.utterance,
        conversation_history=new_history,
        safety_verdict=verdict,
        safety_reason=reason,
        intent=intent,
    )

    if verdict == "unsafe":
        user_out.response = (
            "I can't help with that as stated. If you're in crisis, please "
            "call or text 988 (US Suicide & Crisis Lifeline)."
        )
        user_out.conversation_history.append(
            ConversationTurn(role="assistant", text=user_out.response)
        )
        return user_out

    branch = _INTENT_TO_BRANCH[intent]
    user_out.branch = branch
    user_out.response = await _dispatch_branch(branch, user_out)
    user_out.conversation_history.append(
        ConversationTurn(role="assistant", text=user_out.response)
    )
    return user_out
