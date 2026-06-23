# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SafetyService — UAT Feature 5: Safety Filter (dual-trigger, IP lock, audit)
#
# Extracted from main.py as part of ARCH-001 Phase 5.
# Host-independent — no FastAPI, no HuggingFace dependencies.
#
# Design: ARCH-001, shared infrastructure

import json
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import os
from datetime import datetime, timedelta, timezone

log = ChatHealthyLoggingService()


EMERGENCY_RESPONSE = (
    "I'm concerned about your safety. If this is a medical emergency, "
    "please call **911** or go to your nearest emergency room immediately.\n\n"
    "**National Suicide Prevention Lifeline:** 988\n\n"
    "**Crisis Text Line:** Text HOME to 741741\n\n"
    "This session has been locked for your safety. "
    "A ChatHealthy team member will follow up."
)

# Safe prefixes — skip AI classifier for obvious non-emergency patterns
SAFE_PREFIXES = [
    "can you tell me more about", "tell me about", "more info on",
    "what can you tell me about", "who is", "find me", "search for",
    "look up", "what is", "how do i", "where can i",
]


class SafetyService:
    """Dual-trigger emergency detection with IP locking and audit.

    Dependencies: MongoDB (for IP locks + audit), OpenAI GPT-4.1-mini (classifier).
    """

    def __init__(self, get_db_fn, env_prefix: str, emergency_keywords: list[str],
                 openai_api_key: str = "", admin_unlock_key: str = ""):
        self._get_db = get_db_fn
        self._env = env_prefix
        self._keywords = [kw.lower() for kw in emergency_keywords]
        self._openai_key = openai_api_key or os.getenv("OPENAI_API_KEY", "")
        self._admin_unlock_key = admin_unlock_key or os.getenv("ADMIN_UNLOCK_KEY", "")

    def is_emergency(self, message: str) -> bool:
        """AI-primary emergency detection. Keywords are fallback only when AI is unavailable.

        SAFETY-LOCKOUT-002: Removed keyword OR trigger — false positives on provider
        search queries like 'find me a doctor for chest pain'. The AI classifier with
        its three-gate prompt (body location + acute onset + life-threat) is precise.
        Keywords remain as degraded-mode fallback when the API call fails.
        """
        msg_lower = message.lower()

        # Fast path: skip for obvious non-emergency
        if any(msg_lower.startswith(p) for p in SAFE_PREFIXES):
            return False

        # Primary: AI classifier
        try:
            from openai import OpenAI
            oai = OpenAI(api_key=self._openai_key)
            resp = oai.chat.completions.create(
                model="gpt-4.1-mini",
                max_tokens=64,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": (
                        "You are a safety classifier. Decide ONLY between two outcomes:\n"
                        "  EMERGENCY - call 911 or go to ER immediately\n"
                        "  PROBE - ask a clarifying question first\n\n"
                        "EMERGENCY requires ALL of: (1) specific body location or system named, "
                        "(2) acute onset or severity stated, (3) life-threat implied.\n"
                        "NEVER EMERGENCY: pain without specific body location, general not feeling well, "
                        "any question about a doctor/provider, any request for information.\n\n"
                        'Return ONLY valid JSON: {"emergency": true|false, "confidence": 0.0-1.0}'
                    )},
                    {"role": "user", "content": message},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            result = json.loads(raw)
            confidence = float(result.get("confidence", 0))
            return bool(result.get("emergency", False)) and confidence >= 0.80
        except Exception as exc:
            # Mode 2 (REQ-B-008): Safety AI down; falls back to keyword
            # detection. Safety degradation — operator MUST know.
            log.error("Safety AI failed (%s) — falling back to keyword detection", exc, exc=ChatHealthyException(
                                                                                         mode="safety_ai_failed",
                                                                                         message=f"Safety AI failed: {exc}",
                                                                                         component="SafetyService",
                                                                                         exception=exc,
                                                                                     ), if_not_debug_log=True)
            # Fallback: keyword match ONLY when AI is unavailable
            return any(kw in msg_lower for kw in self._keywords)

    def _safety_collection(self):
        """Get the safety incidents collection. Uses legacy collection name for compatibility."""
        db = self._get_db()
        if db is None:
            return None
        return db[f"{self._env}_Safety"]["emergency_incidents"]

    def is_ip_locked(self, ip: str) -> bool:
        """Check if IP is locked due to prior emergency."""
        col = self._safety_collection()
        if col is None:
            return False
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            record = col.find_one({"ip": ip, "expires_at": {"$gt": now_iso}, "unlocked": {"$ne": True}})
            return record is not None
        except Exception as _exc:
            # Mode 2 (REQ-B-008): lockout query failed; we fail-open (treat
            # as not locked). This means a previously-locked user could
            # bypass safety lock if the query is broken — operator MUST know.
            log.error("is_ip_locked find_one failed (treating as not-locked): %s", _exc, exc=ChatHealthyException(
                                                                                            mode="is_ip_locked_query_failed",
                                                                                            message=f"is_ip_locked find_one failed (treating as not-locked): {_exc}",
                                                                                            component="SafetyService",
                                                                                            exception=_exc,
                                                                                        ), if_not_debug_log=True)
            return False

    def lock_ip(self, ip: str, trigger_message: str = "", history: list = None) -> bool:
        """Lock an IP after emergency detection. Audit trail preserved."""
        col = self._safety_collection()
        if col is None:
            log.warning("SAFETY: DB unavailable — incident for %s NOT persisted.", ip)
            return False
        try:
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=3600)
            col.update_one(
                {"ip": ip},
                {"$set": {
                    "ip": ip,
                    "locked_at": now.isoformat(),
                    "expires_at": expires.isoformat(),
                    "trigger_message": trigger_message[:500],
                    "chat_history": history or [],
                }},
                upsert=True,
            )
            log.warning("IP LOCKED: %s (trigger: %s)", ip, trigger_message[:100])
            return True
        except Exception as exc:
            # Mode 2 (REQ-B-008): lock-write failed; in-memory may be set
            # but DB row isn't — next-turn rehydration sees unlocked.
            # Safety system divergence — operator MUST know.
            log.error("Failed to lock IP %s: %s", ip, exc, exc=ChatHealthyException(
                                                            mode="lock_ip_failed",
                                                            message=f"Failed to lock IP {ip}: {exc}",
                                                            component="SafetyService",
                                                            exception=exc,
                                                        ), if_not_debug_log=True)
            return False

    def try_admin_unlock(self, message: str, ip: str) -> bool:
        """Attempt admin unlock with secret key."""
        if not self._admin_unlock_key or self._admin_unlock_key.lower() not in message.lower():
            return False
        col = self._safety_collection()
        if col is None:
            return False
        try:
            result = col.update_one(
                {"ip": ip, "unlocked": {"$ne": True}},
                {"$set": {"unlocked": True, "unlocked_at": datetime.now(timezone.utc).isoformat(), "unlocked_by": "admin"}},
            )
            if result.modified_count > 0:
                log.info("IP UNLOCKED by admin: %s", ip)
                return True
        except Exception as exc:
            # Mode 2 (REQ-B-008): admin unlock DB write failed; user
            # remains locked. Operator MUST know.
            log.error("Admin unlock failed for %s: %s", ip, exc, exc=ChatHealthyException(
                                                                  mode="admin_unlock_failed",
                                                                  message=f"Admin unlock failed for {ip}: {exc}",
                                                                  component="SafetyService",
                                                                  exception=exc,
                                                              ), if_not_debug_log=True)
        return False
