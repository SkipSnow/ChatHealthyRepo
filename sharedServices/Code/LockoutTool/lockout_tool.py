# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LockoutTool — three-task safety-lockout dispatch.

UR dispatches this tool in two situations:
  1. Pre-UM path: UR's _hydrate_lockout_if_any stamped is_locked_out=True
     because the IP has an active record in {env}_Safety.emergency_incidents.
     LockoutTool's branch decides between Task A (operator backdoor unlock)
     and Task B (still-locked reminder).
  2. Post-UM path: UM emitted target_action=safetyLockout because the
     classifier judged the latest utterance signals immediate medical
     attention. LockoutTool runs Task C (new lockout).

Tasks
-----
A. user is locked + utterance == "unlock$123" (case-insensitive, exact
   match after strip+lower) — write {"unlocked": True, "unlocked_at",
   "unlocked_by"} on the emergency_incidents row, clear in-memory
   is_locked_out + lockout sub-object, stream a deterministic
   you-have-been-unlocked message, morph the IntentDocument to
   closeConnection200.

B. user is locked + utterance is anything else — render the
   deterministic "when you said '<utterance>' you were locked, it
   expires at X, call 911 or seek care ASAP" message from the in-memory
   user_object.lockout fields. Zero DB writes. Morph to
   closeConnection200.

C. user is NOT locked + UM emitted safetyLockout — insert a fresh
   {env}_Safety.emergency_incidents row (ip, locked_at, expires_at +
   1h, trigger_message, history, lockout_reason audit label), stamp
   user_object.is_locked_out=True + user_object.lockout, stream the
   verbatim "when you said '<utterance>'" + "call Skip @ 646 408 8999"
   message, morph to closeConnection200.

