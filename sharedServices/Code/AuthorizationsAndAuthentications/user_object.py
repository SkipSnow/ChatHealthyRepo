# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""UserObject — Pydantic model for the ChatHealthy session-bound user state.

Mirrors the canonical schema at
https://dev.chathealthy.ai/schemas/ChatHealthyUserStateSchema.json, plus
two additions per Skip 2026-05-15:

  * `current_session_token` — the signed security token, restamped on
    every gate hit (new NONCE each time).
  * `expires_at` — reset to NOW + 300s on each restamp; the canonical
    "this session expires at" marker.

Absence-means-not-yet-known: every other field is Optional and only
populated when its fact exists (e.g., `registered_profile` appears
once OAuth completes, never as a ceremonial empty stub).

Persistence: stored in `admin.sessions`, ONE doc per live session,
keyed by `_id = GUID`. The document body is the UserObject serialized
to JSON (model_dump(mode='python', exclude_none=True)) — flat: every
UserObject field at the top level alongside `_id`.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

from chathealthy_frontend_lib.authentication.session_token import SessionToken

from UtteranceManager.intent_document import IntentDocument


class Utterance(BaseModel):
    """One line of verbatim dialogue between person and system.

    Both actors land in the same bucket so the dialogue reads top-to-bottom
    as a narrative — exactly the form the LLM classifier receives when
    UM rebuilds the transcript as "user: ...\\nsystem: ...".
    """
    n: int = Field(ge=1, description="Per-session sequence, monotonic from 1.")
    at: str = Field(description="ISO-8601 local time + literal ' PST' suffix.")
    actor: Literal["person", "system"]
    text: str


class Action(BaseModel):
    """One system-recorded event in the session. Includes tool invocations
    (UM, UR-dispatched tools), the on_load marker (action #1 of every
    session), and recorded UX gestures (button clicks, etc.).
    """
    n: int = Field(ge=1, description="Per-session sequence, monotonic from 1.")
    at: str = Field(description="ISO-8601 local time + literal ' PST' suffix.")
    tool_name: str = Field(description="'on_load' for action #1; otherwise the tool's TOOL_NAME or 'ux_event'.")
    input_json: dict = Field(default_factory=dict)
    output_json: dict = Field(default_factory=dict)


class SessionConversationHistory(BaseModel):
    """Two buckets per Skip 2026-06-10: a single dialogue narrative and a
    parallel system-action log. Each bucket has its own per-session counter
    starting at 1. Timestamps on every entry handle cross-bucket ordering.
    """
    utterances: list[Utterance] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


class SillyQuestionCounts(BaseModel):
    session_total: int = Field(default=0, ge=0)
    current_sequence_total: int = Field(default=0, ge=0)


class PortalAccess(BaseModel):
    """Populated when InsuranceInfo.portal_allowed is true."""
    last_update_date: Optional[date] = None
    accumulator_facts: list[dict] = Field(default_factory=list)
    last_three_claim_eobs: list[dict] = Field(default_factory=list, max_length=3)
    credentials: Optional[dict] = None


class InsuranceInfo(BaseModel):
    plan: Optional[str] = None
    plan_state: Optional[str] = None
    plan_url: Optional[str] = None
    plan_begin_date: Optional[date] = None
    plan_end_date: Optional[date] = None
    portal_allowed: Optional[bool] = None
    portal_access: Optional[PortalAccess] = None


class EMR(BaseModel):
    portal_address: Optional[str] = None
    credentials: Optional[dict] = None
    fhir_summary_document: Optional[dict] = None


class FinancialFacts(BaseModel):
    """Schema spec: list of {condition, spend_limit} entries."""
    max_spend_out_of_pocket: list[dict] = Field(default_factory=list)


class RegisteredUserProfileFacts(BaseModel):
    gender: Optional[str] = None
    sex: Optional[Literal["Male", "Female", "Other", "Did not disclose"]] = None
    age: Optional[int] = Field(default=None, ge=0)
    insurance_info: Optional[InsuranceInfo] = None
    emr: Optional[EMR] = None
    unstructured_facts: dict = Field(default_factory=dict)
    financial_facts: Optional[FinancialFacts] = None


class RegisteredProfile(BaseModel):
    user_id: str
    user_type: Literal["Guest", "Owner", "Customer", "Prospect"]
    oauth_source: Literal["Google"]
    auth_token: str  # OAuth provider's token, distinct from current_session_token
    profile_facts: RegisteredUserProfileFacts


class NotRegisteredFollowup(BaseModel):
    has_asked_for_follow_ups: bool
    follow_up_record_id: Optional[str] = None


