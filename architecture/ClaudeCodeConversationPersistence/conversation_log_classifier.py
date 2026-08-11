"""Classify an utterance by tone and by what it was doing.

Two axes, deliberately independent: HOW something was said and WHAT it was
doing are different questions, and a cold reprimand and a furious one are the
same act. The enumerations come from a full pass over 38,876 archived
utterances, not from invention -- every value below was observed, counted, and
hand-audited (tone 98/100, message class 90/100).

Gemini through pydantic-ai with a structured output type, so the model cannot
return a value outside the enumerations: pydantic rejects it before it reaches
the archive.
"""

from __future__ import annotations

import os
import threading
from enum import Enum

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.llm import run_llm_sync

CLASSIFIER_MODEL_NAME = "gemini-2.5-flash-lite"
CLASSIFIER_VERSION = 1


class Tone(str, Enum):
    """How it was said. Manner of delivery, never the act underneath."""

    matter_of_fact = "matter_of_fact"
    expository = "expository"
    terse = "terse"
    deferential = "deferential"
    emphatic = "emphatic"
    tentative = "tentative"
    appreciative = "appreciative"
    exasperated = "exasperated"
    candid = "candid"
    wry = "wry"
    unclassified = "unclassified"


class UtteranceClass(str, Enum):
    """What it was doing. The act, never the manner."""

    report = "report"
    inquiry = "inquiry"
    source_material = "source_material"
    directive = "directive"
    acknowledgement = "acknowledgement"
    authorization = "authorization"
    correction = "correction"
    declination = "declination"
    requirement = "requirement"
    reprimand = "reprimand"
    architecture = "architecture"
    apology = "apology"
    unclassified = "unclassified"


class Classification(BaseModel):
    tone: Tone = Field(description="How it was said.")
    utterance_class: UtteranceClass = Field(description="What it was doing.")


SYSTEM_PROMPT = """You classify one utterance from a software engineering
conversation on two INDEPENDENT axes. Judge them separately: a reprimand
delivered calmly is still a reprimand, and a matter-of-fact tone says nothing
about what the utterance was doing.

TONE - how it was said:
  matter_of_fact  plain declarative; no hedging, force, or affect
  expository      sustained reasoning; enumerated points, causal connectives
  terse           clipped to the minimum; roughly ten words or fewer
  deferential     service register; hedged offers, politeness ritual
  emphatic        forceful; capitalised absolutes, MUST/NEVER, exclamation
  tentative       hedged and unsure; "I think", "not sure", "probably"
  appreciative    warmth toward the other party; praise, thanks
  exasperated     visible irritation as manner; profanity, "seriously?"
  candid          forthright; volunteers the unwelcome fact or own limits
  wry             humour, irony, self-mockery alongside the point
  unclassified    no manner determinable from the text

MESSAGE CLASS - what it was doing:
  report          states progress or the state of work or a system
  inquiry         asks for information, a fact, or a confirmation
  source_material pasted payload: config, JSON, code, logs, records
  directive       instructs the other party to perform work now
  acknowledgement receipt and uptake only; carries no new content
  authorization   grants, withholds, or picks; unblocks or blocks
  correction      asserts a statement is wrong and supplies the right one
  declination     states an inability or refusal to do or answer
  requirement     states what the SYSTEM must do; subject is the system
  reprimand       rebukes the recipient for conduct; about conduct, not facts
  architecture    settles structure: components, ownership, how parts relate
  apology         accepts fault for one's own conduct
  unclassified    a bare identifier, path or token with no surrounding sentence

Return exactly one tone and one message class."""


def _api_key() -> str:
    key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not key:
        raise ChatHealthyException(
            mode="llm_unavailable",
            component="ConversationLogClassifier",
            message="GEMINI_API_KEY (or GOOGLE_API_KEY) is not set; "
                    "utterances cannot be classified.",
        )
    return key


# One agent per thread, not one shared. run_sync binds an asyncio event loop to
# the calling thread, so a shared Agent used from a pool fails with "bound to a
# different event loop" -- which is what a concurrent backfill hit immediately.
# Same pattern as FindCare's SpecialtyFilter.
_thread_local = threading.local()


def _classifier() -> Agent:
    """Built once per thread. Construction is deferred so importing this module
    costs nothing when classification is switched off."""
    agent = getattr(_thread_local, "agent", None)
    if agent is None:
        agent = Agent(
            GeminiModel(CLASSIFIER_MODEL_NAME,
                        provider=GoogleGLAProvider(api_key=_api_key())),
            output_type=Classification,
            system_prompt=SYSTEM_PROMPT,
        )
        _thread_local.agent = agent
    return agent


def classify(content: str) -> Classification | None:
    """Classify one utterance. None when there is nothing to classify.

    Raises whatever the LLM facade raises after its retries -- the caller
    decides whether an unclassified utterance is worth failing over. The
    archive's own rule is that it is not: a record without a classification is
    still the record, and a record lost is not.
    """
    text = (content or "").strip()
    if not text:
        return None
    result = run_llm_sync(
        _classifier(), text,
        call_site="conversation_log_classifier.classify",
        provider="google",
        server="conversation-log-drain",
        component="ConversationLogClassifier",
    )
    return result.output