The unlock literal is hardcoded here per operator: "unlock$123",
exact match after `text.strip().lower()`. No env var; no .env entry.
"""
from __future__ import annotations

import asyncio
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import os
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from chathealthy_frontend_lib.authentication.agent_deps import AgentDeps, append_system_utterance
from chathealthy_frontend_lib.authentication.chathealthy_tool import ChatHealthyTool
from chathealthy_frontend_lib.authentication.user_object import Lockout

from chathealthy_frontend_lib.authentication.intent_document import (
    Argument,
    IntentCloseConnection200,
    IntentDocument,
)

log = ChatHealthyLoggingService()

# Hardcoded per operator. Exact match (no substring) after strip+lower.
UNLOCK_LITERAL = "unlock$123"
LOCKOUT_DURATION_SECONDS = 3600  # 1 hour
OPERATOR_PHONE = "646 408 8999"
OPERATOR_NAME = "Skip"


def env_prefix() -> str:
    return os.getenv("ENV_PREFIX", "dev")


def safety_collection(deps: AgentDeps):
    if deps.mongo_frontend is None:
        return None
    return deps.mongo_frontend[f"{env_prefix()}_Safety"]["emergency_incidents"]


def latest_person_utterance_text(deps: AgentDeps) -> str:
    for u in reversed(
        deps.user_object.session_conversation_history.utterances
    ):
        actor = getattr(u, "actor", None) or (
            u.get("actor") if isinstance(u, dict) else None
        )
        text = getattr(u, "text", None) or (
            u.get("text") if isinstance(u, dict) else ""
        )
        if actor == "person" and text:
            return str(text).strip()
    return ""


def is_unlock_literal(text: str) -> bool:
    return text.strip().lower() == UNLOCK_LITERAL


def morph_to_close_connection_200(deps: AgentDeps) -> None:
    """End-of-tool morph used by all three tasks. Mirrors NonsenseTool's
    pattern so UR's bounded dispatch loop chains to CloseConnection200Tool
    on the next hop."""
    document = deps.user_object.intent
    close_entry = IntentCloseConnection200(
        name="closeConnection200",
        arguments=[
            Argument(
                name="close_connection",
                value="true",
                type="boolean",
                required=True,
            ),
        ],
    )
    if document is None:
        new_doc = IntentDocument(
            target_action="closeConnection200",
            intents=[close_entry],
            user_message=None,
        )
    else:
        keep = [i for i in document.intents if i.name != "closeConnection200"]
        keep.append(close_entry)
        new_doc = IntentDocument(
            target_action="closeConnection200",
            intents=keep[-4:],
            user_message=None,
        )
    deps.user_object.intent = new_doc


def format_expires_phrase(expires_at: datetime) -> str:
    """Render 'in N minutes' / 'in N hours' / 'at HH:MM UTC' for the
    deterministic reminder text. Computed off `now`, not stored."""
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    remaining = expires_at - now
    secs = int(remaining.total_seconds())
    if secs <= 0:
        return "any moment now"
    if secs < 3600:
        mins = max(1, secs // 60)
        return f"in about {mins} minute{'s' if mins != 1 else ''}"
    hrs = secs // 3600
    return f"in about {hrs} hour{'s' if hrs != 1 else ''}"


class Request(BaseModel):
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    task: str  # 'A' | 'B' | 'C'
    unlocked: bool = False
    locked: bool = False


class LockoutTool(ChatHealthyTool):
    TOOL_NAME = "lockout_tool"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        utterance = latest_person_utterance_text(deps)
        was_locked = bool(deps.user_object.is_locked_out)

        if was_locked and is_unlock_literal(utterance):
            return await self._task_a_unlock(deps)
        if was_locked:
            return await self._task_b_reminder(deps)
        return await self._task_c_lockout(deps, utterance)

    async def _task_a_unlock(self, deps: AgentDeps) -> Response:
        ip = (deps.user_object.ip_address or "").strip()
        coll = safety_collection(deps)
        if coll is not None and ip:
            try:
                coll.update_one(
                    {"ip": ip, "unlocked": {"$ne": True}},
                    {"$set": {
                        "unlocked": True,
                        "unlocked_at": datetime.now(timezone.utc).isoformat(),
                        "unlocked_by": "tool_unlock_literal",
                    }},
                )
            except Exception as exc:
                # Mode 2 (REQ-B-008): unlock DB write failed. In-memory
                # clear below proceeds, so user sees "you have been
                # unlocked" — but next turn rehydrates the still-locked
                # row and the user is silently re-locked. (This is the
                # unlock-trust bug Skip flagged 2026-06-22.) Operator MUST
                # know. log.exception (ERROR+traceback) + always-log.
                log.exception("LockoutTool Task A update_one failed: %s", exc, exc=ChatHealthyException(
                                                                                mode="lockout_task_a_update_failed",
                                                                                message=f"LockoutTool Task A update_one failed: {exc}",
                                                                                component="LockoutTool",
                                                                                exception=exc,
                                                                            ), if_not_debug_log=True)
        # In-memory state clears regardless of DB outcome.
        deps.user_object.is_locked_out = False
        deps.user_object.lockout = None

        msg = (
            "You have been unlocked. The safety lock on this connection "
            "has been cleared. If you are or someone near you is having a "
            "medical emergency, please call 911 or your local emergency "
            "number immediately."
        )
        deps.stream({"kind": "prompt", "data": {"text": msg}})
        append_system_utterance(deps.user_object, msg)

        morph_to_close_connection_200(deps)
        await asyncio.sleep(0)
        return self.Response(task="A", unlocked=True)

    async def _task_b_reminder(self, deps: AgentDeps) -> Response:
        lockout = deps.user_object.lockout
        trigger = (lockout.trigger_utterance if lockout else "") or "your earlier message"
        expires_phrase = (
            format_expires_phrase(lockout.expires_at)
            if lockout else "soon"
        )
        msg = (
            f"You are still safety-locked. When you said \"{trigger}\" "
            "it was determined that you may need immediate medical "
            "attention. This safety lock expires "
            f"{expires_phrase}. If you or someone near you is in danger "
            "right now, please call 911. For non-emergency support, you "
            f"can also contact {OPERATOR_NAME} at {OPERATOR_PHONE}."
        )
        deps.stream({"kind": "prompt", "data": {"text": msg}})
        append_system_utterance(deps.user_object, msg)

        morph_to_close_connection_200(deps)
        await asyncio.sleep(0)
        return self.Response(task="B", locked=True)

    async def _task_c_lockout(self, deps: AgentDeps, utterance: str) -> Response:
        ip = (deps.user_object.ip_address or "").strip()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=LOCKOUT_DURATION_SECONDS)

        # Snapshot session utterances for audit + the in-memory Lockout sub-object.
        history_dump = []
        try:
            history_dump = [
                u.model_dump() if hasattr(u, "model_dump") else dict(u)
                for u in deps.user_object.session_conversation_history.utterances
            ]
        except Exception as _exc:
            # Mode 1 (REQ-B-008): conversation history dump failed; defaults
            # to []; lockout row still gets written. log.info + default debug.
            log.info("LockoutTool history dump failed (using []): %s", _exc, exc=ChatHealthyException(
                                                                                 mode="lockout_history_dump_failed",
                                                                                 message=f"LockoutTool history dump failed (using []): {_exc}",
                                                                                 component="LockoutTool",
                                                                                 exception=_exc,
                                                                             ))
            history_dump = []

        # Audit label from UM's IntentSafetyLockout arg, if present.
        lockout_reason = ""
        document = deps.user_object.intent
        if document is not None:
            entry = next(
                (i for i in document.intents if i.name == "safetyLockout"),
                None,
            )
            if entry is not None:
                lockout_reason = next(
                    (a.value for a in entry.arguments if a.name == "lockout_reason"),
                    "",
                )

        coll = safety_collection(deps)
        if coll is not None and ip:
            try:
                coll.insert_one({
                    "ip": ip,
                    "locked_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "trigger_message": utterance[:500],
                    "history": history_dump,
                    "lockout_reason": lockout_reason[:256],
                    "unlocked": False,
                })
            except Exception as exc:
                # Mode 2 (REQ-B-008): lockout-incident insert failed.
                # In-memory locked-out flag below still gets set, so the
                # current turn surfaces the lockout — but the row isn't
                # persisted, so next-turn hydration finds nothing and the
                # user is silently unlocked. Operator MUST know.
                log.exception("LockoutTool Task C insert_one failed: %s", exc, exc=ChatHealthyException(
                                                                                mode="lockout_task_c_insert_failed",
                                                                                message=f"LockoutTool Task C insert_one failed: {exc}",
                                                                                component="LockoutTool",
                                                                                exception=exc,
                                                                            ), if_not_debug_log=True)

        deps.user_object.is_locked_out = True
        deps.user_object.lockout = Lockout(
            expires_at=expires_at,
            trigger_utterance=utterance,
            history=history_dump,
        )

        msg = (
            f"When you said \"{utterance}\" it has been determined that "
            "you may need immediate medical attention. You cannot use "
            "this tool for 1 hour. If you or someone near you is in "
            "danger right now, please call 911. For non-emergency "
            f"support, you can also contact {OPERATOR_NAME} at "
            f"{OPERATOR_PHONE}."
        )
        deps.stream({"kind": "prompt", "data": {"text": msg}})
        append_system_utterance(deps.user_object, msg)

        morph_to_close_connection_200(deps)
        await asyncio.sleep(0)
        return self.Response(task="C", locked=True)


TOOL = LockoutTool()
