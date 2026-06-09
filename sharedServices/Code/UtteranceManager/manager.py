# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""UtteranceManager — the classifier ChatHealthyTool.

Reads the user's latest utterance off deps.user_object, calls an LLM to
classify it into one of the catalog actions, optionally streams a
clarification prompt to the user, builds the IntentDocument, writes it
back to deps.user_object.intent, and returns.

Per https://dev.chathealthy.ai/schemas/ChatHealthyUtteranceManagerOutputSchema.json
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool

from UtteranceManager.intent_document import (
    Argument,
    IntentCloseConnection200,
    IntentDocument,
    IntentFindAProvider,
    IntentNonsense,
    IntentSpecialtySearch,
)

_log = logging.getLogger("shared_services.utterance_manager")


_LLM_MODEL = "google-gla:gemini-2.5-flash"


class _GeoFacts(BaseModel):
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None


class _ClassifierOutput(BaseModel):
    """Structured output of the UM classifier LLM. Field names match the
    canonical IntentDocument schema so the LLM writes the document's
    target_action directly."""
    target_action: Literal[
        "nonsense", "specialtySearch", "findAProvider", "closeConnection200",
    ]
    complaint: Optional[str] = None
    geography: Optional[_GeoFacts] = None
    prompt_text: Optional[str] = None


