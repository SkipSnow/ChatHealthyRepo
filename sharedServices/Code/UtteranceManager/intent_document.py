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

from typing import Annotated, Literal, Union

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
    value: str = Field(min_length=0, max_length=1000)
    type: Literal["string", "boolean", "integer", "number", "object", "array"]
    required: bool


# ────────────────────────────────────────────────────────────────────
# Per-intent entries
# ────────────────────────────────────────────────────────────────────


class IntentNonsense(BaseModel):
    """Intent entry: utterance was gibberish. Carries the typed text and an
    is_nonsense=true confirmation argument."""

    model_config = {"extra": "forbid"}

    name: Literal["nonsense"]
    arguments: list[Argument] = Field(min_length=2, max_length=2)


class IntentSpecialtySearch(BaseModel):
    """Intent entry: UM extracted a complaint but no geography. SpecialtyFilter
    can still translate the complaint into specialty codes; the FE renders
    candidate provider types. Once geography arrives on a later turn UM
    upgrades the target_action to findAProvider."""

    model_config = {"extra": "forbid"}

    name: Literal["specialtySearch"]
    arguments: list[Argument] = Field(min_length=1, max_length=1)


class IntentFindAProvider(BaseModel):
    """Intent entry: user is looking for a healthcare provider. Carries the
    complaint phrase and a JSON-encoded geography object."""

    model_config = {"extra": "forbid"}

    name: Literal["findAProvider"]
    arguments: list[Argument] = Field(min_length=2, max_length=2)


class IntentCloseConnection200(BaseModel):
    """Intent entry: UM has streamed a clarification prompt to the user and
    UR should close the connection with HTTP 200 OK. Carries a single
    close_connection=true confirmation argument."""

    model_config = {"extra": "forbid"}

    name: Literal["closeConnection200"]
    arguments: list[Argument] = Field(min_length=1, max_length=1)


# Discriminated union — pydantic uses the `name` field to pick the variant.
IntentEntry = Annotated[
    Union[
        IntentNonsense,
        IntentSpecialtySearch,
        IntentFindAProvider,
        IntentCloseConnection200,
    ],
    Field(discriminator="name"),
]


# ────────────────────────────────────────────────────────────────────
# Top-level document
# ────────────────────────────────────────────────────────────────────


TargetAction = Literal[
    "nonsense", "specialtySearch", "findAProvider", "closeConnection200",
]


class IntentDocument(BaseModel):
    """UtteranceManager's output document. Lives on user_object.intent and
    accumulates across turns. target_action names the next action UR
    dispatches; intents[] holds the typed entry for that action plus any
    other intents UM is tracking across turns."""

    model_config = {"extra": "forbid"}

    target_action: TargetAction
    intents: list[IntentEntry] = Field(min_length=1, max_length=3)
