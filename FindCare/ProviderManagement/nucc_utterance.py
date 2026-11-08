# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# NUCC page — utterance mining.
#
# The classifier upstream routes and holds no domain knowledge. This page
# reads the utterance itself and answers with the parameters it needs, so
# adding a page adds a prompt rather than enlarging one shared classifier.
#
# The system prompt and the model are loaded from brain/machine_artifacts/
# content/prompts.json, record `nucc_utterance_mining_system_prompt`.

from typing import Optional

from pydantic import BaseModel, Field

from ProviderManagement.utterance_mining import (
    history_lines, mine, model_name_for, prompt_record)

MINING_RECORD_ID = "nucc_utterance_mining_system_prompt"

COMPONENT = "NuccUtterance"


class MinedNuccParameters(BaseModel):
    """What the NUCC page needs from an utterance, and nothing else. The
    specialties it offers and the codes it offers them under are resolved
    from the complaint, not stated by the person."""
    complaint: str = ""


class NuccUtteranceInput(BaseModel):
    """What this page's miner is given: the latest utterance and the dialogue
    before it. Declared here because the Agent below names it."""
    utterance: str = Field(description="The latest utterance, verbatim.")
    prior_dialogue: str = Field(
        default="",
        description="The conversation before this utterance, or empty.")


_AGENT = None


def _agent():
    """This page's mining agent, built on first use."""
    global _AGENT
    if _AGENT is None:
        from pydantic_ai import Agent
        record = prompt_record(MINING_RECORD_ID, COMPONENT)
        _AGENT = Agent(
            f"openai:{model_name_for(record, COMPONENT)}",
            deps_type=NuccUtteranceInput,
            output_type=MinedNuccParameters,
            system_prompt=record.get("system_prompt", ""),
        )
    return _AGENT


async def mine_nucc_parameters(
        utterance: str,
        history: Optional[list] = None) -> MinedNuccParameters:
    return await mine(
        _agent(),
        NuccUtteranceInput(utterance=utterance,
                           prior_dialogue=history_lines(history)),
        utterance, history,
        component=COMPONENT,
        call_site="NuccUtterance.mine_nucc_parameters")