_CANONICAL_INTENT_DOCUMENT_SCHEMA = r"""
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://dev.chathealthy.ai/schemas/ChatHealthyUtteranceManagerOutputSchema.json",
  "title": "ChatHealthy UtteranceManager Output",
  "description": "Structured output of the UtteranceManager (UM) classifier used by the ChatHealthy Universal Router (UR) to call the correct tool to do work on behalf of an end user. UM examines the user's latest utterance with others as necessary, and the prior intent document carried on user_object.intent.\n\nPERSISTENCE: this document lives on the session as `user_object.intent`. UM's first action on every invocation is to read user_object.intent and construct the Pydantic object from it; if the field is empty (first turn of the session), UM constructs a fresh document and assigns it to user_object.intent. UM updates the document over the course of its classification work and writes the updated version back to user_object.intent BEFORE returning control to UR. The document survives across turns — UM accumulates intents and arguments turn-by-turn rather than starting fresh each time.\n\nDIVISION OF RESPONSIBILITY:\n  * UM (producing side) — before emitting, validates that the chosen target_action's intent entry exists in intents[] and that every required argument for that intent has a value that parses to its declared type. UM enforces every semantic rule the JSON Schema cannot reach (e.g., the sufficiency rule on findAProvider.geography). If any required argument cannot be filled, UM streams a clarification prompt to the user and sets target_action to closeConnection200.\n  * UR (dispatch + compliance check) — UR validates the document before dispatching: target_action MUST be one of the catalog enum values; target_action MUST correspond to a name in intents[]; that intent entry MUST have all of its required: true arguments present with non-empty values that parse to their declared types; every semantic rule the JSON Schema cannot reach (e.g., findAProvider.geography sufficiency) MUST hold. If any check fails UR raises immediately at the smallest possible scope. UR is responsible for not making bad calls.\n  * Each tool (consuming side) — receives the document (or its intent's slice), reads the arguments it cares about, applies any tool-specific business rules, executes its work. Tools add their own defensive asserts without relying on UR having pre-validated.\n\nSTREAMING CONTRACT: every dispatched tool flushes the stream (awaits at least one event-loop tick after its last deps.stream(...) call) before returning control to UR, so when closeConnection200 is the next dispatch all bytes prior tools wrote are on the wire before the StreamingResponse terminates.\n\nMULTI-INTENT: intents[] can carry more than one entry. UM may have detected findAProvider on turn 1 (geography missing) and still be tracking that intent on turn 2 when the user provides the missing geography. UM may also have detected a secondary intent (e.g., a safety concern) in the same utterance. target_action is always the single intent UR actions this turn; the other intents remain in the document for future turns.\n\nCATALOG (this deploy's closed set): nonsense, findAProvider, closeConnection200. UM clamps to one of these three; UR raises on any out-of-catalog target_action.",
  "type": "object",
  "additionalProperties": false,
  "required": ["target_action", "intents"],
  "properties": {
    "target_action": {
      "description": "The single action UR must dispatch next. UM picks one of the catalog values; UR's match/case is keyed off this field. MUST correspond to a name in intents[] — UM enforces that invariant before emitting.",
      "enum": ["nonsense", "specialtySearch", "findAProvider", "closeConnection200"]
    },
    "intents": {
      "type": "array",
      "description": "All intents UM has recognized for this document across turns. Each entry is one of the typed intent shapes defined in $defs. uniqueItems is enforced on the (name) field via the per-shape const — two entries with the same name MUST NOT exist in this array.",
      "minItems": 1,
      "maxItems": 3,
      "uniqueItems": true,
      "items": {
        "oneOf": [
          { "$ref": "#/$defs/IntentNonsense" },
          { "$ref": "#/$defs/IntentSpecialtySearch" },
          { "$ref": "#/$defs/IntentFindAProvider" },
          { "$ref": "#/$defs/IntentCloseConnection200" }
        ]
      }
    }
  },
  "$defs": {
    "Argument": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "value", "type", "required"],
      "description": "Canonical argument shape. Every argument across every intent uses this same four-field shape so the router (and any introspection tool) can walk arguments uniformly. value is always carried as a string; the consumer parses it according to type. Code guards in UM (emit-side) and in each consuming tool (receive-side) enforce semantic constraints the JSON Schema cannot reach.",
      "properties": {
        "name": {
          "type": "string",
          "description": "Argument name. Per-intent the set of valid names is enumerated by that intent's argument oneOf. MUST be one of the names the intent declares; UM-side and tool-side code guards enforce.",
          "minLength": 1,
          "maxLength": 64,
          "pattern": "^[a-z][a-z0-9_]{0,63}$"
        },
        "value": {
          "type": "string",
          "description": "The argument's value, serialized as a string. For type='boolean', exactly 'true' or 'false'. For type='integer'/'number', the decimal string with no leading zeros or whitespace. For type='object'/'array', a JSON-encoded string the consumer parses with json.loads. For type='string', the raw text. MUST be non-empty when the argument is required: true.",
          "minLength": 0,
          "maxLength": 1000
        },
        "type": {
          "description": "Names the JSON-native type that value, once parsed, will produce. Constrained to the JSON Schema 2020-12 primitive type set.",
          "enum": ["string", "boolean", "integer", "number", "object", "array"]
        },
        "required": {
          "type": "boolean",
          "description": "True when the dispatched tool cannot run without this argument's value being present and parseable. False when the argument is optional (tool has a default or can proceed without it). UM MUST NOT emit a target_action whose required: true arguments are missing or whose values fail to parse to their declared type; UM's fallback when any required is unfillable is to set target_action to closeConnection200 after streaming a clarification prompt."
        }
      }
    },
    "IntentNonsense": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for an utterance UM classified as gibberish or otherwise not a real request. When target_action is nonsense, UR dispatches to NonsenseTool, whose deploy-1 behavior is to bump the silly-question counter on user_object using the utterance and is_nonsense arguments.",
      "properties": {
        "name": { "const": "nonsense" },
        "arguments": {
          "type": "array",
          "description": "Exactly two arguments: utterance (the typed text) and is_nonsense (always true). Both required.",
          "minItems": 2,
          "maxItems": 2,
          "uniqueItems": true,
          "items": {
            "oneOf": [
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "utterance" }, "type": { "const": "string" }, "required": { "const": true }, "value": { "minLength": 1, "maxLength": 4096 } } }
                ],
                "description": "Exact text the user typed that was classified as nonsense. NonsenseTool consumes this verbatim for the silly-question audit record."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "is_nonsense" }, "type": { "const": "boolean" }, "required": { "const": true }, "value": { "const": "true" } } }
                ],
                "description": "Explicit boolean carried as data so NonsenseTool can assert on it directly without re-reading target_action. value MUST be the string 'true' (parsed as boolean true) on every nonsense intent entry, when target_action is nonsense."
              }
            ]
          }
        }
      }
    },
    "IntentSpecialtySearch": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for an utterance where UM has extracted a complaint phrase but no usable geography. UR dispatches to SpecialtyFilter, which translates the complaint into NUCC specialty codes; the FE renders the specialty list so the user can see candidate provider types even before they tell us where they are. specialtySearch is the partial-information counterpart to findAProvider; once the user supplies geography on a subsequent turn UM upgrades the target_action to findAProvider.",
      "properties": {
        "name": { "const": "specialtySearch" },
        "arguments": {
          "type": "array",
          "description": "Exactly one argument: complaint (the natural-language symptom/condition phrase).",
          "minItems": 1,
          "maxItems": 1,
          "uniqueItems": true,
          "items": {
            "allOf": [
              { "$ref": "#/$defs/Argument" },
              { "properties": { "name": { "const": "complaint" }, "type": { "const": "string" }, "required": { "const": true }, "value": { "minLength": 1, "maxLength": 1024 } } }
            ],
            "description": "Natural-language complaint phrase UM extracted (e.g. 'back pain'). SpecialtyFilter translates this into NUCC codes via its own LLM call."
          }
        }
      }
    },
    "IntentFindAProvider": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for an utterance UM classified as the user looking for a healthcare provider. When target_action is findAProvider, UR dispatches to the find-a-provider tool, which uses complaint to drive the SpecialtyFilter (NUCC classification) and geography to drive the provider query.",
      "properties": {
        "name": { "const": "findAProvider" },
        "arguments": {
          "type": "array",
          "description": "Exactly two arguments: complaint (the natural-language symptom/condition phrase) and geography (the structured location facts). Both required.",
          "minItems": 2,
          "maxItems": 2,
          "uniqueItems": true,
          "items": {
            "oneOf": [
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "complaint" }, "type": { "const": "string" }, "required": { "const": true }, "value": { "minLength": 1, "maxLength": 1024 } } }
                ],
                "description": "Natural-language complaint phrase UM extracted (e.g. 'back pain', 'persistent cough'). SpecialtyFilter downstream translates this into NUCC codes via its own LLM call. value is the phrase as the user expressed it (or as UM paraphrased it from the prior conversation history)."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "geography" }, "type": { "const": "object" }, "required": { "const": true }, "value": { "minLength": 2, "maxLength": 512 } } }
                ],
                "description": "Structured location facts. value is a JSON-encoded object the consumer parses with json.loads. The parsed object MUST have at minimum one of: (a) zip as a 5-digit ZIP code, (b) state as a 2-letter USPS code, (c) state plus city, or (d) state plus county. City or county WITHOUT state is NOT sufficient. UM-side code guards enforce this rule before setting target_action to findAProvider; if the rule cannot be met, UM streams a clarification prompt asking for the missing location and sets target_action to closeConnection200 instead. The parsed object MAY also have any combination of state/city/county/zip beyond the minimum."
              }
            ]
          }
        }
      }
    },
    "IntentCloseConnection200": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for the close-with-200 action UR dispatches. UM is responsible for streaming the prompt to the user before emitting closeConnection200 as target_action; UR is responsible for closing the connection. The closeConnection200 tool has no knowledge of what was said to the user or what the user said to elicit the response — its only job is to close the StreamingResponse with HTTP 200 OK.",
      "properties": {
        "name": { "const": "closeConnection200" },
        "arguments": {
          "type": "array",
          "description": "Exactly one argument: close_connection (boolean, always true).",
          "minItems": 1,
          "maxItems": 1,
          "uniqueItems": true,
          "items": {
            "allOf": [
              { "$ref": "#/$defs/Argument" },
              { "properties": { "name": { "const": "close_connection" }, "type": { "const": "boolean" }, "required": { "const": true }, "value": { "const": "true" } } }
            ],
            "description": "Explicit confirmation boolean. value MUST be the string 'true' (parsed as boolean true) on every closeConnection200 intent entry. The closing tool asserts on this before terminating the StreamingResponse with HTTP 200 OK."
          }
        }
      }
    }
  }
}
"""


