# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# User Pydantic schema. Field order follows the canonical spec picture.
#
# SHAPE-TBD markers flag fields where the spec did not name a concrete
# representation; values are placeholders pending confirmation.
from __future__ import annotations

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, HttpUrl

from chathealthy_frontend_lib.authentication.session_token import SessionToken


# ── Plan + portal access ───────────────────────────────────────────────
class AccumulatorFact(BaseModel):
    name: str
    value: float
    max: float


class PortalAccess(BaseModel):
    """Plan-portal payload — populated only when Plan.portal_allowed is True."""
    last_update_date: date
    accumulator_facts: List[AccumulatorFact]
    last_three_claim_eobs: List[str]  # SHAPE-TBD: EOB object shape?
    credentials: str  # SHAPE-TBD: opaque token, encrypted blob, dict?


class Plan(BaseModel):
    state: str  # SHAPE-TBD: plan status string vs US state code?
    url: HttpUrl
    begin_date: date
    end_date: date
    portal_allowed: bool
    portal_access: Optional[PortalAccess] = None  # present iff portal_allowed


# ── Insurance info (Plan only) ────────────────────────────────────────
class InsuranceInfo(BaseModel):
    """Per spec: Insurance Infor contains only Plan."""
    plan: Plan


# ── EMR ────────────────────────────────────────────────────────────────
class EMR(BaseModel):
    portal_address: HttpUrl
    credentials: str  # SHAPE-TBD
    fhir_summary_document: str  # SHAPE-TBD: file path? inline FHIR JSON?


# ── Financial facts ────────────────────────────────────────────────────
class MaxSpendOutOfPocket(BaseModel):
    condition: str
    spend_limit: float


class FinancialFacts(BaseModel):
    max_spend_out_of_pocket: List[MaxSpendOutOfPocket]


# ── Registered-user profile facts ──────────────────────────────────────
class ProfileFacts(BaseModel):
    """Per spec: gender, sex, age, then insurance_info, emr,
    unstructured_facts, financial_facts as siblings.
    """
    gender: str  # SHAPE-TBD: enum?
    sex: str     # SHAPE-TBD: enum?
    age: int
    insurance_info: InsuranceInfo
    emr: EMR
    unstructured_facts: List[str]  # SHAPE-TBD: typed records vs raw strings
    financial_facts: FinancialFacts


class RegisteredProfile(BaseModel):
    """Populated only when User.is_registered is True."""
    user_id: str
    user_type: str  # SHAPE-TBD: enum of user types?
    oauth_source: str  # SHAPE-TBD: enum (Google, Microsoft, Apple, ...)
    auth_token: SessionToken
    profile_facts: ProfileFacts


# ── Not-registered branch ──────────────────────────────────────────────
class NotRegisteredFollowUp(BaseModel):
    """Populated only when User.is_registered is False.

    follow_up_record_id is None when has_asked_for_follow_ups is False.
    """
    has_asked_for_follow_ups: bool
    follow_up_record_id: Optional[str] = None


# ── Session-level fields ───────────────────────────────────────────────
class SillyQuestionCounts(BaseModel):
    """Per spec: [session total, current sequence total]."""
    session_total: int
    current_sequence_total: int


class SessionConversationHistory(BaseModel):
    """Relevant-only conversation history for the current session."""
    unanswered_questions: List[str]  # SHAPE-TBD: structured Q records?


# ── User document ──────────────────────────────────────────────────────
class UserData(BaseModel):
    """Field order follows the canonical spec picture."""
    session_conversation_history: SessionConversationHistory
    is_locked_out: bool
    silly_question_counts: SillyQuestionCounts
    ip_address: str  # SHAPE-TBD: IPvAnyAddress vs raw string
    is_registered: bool
    # Conditional branches keyed off is_registered
    registered_profile: Optional[RegisteredProfile] = None      # iff is_registered
    not_registered_followup: Optional[NotRegisteredFollowUp] = None  # iff not is_registered


class User(BaseModel):
    """Top-level user document. JSON serializes as {"user": {...}}."""
    user: UserData
