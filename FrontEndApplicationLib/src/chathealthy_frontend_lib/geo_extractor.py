# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""GeoExtractor — pure function that extracts a US location from an utterance.

Single-purpose LLM tool: takes free text from one user utterance, returns
state / county / city / zip. Lives in the shared library so any
ChatHealthy component (UtteranceManager today, EvaluateCare tomorrow)
can call it in-process without crossing a service boundary.

Sufficiency is NOT enforced here — the caller decides whether the
returned fields are enough to make a query. This module only extracts.
"""
from __future__ import annotations

import os
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent


class LocationOutput(BaseModel):
    """Structured location pulled from the utterance."""
    state: Optional[str] = Field(
        default=None,
        description=(
            "US state code in TWO uppercase letters (e.g. 'NY', 'CA') if the "
            "user named a state; else null. Use the standard USPS abbreviation; "
            "expand a full state name such as 'New York' to 'NY'. Never invent."
        ),
    )
    county: Optional[str] = Field(
        default=None,
        description=(
            "US county name (e.g. 'Westchester County') if the user named a "
            "county; include the literal word ' County' (or ' Parish' for "
            "Louisiana, ' Borough' or ' Census Area' for Alaska). Null otherwise."
        ),
    )
    city: Optional[str] = Field(
        default=None,
        description=(
            "US city name (e.g. 'Brooklyn') if the user named a city; else null. "
            "Do not concatenate state into the city field."
        ),
    )
    zip: Optional[str] = Field(
        default=None,
        description=(
            "US ZIP code (5 digits, no dash) if the user named one; else null. "
            "Strip ZIP+4 to the leading 5 digits."
        ),
    )


_MODEL_ID = "google-gla:gemini-3.1-flash-lite-preview"

_SYSTEM_PROMPT = """
You are a US-location extractor. Given one user utterance, extract the
US location the user mentioned into four separate fields: state, county,
city, zip. Each field is null if the user did not name that level.

Rules:
  - state: two UPPERCASE USPS letters (e.g. 'NY' for New York, 'CA' for
    California, 'DC' for District of Columbia). Expand a full state name
    in the query to its two-letter code.
  - county: the county name with the appropriate suffix the user named
    or that is canonical for that jurisdiction. Use ' County' for most
    states, ' Parish' for Louisiana, ' Borough' or ' Census Area' for
    Alaska. Do not invent.
  - city: the city name only. Do not concatenate state into the city
    field. Do not invent.
  - zip: five digits only. If the user gave ZIP+4, return the leading
    five digits only.

Never invent a location the user did not name. If the user did not name
a particular level, return null for that field.

Output: strict JSON object with exactly those four fields. No preamble,
no markdown, no commentary.
""".strip()


_agent: Optional[Agent] = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY not set; required for geo_extractor."
            )
        _agent = Agent(
            _MODEL_ID,
            output_type=LocationOutput,
            system_prompt=_SYSTEM_PROMPT,
            model_settings={"temperature": 0.1},
            retries=3,
        )
    return _agent


def extract_location(utterance: str) -> LocationOutput:
    """Synchronous: run the LLM call and return the structured location.

    Callers in async contexts should invoke via ``asyncio.to_thread`` to
    avoid blocking the event loop.
    """
    text = (utterance or "").strip()
    if not text:
        return LocationOutput()
    return _get_agent().run_sync(text).output
