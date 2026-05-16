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

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from chathealthy_frontend_lib.authentication.session_token import SessionToken


class SessionConversationHistory(BaseModel):
    """All human / machine / LLM turns captured during the session.

    Three actors (Person, Machine, LLM); the splash renders these as
    four parallel scroll-bar threads (Person, Machine, LLM→Person,
    LLM→Machine). Arrays grow as ops fire through the gate.
    """
    unanswered_questions: list[str] = Field(default_factory=list)
    ux_events: list[dict] = Field(default_factory=list)
    utterances: list[dict] = Field(default_factory=list)


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
    """One identity provider's claim about the user. EPIC-002-F-003-S-004."""
    provider: Literal["Google"]
    provider_user_id: str
    email: str


class UserObject(BaseModel):
    """The complete session-bound user state.

    Required fields (always present from the moment the gate mints):
      current_session_token, expires_at, session_conversation_history.
    Every other field is Optional and absent until its fact is known.
    """
    # ── Skip 2026-05-15 additions ────────────────────────────
    current_session_token: SessionToken
    expires_at: datetime

    # ── Existing User Schema ─────────────────────────────────
    session_conversation_history: SessionConversationHistory = Field(
        default_factory=SessionConversationHistory
    )
    is_locked_out: Optional[bool] = None
    silly_question_counts: Optional[SillyQuestionCounts] = None
    ip_address: Optional[str] = None
    is_registered: Optional[bool] = None
    user_type: Optional[Literal["Guest", "Owner", "Customer", "Prospect"]] = None
    public_username: Optional[str] = None
    OAuthIdentities: list[OAuthIdentity] = Field(default_factory=list)
    registered_profile: Optional[RegisteredProfile] = None
    not_registered_followup: Optional[NotRegisteredFollowup] = None
