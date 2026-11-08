# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Clinical-trial page — utterance mining.
#
# The classifier upstream routes and holds no domain knowledge. This page
# reads the utterance itself and answers with the parameters it needs, so
# adding a page adds a prompt rather than enlarging one shared classifier.
#
# The system prompt and the model are loaded from brain/machine_artifacts/
# content/prompts.json, record
# `clinical_trial_utterance_mining_system_prompt`.

from typing import Optional

from pydantic import BaseModel, Field

from ProviderManagement.utterance_mining import (
    history_lines, mine, model_name_for, prompt_record)

MINING_RECORD_ID = "clinical_trial_utterance_mining_system_prompt"

COMPONENT = "ClinicalTrialUtterance"


class MinedClinicalTrialParameters(BaseModel):
    """What the clinical-trial page needs from an utterance, and nothing
    else."""
    condition: str = ""
    age_years: Optional[int] = None
    sex: str = ""
    united_states_only: Optional[bool] = None


class ClinicalTrialUtteranceInput(BaseModel):
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
            deps_type=ClinicalTrialUtteranceInput,
            output_type=MinedClinicalTrialParameters,
            system_prompt=record.get("system_prompt", ""),
        )
    return _AGENT


async def mine_clinical_trial_parameters(
        utterance: str,
        history: Optional[list] = None) -> MinedClinicalTrialParameters:
    return await mine(
        _agent(),
        ClinicalTrialUtteranceInput(
            utterance=utterance, prior_dialogue=history_lines(history)),
        utterance, history,
        component=COMPONENT,
        call_site="ClinicalTrialUtterance.mine_clinical_trial_parameters")
