# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""UtteranceManager — the classifier ChatHealthyTool.

Reads up to the last 10 USER utterances off deps.user_object, calls an LLM
to classify the latest utterance in context, builds the canonical
IntentDocument (including any pending_disambiguation marker and the
LLM-authored top-level user_message), streams the user_message if
present, writes the document back to deps.user_object.intent, and
returns.

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
    PendingDisambiguation,
)

_log = logging.getLogger("shared_services.utterance_manager")


_LLM_MODEL = "google-gla:gemini-2.5-flash"
_MAX_USER_UTTERANCE_WINDOW = 10  # EPIC-002-F-010-S-001-REQ-B-006


# ────────────────────────────────────────────────────────────────────
# Classifier structured output
# ────────────────────────────────────────────────────────────────────


class _GeoFacts(BaseModel):
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None


class _PendingDisambiguationOut(BaseModel):
    """LLM-emitted pending-disambiguation marker. Mirrors the canonical
    PendingDisambiguation shape on the IntentDocument."""
    kind: str
    candidate: dict[str, Any] = Field(default_factory=dict)


class _ClassifierOutput(BaseModel):
    """Structured output of the UM classifier LLM. Field names match the
    canonical IntentDocument so downstream Python can copy them through
    with minimal translation."""

    target_action: Literal[
        "nonsense", "specialtySearch", "findAProvider", "closeConnection200",
        "safetyLockout",
    ]
    complaint: Optional[str] = None
    geography: Optional[_GeoFacts] = None
    user_message: Optional[str] = None
    pending_disambiguation: Optional[_PendingDisambiguationOut] = None
    # Audit-only label carried on the safetyLockout intent. LockoutTool
    # writes it onto {env}_Safety.emergency_incidents but renders the
    # user-facing prose from the trigger utterance, not from this label.
    lockout_reason: Optional[str] = None


# ────────────────────────────────────────────────────────────────────
# Embedded canonical schema (kept in sync with Website/schemas/…)
# ────────────────────────────────────────────────────────────────────

