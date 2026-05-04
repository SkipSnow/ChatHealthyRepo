# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pydantic models for the specialty-query normalization step
# (EPIC-006-F-002-S-001: AI normalization → vector search pipeline).

from pydantic import BaseModel, Field


class SpecialtyNormalizeInput(BaseModel):
    """Caller-supplied natural-language healthcare request."""
    query: str = Field(..., description="The user's natural-language request for healthcare, in their own words.")


class SpecialtyNormalizeOutput(BaseModel):
    """LLM-produced medically-appropriate specialty terminology."""
    normalized_text: str = Field(
        ...,
        description=(
            "ONE short phrase that names the medically-appropriate specialty "
            "or specialty cluster the user is asking for, suitable for "
            "matching against the NUCC taxonomy via embedding similarity. "
            "No explanation, no punctuation other than spaces."
        ),
    )
