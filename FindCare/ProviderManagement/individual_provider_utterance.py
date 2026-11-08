# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Individual-provider page — utterance mining.
#
# The classifier upstream routes and holds no domain knowledge. This page
# reads the utterance itself and answers with the parameters it needs, so
# adding a page adds a prompt rather than enlarging one shared classifier.
#
# The system prompt and the model are loaded from brain/machine_artifacts/
# content/prompts.json, record
# `individual_provider_utterance_mining_system_prompt`.

from typing import Optional

from pydantic import BaseModel, Field

from ProviderManagement.utterance_mining import (
    history_lines, mine, model_name_for, prompt_record)

MINING_RECORD_ID = "individual_provider_utterance_mining_system_prompt"

COMPONENT = "IndividualProviderUtterance"


class MinedGeography(BaseModel):
    """Where the person wants the care giver to be."""
    state: str = ""
    city: str = ""
    county: str = ""
    zip: str = ""


class MinedProviderName(BaseModel):
    """A care giver named outright."""
    last: str = ""
    first: str = ""
    middle: str = ""


class MinedIndividualProviderParameters(BaseModel):
    """What the individual-provider page needs from an utterance, and
    nothing else. The specialty codes the search runs under are not here:
    they are resolved from the complaint, not stated by the person."""
    complaint: str = ""
    geography: MinedGeography = Field(default_factory=MinedGeography)
    provider_name: MinedProviderName = Field(
        default_factory=MinedProviderName)
    provider_sex: str = ""
    sole_proprietor: Optional[bool] = None
    insurance: str = ""


class IndividualProviderUtteranceInput(BaseModel):
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
            deps_type=IndividualProviderUtteranceInput,
            output_type=MinedIndividualProviderParameters,
            system_prompt=record.get("system_prompt", ""),
        )
    return _AGENT


async def mine_individual_provider_parameters(
        utterance: str,
        history: Optional[list] = None
) -> MinedIndividualProviderParameters:
    return await mine(
        _agent(),
        IndividualProviderUtteranceInput(
            utterance=utterance, prior_dialogue=history_lines(history)),
        utterance, history, component=COMPONENT,
        call_site="IndividualProviderUtterance."
                  "mine_individual_provider_parameters")