_CANONICAL_INTENT_DOCUMENT_SCHEMA = r"""{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://dev.chathealthy.ai/schemas/ChatHealthyUtteranceManagerOutputSchema.json",
  "title": "ChatHealthy UtteranceManager Output",
  "description": "Structured output of the UtteranceManager (UM) classifier used by the ChatHealthy Universal Router (UR) to call the correct tool to do work on behalf of an end user. UM examines the user's latest utterance with others as necessary, and the prior intent document carried on user_object.intent.

PERSISTENCE: this document lives on the session as `user_object.intent`. UM's first action on every invocation is to read user_object.intent and construct the Pydantic object from it; if the field is empty (first turn of the session), UM constructs a fresh document and assigns it to user_object.intent. UM updates the document over the course of its classification work and writes the updated version back to user_object.intent BEFORE returning control to UR. The document survives across turns — UM accumulates intents and arguments turn-by-turn rather than starting fresh each time.

DIVISION OF RESPONSIBILITY:
  * UM (producing side) — before emitting, validates that the chosen target_action's intent entry exists in intents[] and that every required argument for that intent has a value that parses to its declared type. UM enforces every semantic rule the JSON Schema cannot reach (e.g., the sufficiency rule on findAProvider.geography). If any required argument cannot be filled, UM streams a clarification prompt to the user and sets target_action to closeConnection200.
  * UR (dispatch + compliance check) — UR validates the document before dispatching: target_action MUST be one of the catalog enum values; target_action MUST correspond to a name in intents[]; that intent entry MUST have all of its required: true arguments present with non-empty values that parse to their declared types; every semantic rule the JSON Schema cannot reach (e.g., findAProvider.geography sufficiency) MUST hold. If any check fails UR raises immediately at the smallest possible scope. UR is responsible for not making bad calls.
  * Each tool (consuming side) — receives the document (or its intent's slice), reads the arguments it cares about, applies any tool-specific business rules, executes its work. Tools add their own defensive asserts without relying on UR having pre-validated.

STREAMING CONTRACT: every dispatched tool flushes the stream (awaits at least one event-loop tick after its last deps.stream(...) call) before returning control to UR, so when closeConnection200 is the next dispatch all bytes prior tools wrote are on the wire before the StreamingResponse terminates.

MULTI-INTENT: intents[] can carry more than one entry. UM may have detected findAProvider on turn 1 (geography missing) and still be tracking that intent on turn 2 when the user provides the missing geography. UM may also have detected a secondary intent (e.g., a safety concern) in the same utterance. target_action is always the single intent UR actions this turn; the other intents remain in the document for future turns.

CATALOG (this deploy's closed set): nonsense, specialtySearch, findAProvider, closeConnection200, safetyLockout. UM clamps to one of these five; UR raises on any out-of-catalog target_action. safetyLockout is special — UR may dispatch it WITHOUT calling UM whenever user_object.is_locked_out is true (UR hydrates that flag from {env}_Safety.emergency_incidents by IP at /gate entry); UM only emits target_action=safetyLockout on a turn where the user is NOT yet locked out and the classifier judges the utterance signals immediate medical attention.",
  "type": "object",
  "additionalProperties": false,
  "required": ["target_action", "intents"],
  "properties": {
    "target_action": {
      "description": "The single action UR must dispatch next. UM picks one of the catalog values; UR's match/case is keyed off this field. MUST correspond to a name in intents[] — UM enforces that invariant before emitting. NOTE: when user_object.is_locked_out is true (UR stamps it from {env}_Safety.emergency_incidents during hydration), UR dispatches safetyLockout directly without calling UM at all; UM's only role for safetyLockout is to recognise immediate-medical-attention utterances on turns where the user is NOT yet locked out and emit target_action=safetyLockout so UR locks them.",
      "enum": ["nonsense", "specialtySearch", "findAProvider", "closeConnection200", "safetyLockout"]
    },
    "intents": {
      "type": "array",
      "description": "All intents UM has recognized for this document across turns. Each entry is one of the typed intent shapes defined in $defs. uniqueItems is enforced on the (name) field via the per-shape const — two entries with the same name MUST NOT exist in this array.",
      "minItems": 1,
      "maxItems": 4,
      "uniqueItems": true,
      "items": {
        "oneOf": [
          { "$ref": "#/$defs/IntentNonsense" },
          { "$ref": "#/$defs/IntentSpecialtySearch" },
          { "$ref": "#/$defs/IntentFindAProvider" },
          { "$ref": "#/$defs/IntentCloseConnection200" },
          { "$ref": "#/$defs/IntentSafetyLockout" }
        ]
      }
    },
    "user_message": {
      "type": "string",
      "maxLength": 4096,
      "description": "LLM-authored prose intended for the user (clarification question, follow-up, friendly framing). When non-empty UM streams it as {kind:'prompt', data:{text: user_message}} and awaits an event-loop tick to flush before returning to UR. When absent or empty UM streams nothing. The LLM owns this prose; no hardcoded chat strings appear in UM, NonsenseTool, CloseConnection200Tool, or any other component on the dispatch path."
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
          "maxLength": 32768
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
    "PendingDisambiguation": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind", "candidate"],
      "description": "Marker carried on an intent entry when the classifier could not fully fill a required slot but has a plausible candidate value for it. UM sets it; UR does not dispatch the intent's action while pending_disambiguation is set. UM clears it on a subsequent turn when the user resolves the disambiguation (yes/no answer), at which point the slot is filled and target_action is upgraded.",
      "properties": {
        "kind": {
          "type": "string",
          "minLength": 1,
          "maxLength": 64,
          "description": "The disambiguation category. FindCare's geography slot uses 'geography_state'."
        },
        "candidate": {
          "type": "object",
          "description": "Structured candidate value the classifier proposed (e.g., {\"state\":\"WI\"})."
        },
        "scaffolding": {
          "type": "object",
          "description": "Optional free-form context the next-turn resolver needs."
        }
      }
    },
    "IntentNonsense": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for an utterance UM classified as gibberish or otherwise not a real request. When target_action is nonsense, UR dispatches to NonsenseTool, whose deploy-1 behavior is to bump the silly-question counter on user_object using the utterance and is_nonsense arguments.",
      "properties": {
        "name": {
          "const": "nonsense"
        },
        "pending_disambiguation": { "$ref": "#/$defs/PendingDisambiguation" },
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
                  {
                    "properties": {
                      "name": { "const": "utterance" },
                      "type": { "const": "string" },
                      "required": { "const": true },
                      "value": { "minLength": 1, "maxLength": 4096 }
                    }
                  }
                ],
                "description": "Exact text the user typed that was classified as nonsense. NonsenseTool consumes this verbatim for the silly-question audit record."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  {
                    "properties": {
                      "name": { "const": "is_nonsense" },
                      "type": { "const": "boolean" },
                      "required": { "const": true },
                      "value": { "const": "true" }
                    }
                  }
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
      "description": "Intent entry for an utterance where UM has extracted a complaint phrase but no usable geography. UR dispatches to SpecialtyFilter, which translates the complaint into NUCC specialty codes; the FE renders the specialty list so the user can see candidate provider types even before they tell us where they are. specialtySearch is the partial-information counterpart to findAProvider; once the user supplies geography on a subsequent turn UM upgrades the target_action to findAProvider. The optional nucc_codes argument carries the SpecialtyFilter output and persists across turns so UR can skip re-running SpecialtyFilter when the user resolves a pending disambiguation.",
      "properties": {
        "name": {
          "const": "specialtySearch"
        },
        "pending_disambiguation": { "$ref": "#/$defs/PendingDisambiguation" },
        "arguments": {
          "type": "array",
          "description": "One required argument (complaint) and one optional argument (nucc_codes) carrying the cached SpecialtyFilter output.",
          "minItems": 1,
          "maxItems": 2,
          "uniqueItems": true,
          "items": {
            "oneOf": [
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "complaint" }, "type": { "const": "string" }, "required": { "const": true }, "value": { "minLength": 1, "maxLength": 1024 } } }
                ],
                "description": "Natural-language complaint phrase UM extracted (e.g. 'back pain'). SpecialtyFilter translates this into NUCC codes via its own LLM call."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  { "properties": { "name": { "const": "nucc_codes" }, "type": { "const": "array" }, "required": { "const": false }, "value": { "minLength": 2, "maxLength": 4096 } } }
                ],
                "description": "Cached SpecialtyFilter output. value is a JSON-encoded array of {code, name, score, ...} objects. UR writes this after SpecialtyFilter runs the first time and reads it on follow-up turns to avoid re-running SpecialtyFilter on the same complaint."
              }
            ]
          }
        }
      }
    },
    "IntentFindAProvider": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for an utterance UM classified as the user looking for a healthcare provider. When target_action is findAProvider, UR dispatches SpecialtyFilter (if nucc_codes has not been cached) and then ProviderSearch. When pending_disambiguation is set on this entry, target_action stays at specialtySearch (or another partial-information state) until the user resolves the disambiguation; geography may be partial on the entry while pending_disambiguation is set, and UR enforces the geography sufficiency rule only when it is about to dispatch the findAProvider action. The optional nucc_codes argument carries the SpecialtyFilter output across turns so UR can skip re-running SpecialtyFilter on the resolution turn.",
      "properties": {
        "name": {
          "const": "findAProvider"
        },
        "pending_disambiguation": { "$ref": "#/$defs/PendingDisambiguation" },
        "arguments": {
          "type": "array",
          "description": "Up to three arguments: complaint (required), geography (required when dispatched as findAProvider; may be partial while pending_disambiguation is set), and an optional nucc_codes carrying the cached SpecialtyFilter output.",
          "minItems": 1,
          "maxItems": 3,
          "uniqueItems": true,
          "items": {
            "oneOf": [
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  {
                    "properties": {
                      "name": { "const": "complaint" },
                      "type": { "const": "string" },
                      "required": { "const": true },
                      "value": { "minLength": 1, "maxLength": 1024 }
                    }
                  }
                ],
                "description": "Natural-language complaint phrase UM extracted (e.g. 'back pain', 'persistent cough'). SpecialtyFilter downstream translates this into NUCC codes via its own LLM call. value is the phrase as the user expressed it (or as UM paraphrased it from the prior conversation history)."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  {
                    "properties": {
                      "name": { "const": "geography" },
                      "type": { "const": "object" },
                      "required": { "const": true },
                      "value": { "minLength": 2, "maxLength": 512 }
                    }
                  }
                ],
                "description": "Structured location facts. value is a JSON-encoded object the consumer parses with json.loads. The parsed object MUST have at minimum one of: (a) zip as a 5-digit ZIP code, (b) state as a 2-letter USPS code, (c) state plus city, or (d) state plus county when target_action is findAProvider. While pending_disambiguation is set on this entry the geography may be partial (e.g., city alone) and UR does not enforce the sufficiency rule because the entry's action is not being dispatched."
              },
              {
                "allOf": [
                  { "$ref": "#/$defs/Argument" },
                  {
                    "properties": {
                      "name": { "const": "nucc_codes" },
                      "type": { "const": "array" },
                      "required": { "const": false },
                      "value": { "minLength": 2, "maxLength": 4096 }
                    }
                  }
                ],
                "description": "Cached SpecialtyFilter output. value is a JSON-encoded array of {code, name, score, ...} objects. UR writes this after SpecialtyFilter runs the first time and reads it on follow-up turns to avoid re-running SpecialtyFilter on the same complaint."
              }
            ]
          }
        }
      }
    },
    "IntentSafetyLockout": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for the safety-lockout action. UM emits this on a turn where the LLM classifier determines the user's utterance signals immediate medical attention (the prompt asks the LLM to make that determination 100%; no hardcoded keyword match outside the prompt). UR also dispatches LockoutTool when user_object.is_locked_out is already true (UR's pre-UM hydration stamped the flag from {env}_Safety.emergency_incidents); in that path UM is NOT called, so this intent entry only appears on the locking-now turn. LockoutTool reads the trigger utterance from user_object.session_conversation_history (the latest person utterance) and the IP from user_object.ip_address; no required arguments on this intent.",
      "properties": {
        "name": {
          "const": "safetyLockout"
        },
        "pending_disambiguation": { "$ref": "#/$defs/PendingDisambiguation" },
        "arguments": {
          "type": "array",
          "description": "Exactly one argument: lockout_reason (string). UM provides a short LLM-authored category label (e.g. 'cardiac symptoms', 'severe trauma', 'overdose') purely for the audit record; the user-facing 'why' is rendered by LockoutTool from the verbatim trigger utterance, not from this label.",
          "minItems": 1,
          "maxItems": 1,
          "uniqueItems": true,
          "items": {
            "allOf": [
              { "$ref": "#/$defs/Argument" },
              {
                "properties": {
                  "name": { "const": "lockout_reason" },
                  "type": { "const": "string" },
                  "required": { "const": true },
                  "value": { "minLength": 1, "maxLength": 256 }
                }
              }
            ],
            "description": "Audit-only label for the safety classification. LockoutTool stores it on the {env}_Safety.emergency_incidents record alongside the trigger_message but does NOT include it in user-facing prose."
          }
        }
      }
    },
    "IntentCloseConnection200": {
      "type": "object",
      "additionalProperties": false,
      "required": ["name", "arguments"],
      "description": "Intent entry for the close-with-200 action UR dispatches. UM authors the user-facing prose (top-level user_message) on any turn that needs to talk to the user; UR streams it and is responsible for closing the connection. The closeConnection200 tool has no knowledge of what was said to the user or what the user said to elicit the response — its only job is to close the StreamingResponse with HTTP 200 OK.",
      "properties": {
        "name": {
          "const": "closeConnection200"
        },
        "pending_disambiguation": { "$ref": "#/$defs/PendingDisambiguation" },
        "arguments": {
          "type": "array",
          "description": "Exactly one argument: close_connection (boolean, always true).",
          "minItems": 1,
          "maxItems": 1,
          "uniqueItems": true,
          "items": {
            "allOf": [
              { "$ref": "#/$defs/Argument" },
              {
                "properties": {
                  "name": { "const": "close_connection" },
                  "type": { "const": "boolean" },
                  "required": { "const": true },
                  "value": { "const": "true" }
                }
              }
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
You are the utterance classifier for ChatHealthy.ai (the UtteranceManager,
"UM" in the schema below). Your job: examine the user's recent utterance
window AND the prior IntentDocument carried on user_object.intent, and
return a structured output that captures (a) the next target_action the
Universal Router (UR) must dispatch, (b) any per-intent
pending_disambiguation marker, and (c) the top-level user_message you
want streamed to the user before UR dispatches the action. Downstream
Python translates your output into the canonical IntentDocument.

WHERE YOU SIT IN THE PROCESS:
  - The user types an utterance into the ChatHealthy.ai client.
  - The client POSTs the utterance to SharedServices /gate.
  - /gate authenticates the session, appends the new utterance to
    user_object.session_conversation_history.utterances, and hands the
    user_object to the Universal Router (UR).
  - UR dispatches you (UM) FIRST. You receive up to the last 10 dialogue
    lines (person AND system, interleaved, oldest-first) AND the prior
    IntentDocument off user_object.intent. Each line is prefixed with
    "user: " or "system: " — the system lines are prior user_message
    prose YOU wrote to the user on earlier turns.
  - You return your structured output.
  - Python translates your output into the canonical IntentDocument and
    writes it back onto user_object.intent.
  - If user_message is non-empty in your output, UM streams it as
    {kind:"prompt", data:{text: user_message}} and flushes before
    returning to UR. THIS IS THE ONLY PROSE THE USER SEES FROM
    THIS TURN. No other component streams chat text on this turn.
  - UR then reads user_object.intent.target_action and dispatches the
    matching downstream tool: NonsenseTool, SpecialtyFilter alone
    (specialtySearch), SpecialtyFilter+ProviderSearch (findAProvider),
    or CloseConnection200Tool.

READ THE SCHEMA'S COMMENTS. Every description field in the schema below
carries the WHY behind a rule. Read them and let them guide your output.

Canonical IntentDocument schema (read every description, not just the
normative type/enum bits):

""" + _CANONICAL_INTENT_DOCUMENT_SCHEMA + """

Your structured output is a JSON object with these fields:

{
  "target_action": "nonsense" | "specialtySearch" | "findAProvider" | "closeConnection200",
  "complaint": string | null,
  "geography": { "state": string | null, "city": string | null, "county": string | null, "zip": string | null } | null,
  "user_message": string | null,
  "pending_disambiguation": { "kind": string, "candidate": object } | null
}

How the Python translator uses each field:
  - target_action: copied to IntentDocument.target_action.
  - complaint: becomes the complaint Argument on IntentSpecialtySearch
    and/or IntentFindAProvider. Required whenever target_action involves
    a complaint (specialtySearch, findAProvider, or
    ambiguous-but-resolvable holding pattern).
  - geography: becomes the geography Argument on IntentFindAProvider
    (JSON-encoded). May be partial (e.g., {"city":"milwaukee"} alone)
    when pending_disambiguation is set. Must be sufficient (zip, state,
    state+city, or state+county) when target_action is findAProvider.
  - user_message: streamed to the user verbatim as {kind:"prompt"}.
    The LLM owns this prose; the runtime never substitutes hardcoded
    chat strings. Set it on any turn that needs to talk to the user.
  - pending_disambiguation: set when you cannot fully fill a required
    slot but have a plausible candidate. Holds {kind, candidate}.
    For FindCare's geography slot use kind="geography_state" and
    candidate={"state":"WI"} (or similar). Persists across turns until
    cleared.

DECISION RULES (apply in this order):

  0. SAFETY LOCKOUT TAKES PRECEDENCE OVER EVERY OTHER RULE, INCLUDING
     PENDING DISAMBIGUATION. Before anything else, judge whether the
     latest user utterance signals that the user (or someone they are
     speaking for) needs IMMEDIATE MEDICAL ATTENTION right now. The
     classifier — not a keyword list — owns this determination. Make it
     yourself with care; the only hardcoded match in the runtime is the
     operator backdoor literal in LockoutTool, and that's intentionally
     not your concern.

     A signal of immediate medical attention typically has all three of:
       - a specific body location or named body system (chest, head,
         airway, etc.) OR a specific named acute condition (overdose,
         seizure, stroke symptoms, severe bleeding, anaphylaxis, etc.),
       - acute onset or severity stated or strongly implied,
       - a life-threat or limb-threat or sensorium-threat implication.
     Casual phrasing like "I want a chest pain doctor" or "my back
     hurts, can I find a chiro" or "I need someone for my anxiety" is
     NOT immediate medical attention — those are provider-search
     intents. Statements like "I'm having crushing chest pain right
     now and can't breathe" or "I think I'm having a stroke" or "I
     took too many pills" or "I want to hurt myself tonight" ARE.

     Example keyword categories you MAY consult for context but MUST
     NOT match on alone: cardiac (chest crushing, can't breathe,
     numb arm), cerebrovascular (face drooping, slurred speech, sudden
     weakness), trauma (gunshot, bleeding heavily, unconscious),
     toxicological (overdose, took too many, swallowed bleach),
     self-harm (hurt myself, end my life, suicide). The presence of
     one of these words does NOT mean the utterance is an emergency;
     YOU decide based on the full sentence and its tone.

     When the determination is yes:
       - Set target_action to "safetyLockout".
       - Add (or replace) the corresponding intent entry in intents[]
         with name="safetyLockout" and a single lockout_reason
         argument (short audit-label string — e.g. "cardiac symptoms",
         "self-harm statement", "trauma"; max 256 chars). lockout_reason
         is for the {env}_Safety.emergency_incidents audit record, NOT
         for the user-facing prose.
       - Leave user_message empty; LockoutTool will author the
         deterministic "when you said '...'" + 911 + operator phone
         text from the trigger utterance directly.
       - Do NOT also emit nonsense / specialtySearch / findAProvider
         on the same turn — safetyLockout supersedes them.

  1. PENDING DISAMBIGUATION TAKES PRECEDENCE OVER EVERY OTHER RULE.
     Before considering anything else, check the prior IntentDocument
     summary. If it lists a pending_disambiguation, the latest user
     utterance MUST be interpreted as a yes/no/answer to that pending
     question. The prior system: line in the transcript is the question
     you (UM) asked the user on the previous turn — the user's latest
     line is the answer.

       - Affirmative ("yes", "yeah", "yep", "y", "sure", "ok", "right",
         "correct", "uh-huh", "confirmed", any equivalent): fill the
         candidate value into geography (or whichever slot the
         pending_disambiguation names) and upgrade target_action to
         the now-fully-specified action (e.g., findAProvider). DO NOT
         re-emit pending_disambiguation. user_message MAY be a brief
         acknowledgement or null.

       - Negative ("no", "nope", "nah", "n", "negative", "wrong", any
         equivalent): the candidate was wrong. Leave target_action at
         the partial-information action; emit a follow-up user_message
         asking a different way (e.g., "Which state did you mean?");
         pending_disambiguation MAY be re-emitted with a refined
         candidate or left null if you cannot guess.

       - Direct answer (e.g., user replies with the actual missing
         value: "Wisconsin" / "WI" / "michigan"): fill that value
         into the slot, upgrade target_action, do not re-emit pending.

     A "yes" utterance on a turn with a pending disambiguation is
     NEVER nonsense. NEVER classify it as nonsense. The pending
     question gives the "yes" its full meaning.

  2. If not resolving a prior disambiguation, evaluate the latest
     utterance ALONE. If it is gibberish, random characters, a single
     nonsense word, or otherwise not a real request, set target_action
     to "nonsense". Set user_message to a friendly clarification line.
     Prior context never converts gibberish into a real request.

  3. If the utterance is a real request with a clear healthcare
     complaint AND a fully usable geography (zip, state, state+city, or
     state+county), set target_action to "findAProvider" and populate
     complaint + geography. user_message optional.

  4. If the utterance is a real request with a complaint and the
     geography mentions a place name but NOT a state (e.g., "milwaukee"
     alone), set target_action to "specialtySearch" so SpecialtyFilter
     still renders. Populate complaint and a PARTIAL geography (city
     only). Set pending_disambiguation = {"kind":"geography_state",
     "candidate":{"state": YOUR-BEST-GUESS}} and set user_message to
     propose the candidate (e.g., "Did you mean Milwaukee, Wisconsin?").

  5. If the utterance has a complaint but no location at all, set
     target_action to "specialtySearch". Populate complaint. Set
     user_message asking for a location. Do NOT set
     pending_disambiguation (there's nothing to confirm).

  6. If the utterance is a real request but you cannot extract a
     complaint or recognize a healthcare ask, set target_action to
     "closeConnection200" and set user_message to a brief friendly
     clarification.

State codes are 2-letter USPS uppercase. ZIP codes are 5 digits.
"""


