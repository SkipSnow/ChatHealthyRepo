# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pydantic models for the IntentDocument carried on user_object.intent.

Mirrors the canonical JSON Schema at
https://dev.chathealthy.ai/schemas/ChatHealthyUtteranceManagerOutputSchema.json

UM produces this document; UR validates it before dispatch; tools consume
the slice for their intent. The document lives on user_object.intent and
accumulates across turns.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────────────
# Canonical Argument shape
# ────────────────────────────────────────────────────────────────────


class Argument(BaseModel):
    """One argument on an intent entry. Uniform shape across every intent."""

    model_config = {"extra": "forbid"}

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]{0,63}$",
    )
    value: str = Field(min_length=0, max_length=32768)
    type: Literal["string", "boolean", "integer", "number", "object", "array"]
    required: bool


# ────────────────────────────────────────────────────────────────────
# Per-intent pending disambiguation marker (REQ-B-009)
# ────────────────────────────────────────────────────────────────────


class PendingDisambiguation(BaseModel):
    """Marker carried on an intent entry when the classifier could not fully
    fill a required slot but has a plausible candidate value for it. UM
    sets it; UR ignores the partially-filled intent (does not dispatch its
    action) until UM clears it on a subsequent turn."""

    model_config = {"extra": "forbid"}

    kind: str = Field(
        min_length=1,
        max_length=64,
        description="The disambiguation category. For FindCare's geography slot this is 'geography_state'.",
    )
    candidate: dict[str, Any] = Field(
        default_factory=dict,
        description="The structured candidate value the classifier proposed (e.g., {'state':'WI'}).",
    )
    scaffolding: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional free-form context the next-turn resolver needs.",
    )


# ────────────────────────────────────────────────────────────────────
# Per-intent entries
# ────────────────────────────────────────────────────────────────────


class IntentNonsense(BaseModel):
    """Intent entry: utterance was gibberish. Carries the typed text and an
    is_nonsense=true confirmation argument."""

    model_config = {"extra": "forbid"}

    name: Literal["nonsense"]
    arguments: list[Argument] = Field(min_length=2, max_length=2)
    pending_disambiguation: Optional[PendingDisambiguation] = None


class IntentSpecialtySearch(BaseModel):
    """Intent entry: UM extracted a complaint. Carries the complaint
    argument and may carry the SpecialtyFilter output (nucc_codes) plus
    the user's Apply-Filter selection (selected_nucc_codes) once UR has
    run SpecialtyFilter at least once for this complaint. nucc_codes is
    the FULL specialty universe — it MUST NOT be overwritten by Apply
    Filter; the user's narrowing goes onto selected_nucc_codes."""

    model_config = {"extra": "forbid"}

    name: Literal["specialtySearch"]
    arguments: list[Argument] = Field(min_length=1, max_length=3)
    pending_disambiguation: Optional[PendingDisambiguation] = None


class IntentFindAProvider(BaseModel):
    """Intent entry: user is looking for a healthcare provider. Carries the
    complaint phrase, a JSON-encoded geography object, optionally the
    cached SpecialtyFilter universe (nucc_codes), and optionally the
    user's Apply-Filter selection (selected_nucc_codes). nucc_codes
    represents the FULL universe — it MUST NOT be overwritten by Apply
    Filter; the user's narrowing goes onto selected_nucc_codes. When
    the classifier can propose a candidate for the geography slot but
    cannot fully fill it, pending_disambiguation is set and
    target_action stays at specialtySearch."""

    model_config = {"extra": "forbid"}

    name: Literal["findAProvider"]
    arguments: list[Argument] = Field(min_length=1, max_length=4)
    pending_disambiguation: Optional[PendingDisambiguation] = None


class IntentFindClinicalTrials(BaseModel):
    """Intent entry: user is looking for recruiting clinical trials.
    Carries the complaint (condition), an optional user_location string,
    and an optional cursor for paged retrieval. EPIC-006-F-031-S-003."""

    model_config = {"extra": "forbid"}

    name: Literal["findClinicalTrials"]
    arguments: list[Argument] = Field(min_length=1, max_length=3)
    pending_disambiguation: Optional[PendingDisambiguation] = None


class IntentCloseConnection200(BaseModel):
    """Intent entry: UR closes the StreamingResponse with HTTP 200 OK after
    every dispatched tool has flushed its events. Carries a single
    close_connection=true confirmation argument."""

    model_config = {"extra": "forbid"}

    name: Literal["closeConnection200"]
    arguments: list[Argument] = Field(min_length=1, max_length=1)
    pending_disambiguation: Optional[PendingDisambiguation] = None


class IntentSafetyLockout(BaseModel):
    """Intent entry: UM classified the latest utterance as signalling
    immediate medical attention. UR dispatches LockoutTool, which writes
    the {env}_Safety.emergency_incidents record, stamps user_object
    lockout state, and morphs target_action to closeConnection200.
    Carries a single lockout_reason audit-label argument; the user-facing
    'why' is rendered from the verbatim trigger utterance, not from this
    label."""

    model_config = {"extra": "forbid"}

    name: Literal["safetyLockout"]
    arguments: list[Argument] = Field(min_length=1, max_length=1)
    pending_disambiguation: Optional[PendingDisambiguation] = None


# Discriminated union — pydantic uses the `name` field to pick the variant.
IntentEntry = Annotated[
    Union[
        IntentNonsense,
        IntentSpecialtySearch,
        IntentFindAProvider,
        IntentFindClinicalTrials,
        IntentCloseConnection200,
        IntentSafetyLockout,
    ],
    Field(discriminator="name"),
]


# ────────────────────────────────────────────────────────────────────
# Top-level document
# ────────────────────────────────────────────────────────────────────


TargetAction = Literal[
    "nonsense",
    "specialtySearch",
    "findAProvider",
    "findClinicalTrials",
    "closeConnection200",
    "safetyLockout",
]


class IntentDocument(BaseModel):
    """UtteranceManager's output document. Lives on user_object.intent and
    accumulates across turns. target_action names the next action UR
    dispatches; intents[] holds the typed entries; user_message is the
    LLM-authored prose UM streams to the user before returning."""

    model_config = {"extra": "forbid"}

    target_action: TargetAction
    intents: list[IntentEntry] = Field(min_length=1, max_length=4)
    user_message: Optional[str] = Field(
        default=None,
        max_length=4096,
        description="LLM-authored prose intended for the user. When non-empty UM streams it as {kind:'prompt', data:{text: user_message}} and flushes before returning to UR.",
    )