_CLASSIFIER_SYSTEM_PROMPT = """\
You are the utterance classifier for ChatHealthy.ai (the UtteranceManager, "UM"
in the schema below). Your single job: examine the user's latest utterance and
the prior IntentDocument context, and decide which target_action the Universal
Router (UR) must dispatch next. You set target_action; downstream Python turns
your choice into the canonical IntentDocument shown in the schema.

WHERE YOU SIT IN THE PROCESS:
  - The user types an utterance into the ChatHealthy.ai client.
  - The client POSTs the utterance to SharedServices /gate.
  - /gate authenticates the session, then hands the user_object (with the
    new utterance appended and any prior intent document attached) to UR
    (the UniversalNavigationTool).
  - UR dispatches you (UM) FIRST, on every utterance. You read the latest
    utterance and the prior IntentDocument off user_object.intent, and you
    return your structured output.
  - Python translates your output into the canonical IntentDocument and
    writes it back onto user_object.intent.
  - UR then reads user_object.intent.target_action and dispatches the
    matching downstream tool: NonsenseTool, SpecialtyFilter (specialtySearch),
    SpecialtyFilter+ProviderSearch (findAProvider), or CloseConnection200Tool.
  - Each downstream tool streams its events back to the client through the
    same /gate StreamingResponse.
  - Your target_action is the single decision that drives all of that. If
    you pick wrong, the wrong tool runs, the user waits for nothing useful,
    and the streaming response carries no relevant content.

READ THE SCHEMA'S COMMENTS, NOT JUST THE TYPES. Every description field in
the schema below carries the WHY behind a rule — PERSISTENCE, DIVISION OF
RESPONSIBILITY, STREAMING CONTRACT, MULTI-INTENT, CATALOG, the per-intent
semantics, and the per-argument constraints. Read each description field
carefully and let it guide your choice. The descriptions explain when each
target_action is correct and what each intent requires once chosen; do not
skim past them.

Canonical IntentDocument schema (read every description, not just the
normative type/enum bits):

""" + _CANONICAL_INTENT_DOCUMENT_SCHEMA + """

Your structured output (the JSON you return) is a simplified projection of the
canonical IntentDocument. Return ONLY this JSON, no surrounding text:

{
  "target_action": "nonsense" | "specialtySearch" | "findAProvider" | "closeConnection200",
  "complaint": string | null,
  "geography": { "state": string | null, "city": string | null, "county": string | null, "zip": string | null } | null,
  "prompt_text": string | null
}

The Python code translates your output into the canonical IntentDocument:
  - target_action: copied verbatim onto IntentDocument.target_action.
  - complaint: becomes the complaint Argument on IntentSpecialtySearch /
    IntentFindAProvider. Required when target_action is "specialtySearch" or
    "findAProvider"; MUST be null otherwise.
  - geography: becomes the geography Argument (JSON-encoded) on
    IntentFindAProvider. Required when target_action is "findAProvider"; MUST
    be null otherwise (including specialtySearch). The schema's geography
    sufficiency rule (zip OR state OR state+city OR state+county) is binding;
    city or county WITHOUT state is NOT sufficient — set target_action to
    "specialtySearch" instead.
  - prompt_text: streamed to the user as a clarification question. Required
    when target_action is "closeConnection200"; MUST be null otherwise.

Decision rules (applied in this order):
  1. Evaluate the user's latest utterance ALONE first. If it is gibberish,
     random characters, a single nonsense word, or otherwise not a real
     request, set target_action to "nonsense" regardless of any prior turn
     context. Prior context never converts gibberish into a real request.
  2. If the utterance is a real request and you can extract a healthcare
     complaint AND a usable geography (per the schema's sufficiency rule),
     set target_action to "findAProvider" and populate both complaint and
     geography.
  3. If the utterance is a real request and you can extract a complaint but
     geography is missing or insufficient, set target_action to
     "specialtySearch" and populate only complaint.
  4. If the utterance is a real request but you cannot extract a complaint
     or recognize a healthcare ask, set target_action to "closeConnection200"
     and populate prompt_text with a brief friendly clarification question.

State codes are 2-letter USPS uppercase. ZIP codes are 5 digits.
"""