# ────────────────────────────────────────────────────────────────────
# Request / Response
# ────────────────────────────────────────────────────────────────────


class Request(BaseModel):
    """UM accepts two trigger types per EPIC-002-F-010-S-003-REQ-B-001.

    trigger_type='utterance' (default): UM reads the latest person
    utterance off deps.user_object.session_conversation_history and runs
    the classifier prompt. This is the interpret path.

    trigger_type='manufacture': UR has dispatched UM with a
    manufacture_utterance_reason dict of structured facts. UM does NOT
    read free-text; it runs the manufacture prompt with the reason +
    prior dialogue and authors a brief user-facing user_message.
    target_action is always closeConnection200 on this path
    (EPIC-002-F-010-S-003-REQ-B-002).
    """
    model_config = {"extra": "ignore"}
    trigger_type: Literal["utterance", "manufacture"] = "utterance"
    manufacture_utterance_reason: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    """UM writes its IntentDocument to deps.user_object.intent. The Response
    just carries the target_action for the router's convenience."""
    target_action: str


# ────────────────────────────────────────────────────────────────────
# Utterance window (EPIC-002-F-010-S-001-REQ-B-006)
# ────────────────────────────────────────────────────────────────────


def _recent_transcript(deps: AgentDeps, max_count: int = _MAX_USER_UTTERANCE_WINDOW) -> list[str]:
    """Return the most recent up-to-max_count dialogue lines (person AND
    system), oldest first, each rendered as 'user: <text>' or
    'system: <text>'. The narrative form lets the LLM resolve follow-up
    turns like 'yes' against the system's prior proposal."""
    out: list[str] = []
    utterances = deps.user_object.session_conversation_history.utterances
    for u in reversed(utterances):
        actor = getattr(u, "actor", None) or (u.get("actor") if isinstance(u, dict) else None)
        text = getattr(u, "text", None) or (u.get("text") if isinstance(u, dict) else "")
        if actor not in ("person", "system") or not text:
            continue
        label = "user" if actor == "person" else "system"
        out.append(f"{label}: {text}")
        if len(out) >= max_count:
            break
    return list(reversed(out))


