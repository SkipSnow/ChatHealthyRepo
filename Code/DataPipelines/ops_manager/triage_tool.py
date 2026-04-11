# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# TriageTool — rule-based precheck + GPT-4.1-mini fallback.
# Classifies errors as INFRASTRUCTURE, PIPELINE, or UNKNOWN.

import json
import logging
import os
import sys
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
from agent_framework.base_tool import BaseTool, ToolResult

_log = logging.getLogger("ops.triage_tool")

# Rule-based classification patterns
# Key: substring to match in error_code or error_message
# Value: classification
INFRA_PATTERNS = [
    "ConnectionFailure",
    "ServerSelectionTimeoutError",
    "AuthenticationFailed",
    "TLS handshake",
    "NetworkTimeout",
    "ReplicaSetNoPrimary",
    "AutoReconnect",
]

PIPELINE_PATTERNS = [
    "KeyError",
    "TypeError",
    "ValidationError",
    "IndexError",
    "ValueError",
    "SchemaError",
]

# Allowed repair actions (config-driven allowlist)
ALLOWED_REPAIR_ACTIONS = [
    "restart_cluster",
    "reconnect",
    "scale_up",
    "flush_connection_pool",
]


def _rule_based_classify(error_code: str, error_message: str, cluster_state: str) -> dict | None:
    """Apply deterministic rules. Returns classification dict or None if inconclusive."""

    # Cluster state rules
    if cluster_state in ("PAUSED", "REPAIRING"):
        return {
            "category": "INFRASTRUCTURE",
            "confidence": 1.0,
            "reasoning": f"Cluster state is {cluster_state} — always infrastructure.",
            "source": "rule",
            "self_repair_possible": True,
            "repair_action": "restart_cluster",
        }

    combined = f"{error_code} {error_message}"

    # Infrastructure patterns
    for pattern in INFRA_PATTERNS:
        if pattern in combined:
            return {
                "category": "INFRASTRUCTURE",
                "confidence": 0.95,
                "reasoning": f"Error matches infrastructure pattern: {pattern}",
                "source": "rule",
                "self_repair_possible": True,
                "repair_action": "restart_cluster",
            }

    # Pipeline patterns — also check traceback for Code/DataPipelines/
    for pattern in PIPELINE_PATTERNS:
        if pattern in combined:
            return {
                "category": "PIPELINE",
                "confidence": 0.90,
                "reasoning": f"Error matches pipeline code pattern: {pattern}",
                "source": "rule",
                "self_repair_possible": False,
                "repair_action": None,
            }

    return None  # inconclusive — send to LLM


def _llm_classify(error_code: str, error_message: str, cluster_state: str,
                   job_id: str, requester: str, recent_events: list) -> dict:
    """Call GPT-4.1-mini for ambiguous errors. Structured JSON output."""
    try:
        from openai import OpenAI
    except ImportError:
        _log.error("openai package not installed — cannot do LLM triage")
        return {
            "category": "UNKNOWN",
            "confidence": 0.0,
            "reasoning": "LLM triage unavailable (openai package not installed)",
            "source": "llm_unavailable",
            "self_repair_possible": False,
            "repair_action": None,
        }

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _log.warning("OPENAI_API_KEY not set — LLM triage unavailable")
        return {
            "category": "UNKNOWN",
            "confidence": 0.0,
            "reasoning": "LLM triage unavailable (no API key)",
            "source": "llm_unavailable",
            "self_repair_possible": False,
            "repair_action": None,
        }

    client = OpenAI(api_key=api_key)

    system_prompt = (
        "You are an error triage agent for a healthcare data pipeline infrastructure. "
        "Classify the error as INFRASTRUCTURE, PIPELINE, or UNKNOWN.\n\n"
        "INFRASTRUCTURE: cluster down, connection failed, timeout, auth error, network issue.\n"
        "PIPELINE: schema mismatch, data format error, business rule violation, code bug.\n"
        "UNKNOWN: cannot determine with available evidence.\n\n"
        "If INFRASTRUCTURE and self-repairable, set self_repair_possible=true and "
        f"repair_action to one of: {ALLOWED_REPAIR_ACTIONS}\n\n"
        "Respond with JSON only: {category, confidence, reasoning, self_repair_possible, repair_action}"
    )

    user_prompt = json.dumps({
        "error_code": error_code,
        "error_message": error_message[:2000],  # cap to avoid token explosion
        "cluster_state": cluster_state,
        "job_id": job_id,
        "requester": requester,
        "recent_events": recent_events[-5:],  # last 5 for context
    })

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=500,
            response_format={"type": "json_object"},
        )
        result = json.loads(response.choices[0].message.content)

        # Validate repair_action is in allowlist
        if result.get("repair_action") and result["repair_action"] not in ALLOWED_REPAIR_ACTIONS:
            _log.warning("LLM suggested disallowed repair action: %s — stripping", result["repair_action"])
            result["repair_action"] = None
            result["self_repair_possible"] = False

        result["source"] = "llm"
        return result

    except Exception as e:
        _log.error("LLM triage failed: %s", e)
        return {
            "category": "UNKNOWN",
            "confidence": 0.0,
            "reasoning": f"LLM triage failed: {e}",
            "source": "llm_error",
            "self_repair_possible": False,
            "repair_action": None,
        }


class TriageTool(BaseTool):
    """Classify errors: rule-based first, GPT-4.1-mini for ambiguous cases."""

    @property
    def name(self) -> str:
        return "triage"

    @property
    def description(self) -> str:
        return "Classify errors as INFRASTRUCTURE, PIPELINE, or UNKNOWN using rules then LLM."

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "error_code": {"type": "string"},
                "error_message": {"type": "string"},
                "cluster_state": {"type": "string"},
                "job_id": {"type": "string"},
                "requester": {"type": "string"},
                "recent_events": {"type": "array"},
            },
            "required": ["error_code", "error_message"],
        }

    def execute(self, **params) -> ToolResult:
        error_code = params.get("error_code", "")
        error_message = params.get("error_message", "")
        cluster_state = params.get("cluster_state", "UNKNOWN")
        job_id = params.get("job_id", "")
        requester = params.get("requester", "")
        recent_events = params.get("recent_events", [])

        # Step 1: Rule-based precheck
        result = _rule_based_classify(error_code, error_message, cluster_state)
        if result:
            _log.info("Rule-based triage: %s (confidence: %.2f)", result["category"], result["confidence"])
            return ToolResult(success=True, data=result)

        # Step 2: LLM triage (ambiguous)
        _log.info("Rules inconclusive for %s — invoking GPT-4.1-mini", error_code)
        result = _llm_classify(error_code, error_message, cluster_state,
                               job_id, requester, recent_events)
        _log.info("LLM triage: %s (confidence: %.2f)", result["category"], result["confidence"])
        return ToolResult(success=True, data=result)