class Request(BaseModel):
    """No payload — UM reads the utterance off deps.user_object."""
    model_config = {"extra": "ignore"}


class Response(BaseModel):
    """UM writes its IntentDocument to deps.user_object.intent. The Response
    just carries the target_action for the router's convenience."""
    target_action: str


def _latest_utterance_text(deps: AgentDeps) -> str:
    utterances = deps.user_object.session_conversation_history.utterances
    if not utterances:
        return ""
    last = utterances[-1]
    if isinstance(last, dict):
        return str(last.get("text", "")).strip()
    return ""


_classifier_agent = Agent(
    _LLM_MODEL,
    output_type=_ClassifierOutput,
    system_prompt=_CLASSIFIER_SYSTEM_PROMPT,
)


async def _call_classifier_llm(
    utterance_text: str, prior: Optional[IntentDocument],
) -> _ClassifierOutput:
    """Single LLM call: classify the utterance and (if needed) generate a
    clarification prompt. Returns the structured output."""
    prior_summary = "(no prior turns)"
    if prior is not None:
        prior_summary = (
            f"Prior target_action was {prior.target_action!r}. Prior intents tracked: "
            f"{[i.name for i in prior.intents]}."
        )

    user_msg = (
        f"User's latest utterance: {utterance_text!r}\n"
        f"{prior_summary}\n\n"
        "Classify and return the structured output."
    )
    _log.debug("UM classifier input: %s", user_msg)
    result = await _classifier_agent.run(user_msg)
    _log.debug("UM classifier output: %s", result.output.model_dump_json())
    return result.output


def _build_nonsense_intent(utterance_text: str) -> IntentNonsense:
    return IntentNonsense(
        name="nonsense",
        arguments=[
            Argument(name="utterance", value=utterance_text, type="string", required=True),
            Argument(name="is_nonsense", value="true", type="boolean", required=True),
        ],
    )