# ────────────────────────────────────────────────────────────────────
# Classifier call
# ────────────────────────────────────────────────────────────────────


_classifier_agent = Agent(
    _LLM_MODEL,
    output_type=_ClassifierOutput,
    system_prompt=_CLASSIFIER_SYSTEM_PROMPT,
)


def _summarize_prior(prior: Optional[IntentDocument]) -> str:
    if prior is None:
        return "(no prior turns)"
    parts = [
        f"Prior target_action was {prior.target_action!r}.",
        f"Prior intents tracked: {[i.name for i in prior.intents]}.",
    ]
    if prior.user_message:
        parts.append(f"Prior user_message was: {prior.user_message!r}.")
    for entry in prior.intents:
        pd = getattr(entry, "pending_disambiguation", None)
        if pd is not None:
            parts.append(
                f"Prior pending_disambiguation on {entry.name}: "
                f"kind={pd.kind!r} candidate={pd.candidate!r}."
            )
    return " ".join(parts)


_MANUFACTURE_SYSTEM_PROMPT = """You are the ChatHealthy Utterance Manager
running in MANUFACTURE mode (EPIC-002-F-010-S-003).

The user did NOT type a free-text utterance this turn. The user clicked
a structured UI control (Apply Filter, future selected-for-evaluation
gates, future pagination edges) and the Universal Router has decided
the system needs to ask the user something before it can act. UR has
populated `manufacture_utterance_reason` with a structured dict of
hardcoded, NON-user-facing facts describing why a manufactured
utterance is needed and what slot is missing.

YOUR JOB: read the facts AND the recent dialogue, then author ONE brief
user-facing message that:
  - asks the user for the missing slot,
  - reads as a natural continuation of the conversation (not a
    formulaic templated phrase that ignores prior turns),
  - is sensitive to what the user already said and what the system has
    already told them.

DO NOT classify intents. DO NOT pick a target_action — the system will
set target_action=closeConnection200 deterministically because the
manufactured utterance IS this turn's answer; the user's next free-text
reply comes back through the interpret path and resolves any pending
slot via the normal disambiguation mechanism.

DO NOT echo manufacture_utterance_reason facts verbatim. The reason
dict is internal plumbing; the user never sees keys or values.

DO NOT use templated phrases like "Please provide your location." The
whole reason this path is LLM-mediated (and not hardcoded prose in UR)
is that UR cannot author this prose deterministically. A prose output
that could have been hardcoded is a regression on
EPIC-002-F-010-S-003-REQ-B-003.

Examples of gestures you may encounter and what they typically imply:
  - gesture='apply_filter', missing_slot='geography': the user has
    refined the specialty filter and wants to apply it, but the
    system has no location yet. Ask for their location (city + state,
    state, county + state, or zip code) in a way that names what
    they just did. Vary the phrasing based on what was said before.
    Example shape (NOT verbatim): "Got it — happy to apply that filter.
    What city and state are you in?"
  - Other gestures will be added by populating new reason keys; treat
    the dict as the source of truth for what to ask.

Output: a single user_message string. Length: a sentence or two,
never a wall of text."""


