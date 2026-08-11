# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Batched NUCC code derivation for clinical trials.

Single LLM round-trip per page: list of trials in, map nct_id -> NUCC codes
out. Failure returns an empty map so NPI enrichment becomes a no-op rather
than blocking the response."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()


def _anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


NUCC_DERIVATION_MODEL_NAME = "claude-haiku-4-5-20251001"

NUCC_DERIVATION_SYSTEM_PROMPT = """\
You are deriving NUCC provider-taxonomy codes for a batch of clinical trials.

For each trial you receive (identified by nct_id), examine the trial's
conditions, brief_title, and brief_summary and return the NUCC taxonomy
codes (10-character strings like '207R00000X', '2084P0800X', '363LP0808X')
that represent the clinical specialties whose providers would typically
treat the condition under study.

Return at most 10 codes per trial. Prefer specialties whose practitioners
actively manage the condition (treating clinicians, surgeons, specialists)
over support specialties (lab, radiology, pharmacy) unless the trial's
focus is on those support specialties.

Output strictly the structured schema you are given: a list of items,
each item carries the trial's nct_id and a list of NUCC codes for that
trial. If a trial's content is too thin to derive any codes, return an
empty list for that trial. Never invent NCT IDs not in the input.
"""


class TrialNuccCodes(BaseModel):
    nct_id: str
    codes: list[str] = Field(default_factory=list)


class NuccDerivationOutput(BaseModel):
    results: list[TrialNuccCodes] = Field(default_factory=list)


_model = AnthropicModel(
    NUCC_DERIVATION_MODEL_NAME,
    provider=AnthropicProvider(api_key=_anthropic_api_key()),
)

_agent = Agent(
    _model,
    output_type=NuccDerivationOutput,
    system_prompt=NUCC_DERIVATION_SYSTEM_PROMPT,
)


def _format_input(trials: list[dict]) -> str:
    lines = []
    for t in trials:
        lines.append(
            f"nct_id: {t.get('nct_id','')}\n"
            f"conditions: {', '.join(t.get('conditions') or [])}\n"
            f"brief_title: {t.get('brief_title','')}\n"
            f"brief_summary: {(t.get('brief_summary','') or '')[:600]}\n"
            "---"
        )
    return "\n".join(lines)


async def derive_nucc_codes_for_page(trials: list[dict]) -> dict[str, list[str]]:
    if not trials:
        return {}
    try:
        result = await _agent.run(_format_input(trials))
    except Exception as exc:
        log.warning(
            "NUCC derivation LLM call failed; returning empty map: %s", exc,
            exc=ChatHealthyException(
                mode="nucc_derivation_failed",
                message=f"NUCC derivation LLM call failed: {exc}",
                component="NuccDerivation",
                exception=exc,
            ),
            if_not_debug_log=True,
        )
        return {}
    return {r.nct_id: [c for c in r.codes if c] for r in result.output.results}