def _build_specialty_search_intent(complaint: str) -> IntentSpecialtySearch:
    return IntentSpecialtySearch(
        name="specialtySearch",
        arguments=[
            Argument(name="complaint", value=complaint, type="string", required=True),
        ],
    )


def _build_find_a_provider_intent(complaint: str, geography: dict[str, Any]) -> IntentFindAProvider:
    return IntentFindAProvider(
        name="findAProvider",
        arguments=[
            Argument(name="complaint", value=complaint, type="string", required=True),
            Argument(
                name="geography",
                value=json.dumps({k: v for k, v in geography.items() if v}),
                type="object",
                required=True,
            ),
        ],
    )


def _build_close_connection_200_intent() -> IntentCloseConnection200:
    return IntentCloseConnection200(
        name="closeConnection200",
        arguments=[
            Argument(name="close_connection", value="true", type="boolean", required=True),
        ],
    )


def _geography_sufficient(geo: Optional[dict[str, Any]]) -> bool:
    if not geo:
        return False
    state = (geo.get("state") or "").strip()
    city = (geo.get("city") or "").strip()
    county = (geo.get("county") or "").strip()
    zip_code = (geo.get("zip") or "").strip()
    if zip_code:
        return True
    if state:
        return True
    return False


def _merge_intent_into_document(
    document: IntentDocument,
    new_intent: Any,
    new_target_action: str,
) -> IntentDocument:
    """Replace the existing entry for new_intent.name (if any) with the new
    entry, leaving other entries intact. Set target_action."""
    keep = [i for i in document.intents if i.name != new_intent.name]
    keep.append(new_intent)
    return IntentDocument(
        target_action=new_target_action,  # type: ignore[arg-type]
        intents=keep[:3],
    )


class UtteranceManagerTool(ChatHealthyTool):
    """First-class tool the router dispatches to for op == 'utterance'.
    Classifies the latest utterance and writes the resulting IntentDocument
    onto deps.user_object.intent before returning to the router."""

    TOOL_NAME = "utterance_manager"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        utterance_text = _latest_utterance_text(deps)
        if not utterance_text:
            raise ValueError("UtteranceManager: no utterance text on user_object")

        prior = deps.user_object.intent

        llm_result = await _call_classifier_llm(utterance_text, prior)

        target_action = llm_result.target_action

        document = prior or IntentDocument(
            target_action="closeConnection200",
            intents=[_build_close_connection_200_intent()],
        )

        if target_action == "nonsense":
            new_doc = _merge_intent_into_document(
                document, _build_nonsense_intent(utterance_text), target_action,
            )

        elif target_action == "specialtySearch":
            complaint = (llm_result.complaint or "").strip()
            if not complaint:
                raise ValueError(
                    "UtteranceManager classifier set target_action=specialtySearch "
                    "but produced no complaint"
                )
            new_doc = _merge_intent_into_document(
                document,
                _build_specialty_search_intent(complaint),
                target_action,
            )

        elif target_action == "findAProvider":
            complaint = (llm_result.complaint or "").strip()
            geography = llm_result.geography.model_dump() if llm_result.geography else {}
            if not complaint:
                raise ValueError(
                    "UtteranceManager classifier set target_action=findAProvider "
                    "but produced no complaint"
                )
            if not _geography_sufficient(geography):
                raise ValueError(
                    "UtteranceManager classifier set target_action=findAProvider "
                    "but geography is insufficient (need zip, state, state+city, "
                    "or state+county)"
                )
            new_doc = _merge_intent_into_document(
                document,
                _build_find_a_provider_intent(complaint, geography),
                target_action,
            )

        elif target_action == "closeConnection200":
            prompt = (llm_result.prompt_text or "").strip()
            if not prompt:
                raise ValueError(
                    "UtteranceManager classifier set target_action=closeConnection200 "
                    "but produced no prompt_text"
                )
            deps.stream({"kind": "prompt", "data": {"text": prompt}})
            new_doc = _merge_intent_into_document(
                document, _build_close_connection_200_intent(), target_action,
            )

        else:
            raise ValueError(
                f"UtteranceManager classifier returned out-of-catalog "
                f"target_action {target_action!r}"
            )

        deps.user_object.intent = new_doc

        # Streaming-contract flush: yield once so any queued events drain
        # before we return control to the router.
        await asyncio.sleep(0)

        return self.Response(target_action=new_doc.target_action)


TOOL = UtteranceManagerTool()