class _ManufactureOutput(BaseModel):
    """Structured output of the UM manufacture-path LLM. Narrower than
    the classifier output — no target_action choice (always
    closeConnection200), no intent classification, no geography
    extraction. Just the LLM-authored user-facing user_message."""
    user_message: str = Field(min_length=1, max_length=4096)


_manufacture_agent = Agent(
    _LLM_MODEL,
    output_type=_ManufactureOutput,
    system_prompt=_MANUFACTURE_SYSTEM_PROMPT,
)


async def _call_manufacture_llm(
    transcript: list[str],
    reason: dict[str, Any],
    prior: Optional[IntentDocument],
) -> _ManufactureOutput:
    """Manufacture-path LLM call. Receives the recent dialogue + the
    structured reason facts UR populated. Authors a context-sensitive
    user_message."""
    window_block = (
        "\n".join(f"  {i+1}. {line}" for i, line in enumerate(transcript))
        if transcript else "  (no prior dialogue yet)"
    )
    reason_block = json.dumps(reason, ensure_ascii=False, indent=2)
    prior_summary = _summarize_prior(prior)
    user_msg = (
        f"Recent dialogue (last {len(transcript)} lines, oldest first):\n"
        f"{window_block}\n\n"
        f"Prior IntentDocument summary: {prior_summary}\n\n"
        f"manufacture_utterance_reason (structured facts; internal — DO NOT echo verbatim):\n"
        f"{reason_block}\n\n"
        "Author the brief user-facing user_message per the system "
        "prompt rules. Return the structured output."
    )
    _log.debug("UM manufacture input: %s", user_msg)
    result = await _manufacture_agent.run(user_msg)
    _log.debug("UM manufacture output: %s", result.output.model_dump_json())
    return result.output