class OAuthIdentity(BaseModel):
    identity_provider: str
    identity_provider_user_id: str
    email: str


class UserObject(BaseModel):
    """The complete session-bound user state.

    Required fields (always present from the moment the gate mints):
      current_session_token, expires_at, session_conversation_history.
    Every other field is Optional and absent until its fact is known.
    """
    # current_session_token is the SessionToken envelope for the live
    # session. Sentinel value "NULL" signals "no session yet" — the
    # gateway uses this on a manufacture_session call so the auth tool
    # can mint a fresh token in place. The sentinel never appears on a
    # response: the manufacture_session branch replaces it with a real
    # SessionToken before returning.
    current_session_token: Union[SessionToken, Literal["NULL"]]
    expires_at: datetime

    # ── Existing User Schema ─────────────────────────────────
    session_conversation_history: SessionConversationHistory = Field(
        default_factory=SessionConversationHistory
    )
    is_locked_out: Optional[bool] = None
    silly_question_counts: Optional[SillyQuestionCounts] = None
    ip_address: Optional[str] = None
    is_registered: Optional[bool] = None
    user_id: Optional[str] = None
    user_type: Optional[Literal["Guest", "Owner", "Customer", "Prospect"]] = None
    public_username: Optional[str] = None
    OAuthIdentities: list[OAuthIdentity] = Field(default_factory=list)
    registered_profile: Optional[RegisteredProfile] = None
    not_registered_followup: Optional[NotRegisteredFollowup] = None

    # IntentDocument carried across turns; populated and updated by
    # UtteranceManager, read and dispatched by UniversalNavigationTool.
    # Per https://dev.chathealthy.ai/schemas/ChatHealthyUtteranceManagerOutputSchema.json
    intent: Optional["IntentDocument"] = None

    def merge(self, guest: "UserObject") -> "UserObject":
        """Merge a guest UserObject INTO this stored UserObject.

        Per-field rules (EPIC-002-F-003-S-004-REQ-T-012 + REQ-T-015):

          current_session_token         guest
          expires_at                    guest
          session_conversation_history  stored arrays first, guest appended
          OAuthIdentities               stored
          is_registered                 bool(OAuthIdentities)
          user_type                     stored
          public_username               stored
          ip_address                    guest
          is_locked_out                 stored
          silly_question_counts         stored + guest
          registered_profile            stored
          not_registered_followup       stored
        """
        # Concatenate stored arrays first, guest appended. Re-number both
        # buckets monotonically so the merged session has a clean 1..N
        # sequence in each bucket (the original n values came from two
        # separate sessions and would collide).
        def _renumber(items: list, kind: str) -> list:
            out = []
            for i, item in enumerate(items, start=1):
                d = item.model_dump() if hasattr(item, "model_dump") else dict(item)
                d["n"] = i
                cls = Utterance if kind == "utterance" else Action
                out.append(cls.model_validate(d))
            return out

        merged_utterances = (
            list(self.session_conversation_history.utterances)
            + list(guest.session_conversation_history.utterances)
        )
        merged_actions = (
            list(self.session_conversation_history.actions)
            + list(guest.session_conversation_history.actions)
        )
        merged_hist = SessionConversationHistory(
            utterances=_renumber(merged_utterances, "utterance"),
            actions=_renumber(merged_actions, "action"),
        )

        # silly_question_counts: lifetime accumulator.
        stored_sqc = self.silly_question_counts
        guest_sqc = guest.silly_question_counts
        if stored_sqc is None and guest_sqc is None:
            merged_sqc = None
        else:
            s = stored_sqc or SillyQuestionCounts()
            g = guest_sqc or SillyQuestionCounts()
            merged_sqc = SillyQuestionCounts(
                session_total=s.session_total + g.session_total,
                current_sequence_total=s.current_sequence_total + g.current_sequence_total,
            )

        # is_registered: derived from OAuthIdentities membership.
        derived_is_registered = len(self.OAuthIdentities) >= 1

        return self.model_copy(update={
            "current_session_token": guest.current_session_token,
            "expires_at": guest.expires_at,
            "session_conversation_history": merged_hist,
            "silly_question_counts": merged_sqc,
            "is_registered": derived_is_registered,
            "ip_address": guest.ip_address if guest.ip_address is not None else self.ip_address,
        })

    def persist_user_state(self, text: str) -> None:
        """Append the typed user text to the session conversation history
        as a person utterance with the next sequence number."""
        from authentication.agent_deps import append_person_utterance
        append_person_utterance(self, text)