async def _call_classifier_llm(
    transcript: list[str], prior: Optional[IntentDocument],
) -> _ClassifierOutput:
    """Single LLM call. Receives the recent transcript as already-labeled
    lines (each prefixed with 'user: ' or 'system: ') plus the prior
    IntentDocument summary. Classifies the latest 'user: ...' line."""
    if not transcript:
        raise ValueError("UtteranceManager: empty transcript window")
    window_block = "\n".join(f"  {i+1}. {line}" for i, line in enumerate(transcript))
    prior_summary = _summarize_prior(prior)
    user_msg = (
        f"Recent dialogue (last {len(transcript)} lines, oldest first):\n"
        f"{window_block}\n\n"
        f"Prior IntentDocument summary: {prior_summary}\n\n"
        "Classify the LATEST 'user:' line (the most recent person turn) "
        "using the decision rules in the system prompt. If the prior "
        "IntentDocument summary lists a pending_disambiguation, Rule 1 "
        "applies and overrides every other rule. Return the structured "
        "output."
    )
    _log.debug("UM classifier input: %s", user_msg)
    result = await _classifier_agent.run(user_msg)
    _log.debug("UM classifier output: %s", result.output.model_dump_json())
    return result.output


# ────────────────────────────────────────────────────────────────────
# Intent builders
# ────────────────────────────────────────────────────────────────────


def _existing_intent(document: IntentDocument, name: str) -> Optional[Any]:
    return next((i for i in document.intents if i.name == name), None)


def _cached_nucc_codes(entry: Any) -> Optional[str]:
    """Return the JSON-encoded nucc_codes argument value from an intent
    entry if present, else None."""
    if entry is None:
        return None
    for arg in entry.arguments:
        if arg.name == "nucc_codes":
            return arg.value
    return None


def _build_nonsense_intent(utterance_text: str) -> IntentNonsense:
    return IntentNonsense(
        name="nonsense",
        arguments=[
            Argument(name="utterance", value=utterance_text, type="string", required=True),
            Argument(name="is_nonsense", value="true", type="boolean", required=True),
        ],
    )


def _build_specialty_search_intent(
    complaint: str,
    nucc_codes_json: Optional[str] = None,
) -> IntentSpecialtySearch:
    args = [Argument(name="complaint", value=complaint, type="string", required=True)]
    if nucc_codes_json:
        args.append(Argument(
            name="nucc_codes", value=nucc_codes_json, type="array", required=False,
        ))
    return IntentSpecialtySearch(name="specialtySearch", arguments=args)


def _build_find_a_provider_intent(
    complaint: str,
    geography: dict[str, Any],
    nucc_codes_json: Optional[str] = None,
    pending: Optional[PendingDisambiguation] = None,
) -> IntentFindAProvider:
    args = [Argument(name="complaint", value=complaint, type="string", required=True)]
    geo_compact = {k: v for k, v in (geography or {}).items() if v}
    if geo_compact:
        args.append(Argument(
            name="geography",
            value=json.dumps(geo_compact),
            type="object",
            required=True,
        ))
    if nucc_codes_json:
        args.append(Argument(
            name="nucc_codes", value=nucc_codes_json, type="array", required=False,
        ))
    return IntentFindAProvider(
        name="findAProvider",
        arguments=args,
        pending_disambiguation=pending,
    )


def _build_close_connection_200_intent() -> IntentCloseConnection200:
    return IntentCloseConnection200(
        name="closeConnection200",
        arguments=[
            Argument(name="close_connection", value="true", type="boolean", required=True),
        ],
    )


def _build_safety_lockout_intent(lockout_reason: str) -> "IntentSafetyLockout":
    """UM emits target_action=safetyLockout when the classifier judges the
    latest utterance signals immediate medical attention. The intent
    carries a single lockout_reason audit-label argument (max 256 chars);
    LockoutTool writes it onto the {env}_Safety.emergency_incidents row
    and renders the user-facing prose from the verbatim trigger
    utterance, not from this label."""
    from UtteranceManager.intent_document import IntentSafetyLockout
    label = (lockout_reason or "").strip() or "immediate medical attention"
    return IntentSafetyLockout(
        name="safetyLockout",
        arguments=[
            Argument(
                name="lockout_reason",
                value=label[:256],
                type="string",
                required=True,
            ),
        ],
    )


def _geography_sufficient(geo: Optional[dict[str, Any]]) -> bool:
    if not geo:
        return False
    state = (geo.get("state") or "").strip()
    zip_code = (geo.get("zip") or "").strip()
    return bool(zip_code or state)


def _merge_intents(
    document: IntentDocument,
    new_intents: list[Any],
    target_action: str,
    user_message: Optional[str],
) -> IntentDocument:
    """Replace existing entries by name with the new ones, leaving any
    other entries intact. Cap at 3 to honor the schema."""
    new_names = {i.name for i in new_intents}
    keep = [i for i in document.intents if i.name not in new_names]
    keep.extend(new_intents)
    return IntentDocument(
        target_action=target_action,  # type: ignore[arg-type]
        intents=keep[-3:],
        user_message=user_message,
    )


# ────────────────────────────────────────────────────────────────────
# Tool entry point
# ────────────────────────────────────────────────────────────────────


def _to_pending(out: Optional[_PendingDisambiguationOut]) -> Optional[PendingDisambiguation]:
    if out is None:
        return None
    return PendingDisambiguation(kind=out.kind, candidate=out.candidate)


class UtteranceManagerTool(ChatHealthyTool):
    """First-class tool the router dispatches to for op == 'utterance'.
    Classifies the latest utterance and writes the resulting IntentDocument
    onto deps.user_object.intent before returning to the router."""

    TOOL_NAME = "utterance_manager"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        if request.trigger_type == "manufacture":
            return await self._run_manufacture(deps, request)
        return await self._run_interpret(deps, request)

    async def _run_manufacture(self, deps: AgentDeps, request: "Request") -> "Response":
        """Manufacture path per EPIC-002-F-010-S-003. UR populated
        request.manufacture_utterance_reason with structured facts. UM
        authors a context-sensitive user_message via the manufacture
        LLM and morphs the IntentDocument to target_action=
        closeConnection200."""
        transcript = _recent_transcript(deps)  # may be empty
        prior = deps.user_object.intent

        llm_result = await _call_manufacture_llm(
            transcript, request.manufacture_utterance_reason, prior,
        )
        user_message = (llm_result.user_message or "").strip()
        if not user_message:
            raise ValueError(
                "UtteranceManager manufacture-path: LLM returned empty user_message"
            )

        # Build the resulting IntentDocument. Carry forward any prior
        # intents[] entries (cached nucc_codes, partial geography,
        # pending_disambiguation markers) so the next-turn interpret
        # path can resolve naturally. The only thing this turn dictates
        # is target_action=closeConnection200 + the close intent entry.
        base_doc = prior or IntentDocument(
            target_action="closeConnection200",
            intents=[_build_close_connection_200_intent()],
        )
        new_doc = _merge_intents(
            base_doc,
            [_build_close_connection_200_intent()],
            target_action="closeConnection200",
            user_message=user_message,
        )

        # Stream + persist the manufactured prose, same shape as the
        # interpret path's user_message handling.
        from authentication.agent_deps import append_system_utterance
        deps.stream({"kind": "prompt", "data": {"text": user_message}})
        append_system_utterance(deps.user_object, user_message)

        deps.user_object.intent = new_doc
        await asyncio.sleep(0)
        return self.Response(target_action=new_doc.target_action)

    async def _run_interpret(self, deps: AgentDeps, request: "Request") -> "Response":
        transcript = _recent_transcript(deps)
        if not transcript:
            raise ValueError("UtteranceManager: no utterances on user_object")
        # latest_text must be the LATEST PERSON utterance (raw text, no
        # 'user: ' prefix). Read it directly off the live bucket so the
        # intent-builders get the verbatim string.
        latest_text = ""
        for u in reversed(deps.user_object.session_conversation_history.utterances):
            actor = getattr(u, "actor", None) or (u.get("actor") if isinstance(u, dict) else None)
            text = getattr(u, "text", None) or (u.get("text") if isinstance(u, dict) else "")
            if actor == "person" and text:
                latest_text = str(text).strip()
                break
        if not latest_text:
            raise ValueError("UtteranceManager: no person utterance on user_object")
        prior = deps.user_object.intent

        llm_result = await _call_classifier_llm(transcript, prior)
        target_action = llm_result.target_action
        complaint = (llm_result.complaint or "").strip()
        geography = llm_result.geography.model_dump() if llm_result.geography else {}
        user_message = (llm_result.user_message or "").strip() or None
        pending = _to_pending(llm_result.pending_disambiguation)

        base_doc = prior or IntentDocument(
            target_action="closeConnection200",
            intents=[_build_close_connection_200_intent()],
        )

        # Cache lookups so we can carry SpecialtyFilter output across turns.
        cached_specialty = _cached_nucc_codes(_existing_intent(base_doc, "specialtySearch"))
        cached_findap = _cached_nucc_codes(_existing_intent(base_doc, "findAProvider"))
        cached_nucc = cached_specialty or cached_findap

        if target_action == "safetyLockout":
            # Rule 0: UM judged immediate medical attention. user_message
            # stays empty (LockoutTool authors the verbatim "when you
            # said '...'" + 911 + operator phone text). Only the
            # lockout_reason audit-label arg lands on the intent entry.
            new_doc = _merge_intents(
                base_doc,
                [_build_safety_lockout_intent(llm_result.lockout_reason or "")],
                target_action,
                user_message=None,
            )

        elif target_action == "nonsense":
            new_doc = _merge_intents(
                base_doc,
                [_build_nonsense_intent(latest_text)],
                target_action,
                user_message,
            )

        elif target_action == "specialtySearch":
            if not complaint:
                raise ValueError(
                    "UtteranceManager classifier set target_action=specialtySearch "
                    "but produced no complaint"
                )
            built: list[Any] = [
                _build_specialty_search_intent(complaint, cached_nucc),
            ]
            # Ambiguous-but-resolvable: also park a findAProvider entry
            # with the partial geography and the pending disambiguation
            # marker, so a follow-up "yes" can upgrade it cleanly.
            if pending is not None:
                built.append(_build_find_a_provider_intent(
                    complaint,
                    geography,  # partial allowed
                    nucc_codes_json=cached_nucc,
                    pending=pending,
                ))
            new_doc = _merge_intents(base_doc, built, target_action, user_message)

        elif target_action == "findAProvider":
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
            built = [
                _build_specialty_search_intent(complaint, cached_nucc),
                _build_find_a_provider_intent(
                    complaint, geography, nucc_codes_json=cached_nucc, pending=None,
                ),
            ]
            new_doc = _merge_intents(base_doc, built, target_action, user_message)

        elif target_action == "closeConnection200":
            if not user_message:
                raise ValueError(
                    "UtteranceManager classifier set target_action=closeConnection200 "
                    "but produced no user_message"
                )
            new_doc = _merge_intents(
                base_doc,
                [_build_close_connection_200_intent()],
                target_action,
                user_message,
            )

        else:
            raise ValueError(
                f"UtteranceManager classifier returned out-of-catalog "
                f"target_action {target_action!r}"
            )

        # Stream the LLM-authored user_message before returning, per
        # REQ-B-009 and REQ-B-010. Also append it to the dialogue bucket
        # as a system utterance so it (a) appears verbatim on the splash
        # next to the prior person turn, and (b) is part of the labeled
        # transcript UM hands itself on the NEXT turn, which is what
        # gives a follow-up "yes" its referent.
        if new_doc.user_message:
            from authentication.agent_deps import append_system_utterance
            deps.stream({"kind": "prompt", "data": {"text": new_doc.user_message}})
            append_system_utterance(deps.user_object, new_doc.user_message)

        deps.user_object.intent = new_doc
        await asyncio.sleep(0)
        return self.Response(target_action=new_doc.target_action)


TOOL = UtteranceManagerTool()
