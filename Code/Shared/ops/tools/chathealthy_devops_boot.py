# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthy DevOps Boot — governance entry point for Claude Code.
#
# Five modes, mapped to Claude Code lifecycle hooks:
#   --mode prompt        → UserPromptSubmit: boot on first call, then predict governance path
#   --mode tool_call     → PreToolUse: gate Bash/Edit/Write
#   --mode prompt_result → Stop: audit Claude's output
#   --mode session_start → SessionStart: clear booted flag (primary — fires every new session)
#   --mode session_end   → SessionEnd: clear booted flag (secondary — only fires on clean exits)
#
# SessionStart MUST NOT perform auth-dependent work (no Anthropic API, no OAuth-gated
# operations). Its sole job is a local filesystem write of booted=false.
#
# Each brain JSON has a compliance function that returns:
#   {"comply": True}                              — pass
#   {"comply": True,  "warning": "..."}           — pass with warning
#   {"comply": False, "action": "abend|recycle",  — block
#    "reason": "..."}
#
# Singleton — state persists across hook calls within a process.

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional, Literal

try:
    from pydantic import BaseModel, Field
except ImportError:
    # Fallback if pydantic not installed
    BaseModel = object
    Field = lambda **kwargs: None

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[4] / "Code" / ".env")
except ImportError:
    pass  # dotenv not installed — secrets must be in environment already

_log = logging.getLogger("devops_boot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

REPO_ROOT = Path(__file__).resolve().parents[4]
BRAIN_DIR = REPO_ROOT / "brain" / "machine_artifacts" / "content"
# BOOT_DIR removed — chathealthyai_brainboot.json deleted per ARCH-ORPHAN-001-REQ-005
CLAUDE_DIR = REPO_ROOT / ".claude"
# Project-local gitignored cache folder (test_output/ is already gitignored in .gitignore).
# Transient boot artifacts — digest cache, req_index byte-stream — live here so
# they're (a) not committed, (b) not in the user-profile AppData/Local/Temp that
# Microsoft backs up, (c) under the repo so paths are portable across machines.
CACHE_DIR = REPO_ROOT / "test_output"
DIGEST_PATH = CACHE_DIR / "chathealthy_brain_digest.json"
SETTINGS_PATH = CLAUDE_DIR / "ChatHealthySettings.json"

# Current environment — read from ENV_PREFIX or default to dev
ENV = os.environ.get("ENV_PREFIX", os.environ.get("ENVIRONMENT", "local"))


# CV-007 propensity labels — immutable mapping to system behavior
PROPENSITY = {
    "system_abend":        {"comply": False, "scope": "system",      "action": "abend"},
    "session_abend":       {"comply": False, "scope": "session",     "action": "abend"},
    "environment_abend":   {"comply": False, "scope": "environment", "action": "abend"},
    "system_warning":      {"comply": True,  "scope": "system",      "action": "warn"},
    "session_warning":     {"comply": True,  "scope": "session",     "action": "warn"},
    "environment_warning": {"comply": True,  "scope": "environment", "action": "warn"},
    "environment_display": {"comply": True,  "scope": "environment", "action": "display"},
    "allow":               {"comply": True,  "scope": "none",        "action": "allow"},
}


def _propensity_response(label: str, json_stem: str = "", reason: str = "") -> dict:
    """Build a response dict from a CV-007 propensity label."""
    base = dict(PROPENSITY.get(label, PROPENSITY["allow"]))
    base["propensity"] = label
    base["json"] = json_stem
    if reason:
        base["reason"] = reason
    return base


class governance_worker_base(BaseModel if BaseModel is not object else object):
    """Base class for all pydantic governance worker objects.
    Every worker inherits Boss constraint check and risk acceptance validation.
    BUG-GOV-002: Boss prompt instructions take precedence over all other rules."""

    risk_acceptance_id: str = None
    ch_matrix_id: str = ""

    def check_boss_constraint(self, transcript_path: str = "") -> dict:
        """Check if Boss has an active constraint against state changes.
        Returns {"constrained": True/False, "constraint": "..."}"""
        return chathealthy_devops_boot._boss_has_active_constraint(transcript_path)

    def check_risk_acceptance(self) -> bool:
        """Returns True if this record has an authorized risk acceptance."""
        return self.risk_acceptance_id is not None and self.risk_acceptance_id != ""

    def pre_run_checks(self, transcript_path: str = "") -> dict:
        """Run before any governance process. Returns escalate if Boss constrained
        or if action requires risk acceptance that doesn't exist."""
        boss_check = self.check_boss_constraint(transcript_path)
        if boss_check.get("constrained"):
            return {
                "comply": False,
                "action": "escalate",
                "reason": f"BUG-GOV-002: Boss constraint active — '{boss_check['constraint']}'",
            }
        return {"comply": True}


class bug_governance_constraints(governance_worker_base):
    """Pydantic model for a governed bug record.
    Constructor validates bug data against governance constraints.
    Mirrors ChatHealthyBugsSchema (16 fields)."""

    bugid: str = ""
    title: str = ""
    description: str = ""
    environments: list = []            # CV-010
    status: str = "new"                # CV-009 (was resolution_status)
    severity: str = ""                 # CV-008
    req_id: str = ""
    story_id: str = ""
    pytest_id: str = ""
    pytest_success_criteria: list = []
    discovery_date: str = ""
    due_date: str = ""
    fix: str = ""
    reproduction: str = ""
    orphan: bool = False
    next_action: list = []

    def is_show_stopper(self) -> bool:
        return self.severity == "show_stopper"

    def is_release_blocker(self) -> bool:
        return self.severity in ("show_stopper", "critical")

    def run_governance_process(self, transcript_path: str = "") -> dict:
        """Execute the governance process for this bug instance.
        Calls base class pre_run_checks first — Boss constraint and risk acceptance."""

        # Base class checks — Boss constraint, risk acceptance
        pre = self.pre_run_checks(transcript_path)
        if not pre.get("comply", True):
            return pre

        result = {
            "bugid": self.bugid,
            "severity": self.severity,
            "status": self.status,
            "comply": True,
        }

        # Show stoppers / release blockers without risk acceptance — escalate.
        # Risk-acceptance lookup now goes through the risk_acceptance collection
        # keyed by bugid (system key: `risk_acceptance_id` was eliminated from
        # bug records).
        if not self.check_risk_acceptance() and (self.is_show_stopper() or self.is_release_blocker()):
            result["comply"] = False
            result["action"] = "escalate"
            result["reason"] = f"{self.bugid}: {self.severity or 'SHOW STOPPER'} — no risk acceptance. Escalate to Boss."
            return result

        if self.check_risk_acceptance():
            result["authorized"] = True
            return result

        # Default: allow for non-blocking bugs
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Construct from a bug dict, tolerating extra fields."""
        fields = {k: v for k, v in data.items() if k in cls.__annotations__} if hasattr(cls, '__annotations__') else data
        try:
            return cls(**fields)
        except Exception:
            # Fallback for non-pydantic
            obj = cls()
            for k, v in data.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
            return obj


class conversation_log_worker(governance_worker_base):
    """Pydantic worker for conversation log.
    T001: POST raw payload to localhost sidecar. Complete in <100ms.
    No parsing, no redaction, no transformation, no file writes.
    Emergency fallback to local file if sidecar unreachable."""

    SIDECAR_URL: str = "http://127.0.0.1:8100/produce"
    SIDECAR_TIMEOUT: int = 2  # seconds — generous, but hook must still be fast
    FALLBACK_PATH: object = BRAIN_DIR / "conversation_log_fallback.jsonl"

    def __init__(self, **kwargs):
        super().__init__(**kwargs) if BaseModel is not object else None

    def _post_to_sidecar(self, hook_input: dict, hook_event: str) -> dict:
        """T001: POST raw payload to sidecar. No transformation."""
        import urllib.request
        import urllib.error
        try:
            payload = json.dumps(hook_input).encode("utf-8")
            req = urllib.request.Request(
                self.SIDECAR_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Hook-Event": hook_event,
                },
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=self.SIDECAR_TIMEOUT)
            return {"comply": True, "logged": True, "status": resp.status}
        except (urllib.error.URLError, OSError) as e:
            # T001: Sidecar unreachable — emergency fallback
            _log.warning("Sidecar unreachable: %s — writing to fallback log", e)
            return self._fallback_write(hook_input, hook_event)
        except Exception as e:
            _log.warning("conversation_log_worker failed: %s", e)
            return {"comply": True, "logged": False, "error": str(e)}

    def _fallback_write(self, hook_input: dict, hook_event: str) -> dict:
        """T001: Emergency append-only fallback when sidecar is down."""
        try:
            from datetime import datetime, timezone
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "hook_event": hook_event,
                "payload": hook_input,
            }
            with open(self.FALLBACK_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return {"comply": True, "logged": True, "fallback": True}
        except Exception as e:
            _log.warning("Fallback write failed: %s", e)
            return {"comply": True, "logged": False, "error": str(e)}

    def run(self, hook_input: dict, action_event: str = "user_prompt_submit") -> dict:
        """T001: POST raw hook_input to sidecar. No processing in the hook."""
        if action_event in ("user_prompt_submit", "stop"):
            return self._post_to_sidecar(hook_input, action_event)
        return {"comply": True}

    @classmethod
    def from_dict(cls, data: dict):
        return cls()


class _legacy_conversation_log_worker_stop:
    """Legacy stop handler — kept for reference but no longer used.
    The consumer (PID 2) now reads the transcript via Kafka."""
    pass


# Keep the old from_dict for backward compat with the worker registry
class _conversation_log_worker_compat:
    """Shim so the WORKER_REGISTRY entry still works."""
    @classmethod
    def from_dict(cls, data: dict):
        return conversation_log_worker()


# Override the old transcript-reading stop logic — now the hook just
# POSTs the raw hook_input (which includes transcript_path) and the
# consumer handles transcript reading.

# The run() method above handles both user_prompt_submit and stop
# by POSTing the raw payload to the sidecar. The consumer (PID 2)
# is responsible for parsing the transcript_path from stop events.

# ── end conversation_log_worker ──────────────────────────────────────


class engineering_rules_worker(governance_worker_base):
    """Pydantic worker for engineering_rules.json.
    Polymorphic child — invoked on pre_tool_use when grid says code_controlled.
    Uses GPT-4.1-mini to adjudicate tool calls. No regex."""

    def _load_rules_text(self) -> str:
        """Load engineering rules + policies as text for GPT."""
        lines = []
        path = BRAIN_DIR / "engineering_rules.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for r in data.get("rules", []):
                rule_id = r.get("id", "?")
                rule_text = r.get("rule", "")[:200]
                lines.append(f"[{rule_id}] {rule_text}")
        # Policies from governance.json
        gov_path = BRAIN_DIR / "governance.json"
        if gov_path.exists():
            gov = json.loads(gov_path.read_text(encoding="utf-8"))
            policies = gov.get("policies", {})
            if isinstance(policies, dict):
                for p in policies.get("corporate_policies", []):
                    text = p.get("policy", "") or p.get("description", "")
                    if text:
                        lines.append(f"[POLICY] {text[:200]}")
        return "\n".join(lines)

    # Structured output schemas for GPT calls (BUG-GOV-006)
    _STATE_CHANGE_SCHEMA = {
        "type": "json_schema",
        "json_schema": {
            "name": "state_change_evaluation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "pass": {"type": "boolean", "description": "true if action is allowed, false if blocked"},
                    "reason": {"type": "string", "description": "why the action is allowed or blocked"},
                    "changes_external_state": {"type": "boolean"},
                    "state_description": {"type": "string"},
                    "risk_accepted": {"type": "boolean", "description": "true if covered by a risk acceptance"},
                    "risk_id": {"type": "string", "description": "the RISK-NNN id if risk_accepted is true, empty string otherwise"},
                },
                "required": ["pass", "reason", "changes_external_state", "state_description", "risk_accepted", "risk_id"],
                "additionalProperties": False,
            },
        },
    }

    _GIT_COMMIT_SCHEMA = {
        "type": "json_schema",
        "json_schema": {
            "name": "git_commit_evaluation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "has_violations": {"type": "boolean"},
                    "violations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rule_id": {"type": "string"},
                                "description": {"type": "string"},
                                "auto_fixable": {"type": "boolean"},
                            },
                            "required": ["rule_id", "description", "auto_fixable"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["has_violations", "violations"],
                "additionalProperties": False,
            },
        },
    }

    # REQ-013 universal governance: structured output for rule compliance adjudication.
    _RULE_COMPLIANCE_SCHEMA = {
        "type": "json_schema",
        "json_schema": {
            "name": "rule_compliance_evaluation",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "violates": {"type": "boolean"},
                    "rule_ids": {"type": "array", "items": {"type": "string"}},
                    "reasoning": {"type": "string"},
                    "refusal_text": {"type": "string"},
                },
                "required": ["violates", "rule_ids", "reasoning", "refusal_text"],
                "additionalProperties": False,
            },
        },
    }

    def _call_gpt(self, system_prompt: str, user_prompt: str, response_schema: dict = None) -> dict:
        """Call GPT-4.1-mini with structured output schema. Returns parsed JSON or error dict."""
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            fmt = response_schema if response_schema else {"type": "json_object"}
            resp = client.chat.completions.create(
                model="gpt-4.1-mini",
                max_tokens=300,
                response_format=fmt,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            _log.warning("engineering_rules_worker GPT call failed: %s", e)
            return {"error": str(e), "allow": True}

    _SYSTEM_PROMPT: str = (
        "You are a governance advisor for ChatHealthy, a healthcare AI startup. "
        "You evaluate tool calls against engineering rules and policies. "
        "Respond in JSON only."
    )

    def _evaluate_git_commit(self, command: str, rules_text: str) -> dict:
        """Eval A: git commit — check against rules, report violations."""
        user_prompt = (
            f"A git commit is being executed:\n"
            f"Command: {command[:500]}\n\n"
            f"Engineering rules and policies:\n{rules_text}\n\n"
            f"Respond JSON: {{\"violations\": [{{\"rule_id\": \"...\", \"description\": \"...\", "
            f"\"auto_fixable\": true/false, \"fix_description\": \"...\"}}], "
            f"\"has_violations\": true/false}}"
        )
        return self._call_gpt(self._SYSTEM_PROMPT, user_prompt)

    # DEAD CODE (v4-031) -- unreferenced function '_evaluate_state_change', marked for deletion
    # Replaced by _evaluate_state_change_with_risks which includes risk acceptance context
    # END DEAD CODE

    def _adjudicate_rule_compliance(self, action_type: str, payload: dict) -> dict:
        """REQ-013 universal governance. GPT-4.1-mini adjudicates whether an action
        (user prompt, tool call, or assistant output) violates any engineering rule.
        Returns dict with keys: violates (bool), rule_ids (list), reasoning (str),
        refusal_text (str). Fails open (violates=False) on GPT error — but this is
        a defense-in-depth layer; carve-outs and v4-000 self-compliance remain primary."""
        rules_text = self._load_rules_text()

        if action_type == "user_prompt_submit":
            prompt_text = payload.get("prompt", payload.get("content", payload.get("message", "")))
            if not prompt_text:
                return {"violates": False, "rule_ids": [], "reasoning": "empty prompt", "refusal_text": ""}
            user_prompt = (
                f"A user has submitted a prompt. You must decide whether this prompt ASKS FOR an action "
                f"that would violate any engineering rule below. A prompt that merely discusses or asks "
                f"about a rule-violating topic is NOT a violation — only prompts that explicitly ask "
                f"Claude to TAKE a rule-violating action are violations.\n\n"
                f"USER PROMPT:\n{prompt_text[:3000]}\n\n"
                f"ENGINEERING RULES:\n{rules_text}\n\n"
                f"If the prompt asks for a rule-violating action: set violates=true, list the rule_ids, "
                f"give a one-sentence reasoning, and write refusal_text that Claude will show the user "
                f"(starting with 'I cannot do that because...'). Otherwise set violates=false and leave "
                f"rule_ids empty and refusal_text empty."
            )
        elif action_type == "pre_tool_use":
            tool_name = payload.get("tool_name", "")
            tool_input = payload.get("tool_input", {})
            detail = str(tool_input.get("command", tool_input.get("file_path", tool_input)))[:1000]
            user_prompt = (
                f"A tool call is about to execute. You must decide whether this tool call would "
                f"violate any engineering rule below.\n\n"
                f"TOOL CALL:\nTool: {tool_name}\nInput: {detail}\n\n"
                f"ENGINEERING RULES:\n{rules_text}\n\n"
                f"If the tool call violates a rule: set violates=true, list the rule_ids, give a "
                f"one-sentence reasoning, and write refusal_text explaining the block. Otherwise set "
                f"violates=false and leave rule_ids empty and refusal_text empty."
            )
        elif action_type == "stop":
            output_text = payload.get("assistant_output", "")
            if not output_text:
                return {"violates": False, "rule_ids": [], "reasoning": "no output captured", "refusal_text": ""}
            user_prompt = (
                f"Claude has produced a final assistant message. You must decide whether Claude's "
                f"output represents Claude TAKING, COMMITTING TO, or ANNOUNCING the completion of an "
                f"action that violates a rule.\n\n"
                f"NOT violations: asking the user a question (even a question about a rule-governed "
                f"topic); presenting a menu of options for the user to choose from; reporting status; "
                f"explaining or discussing a rule-governed topic; proposing an action for user "
                f"approval; refusing to act. Questions, explanations, reports, and proposals are "
                f"never violations regardless of subject.\n\n"
                f"Violations: Claude declaring it has done a rule-forbidden action, Claude stating "
                f"it will now do a rule-forbidden action without further authorization, Claude "
                f"having produced output whose content itself violates a rule (e.g. leaking stored "
                f"user data when a rule forbids it).\n\n"
                f"ASSISTANT OUTPUT:\n{output_text[:5000]}\n\n"
                f"ENGINEERING RULES:\n{rules_text}\n\n"
                f"If Claude TOOK or COMMITTED TO a rule-violating action, or the output content "
                f"itself violates a rule: set violates=true, list the rule_ids, give a one-sentence "
                f"reasoning, and write refusal_text describing the violation. Otherwise set "
                f"violates=false."
            )
        else:
            return {"violates": False, "rule_ids": [], "reasoning": f"unknown action_type {action_type}", "refusal_text": ""}

        result = self._call_gpt(self._SYSTEM_PROMPT, user_prompt, self._RULE_COMPLIANCE_SCHEMA)
        if result.get("error"):
            return {"violates": False, "rule_ids": [], "reasoning": f"adjudicator error: {result.get('error')}", "refusal_text": ""}
        return {
            "violates": bool(result.get("violates", False)),
            "rule_ids": result.get("rule_ids", []),
            "reasoning": result.get("reasoning", ""),
            "refusal_text": result.get("refusal_text", ""),
        }

    def _evaluate_state_change_with_risks(self, tool_name, tool_input, risk_text):
        """Eval B with risk acceptance context. GPT checks risks FIRST."""
        detail = tool_input.get("command", tool_input.get("file_path", ""))[:500]
        user_prompt = (
            f"A tool call is being made:\n"
            f"Tool: {tool_name}\n"
            f"Input: {detail}\n\n"
            f"STEP 1 — CHECK RISK ACCEPTANCES FIRST:\n"
            f"Boss has pre-authorized these actions. If the tool call is covered by ANY of them, "
            f"set risk_accepted=true and risk_id to the matching RISK ID, and you are DONE — do not evaluate further.\n"
        )
        if risk_text:
            user_prompt += f"{risk_text}\n\n"
        else:
            user_prompt += "No active risk acceptances.\n\n"
        user_prompt += (
            f"STEP 2 — ONLY if not covered by a risk acceptance:\n"
            f"Does this tool call change state OUTSIDE of files tracked in git?\n"
            f"External state: database writes, API POSTs, system config changes, cloud deploys.\n"
            f"NOT external state: editing source/config/JSON files, reading from anywhere, "
            f"killing/restarting non-prod frontend processes (ports 80, 443, 5173, 8000, 8001).\n"
        )
        return self._call_gpt(self._SYSTEM_PROMPT, user_prompt, self._STATE_CHANGE_SCHEMA)

    # Governance infrastructure + Boss-authorized patterns — always pass
    _GOVERNANCE_PATTERNS = [
        r"chathealthy_devops_boot\.py",
        r"conversation_log_hook\.py",
        r"bash_rule_guard\.py",
        r"kill_zombies\.py",
        r"bump_build\.py",
        r"pre_deploy_rule_check",
        r"devpipelinemanagmentservice.*azurewebsites\.net",  # Pipeline service invocation
        r"start_local\.bat",  # Local dev environment launcher
        r"^curl\s",  # Network reads — health checks, status polls
        r"^(tasklist|netstat)",  # Process inspection
        r"^gh\s+(run|pr|issue)\s+(list|view|watch|status)",  # GitHub CLI read-only commands
        r"uvicorn",  # Local dev servers
        r"npm\s+(run|dev|start)",  # Frontend dev server
        r"caddy",  # HTTPS reverse proxy
        r"huggingface|hf\.space|hf_space|create_hf_space|delete_hf_space",  # HF Spaces ($0.03/hr)
        r"playwright",  # Playwright browser testing
        r"pytest",  # Test runner
        r"regression_runner\.py",  # Ordered regression test runner
        r"scan_http\.py",  # HTTPS security scanner (read-only)
        r"^python3?\s+(-c\s|<<)",  # Inline python scripts
    ]

    # v4-028: Bug/feature detection keywords
    _BUG_FEATURE_KEYWORDS = [
        "bug", "broken", "fix", "doesn't work", "not working", "wrong", "fail",
        "crash", "error", "missing", "lost", "feature", "enhancement", "add",
        "new", "should", "must", "requirement", "need", "implement", "build",
    ]

    def _detect_bug_or_feature(self, prompt: str) -> bool:
        """Detect if user prompt is reporting a bug or requesting a feature."""
        prompt_lower = prompt.lower()
        matches = sum(1 for kw in self._BUG_FEATURE_KEYWORDS if kw in prompt_lower)
        return matches >= 2  # At least 2 keywords to avoid false positives

    def run(self, hook_input: dict, action_event: str = "pre_tool_use") -> dict:
        """Polymorphic run — called by singleton when grid says code_controlled.
        Implements REQ-013 universal governance: every action_event adjudicates
        against the engineering rules before allowing."""

        # REQ-013 + v4-028: On UserPromptSubmit, adjudicate the prompt against rules.
        if action_event == "user_prompt_submit":
            prompt = hook_input.get("prompt", hook_input.get("content", hook_input.get("message", "")))
            contexts = []

            # v4-028 keyword detector (legacy, belt-and-suspenders with REQ-013 adjudicator).
            if self._detect_bug_or_feature(prompt):
                contexts.append(
                    "v4-028 DIRECTIVE: The user appears to be reporting a bug or requesting a feature. "
                    "Silently perform two pre-flight steps: "
                    "(1) Search bugs.json for duplicates or related bugs, "
                    "(2) Search agile_backlog.json for duplicate or contradicting features/requirements. "
                    "If a duplicate or contradiction is found, surface it to resolve before coding. "
                    "AUTHORIZATION IS ALREADY GRANTED when Boss has stated the work ('fix it', 'do it', "
                    "'implement it', etc.). Do NOT ask 'want me to...', 'shall I...', or any other "
                    "permission-seeking question for work Boss has already directed. Execute the directive; "
                    "report back only on completion or on a genuine semantic ambiguity."
                )

            # REQ-013: GPT-adjudicate whether the prompt asks for a rule-violating action.
            verdict = self._adjudicate_rule_compliance("user_prompt_submit", {"prompt": prompt})
            if verdict.get("violates"):
                rule_ids = ", ".join(verdict.get("rule_ids", [])) or "(unspecified)"
                contexts.append(
                    f"REQ-013 REFUSAL DIRECTIVE: This user prompt asks for an action that would violate "
                    f"engineering rule(s): {rule_ids}. Reasoning: {verdict.get('reasoning', '')}. "
                    f"Claude MUST refuse this request. Begin your reply with this refusal (verbatim): "
                    f"\"{verdict.get('refusal_text', 'I cannot do that because it would violate an engineering rule.')}\" "
                    f"Do not proceed with the requested action under any framing."
                )

            if contexts:
                return {"comply": True, "additionalContext": "\n\n".join(contexts)}
            return {"comply": True}

        # REQ-013: On Stop (final assistant output), adjudicate Claude's output.
        if action_event == "stop":
            # Read the last assistant message from the transcript (Claude Code passes transcript_path).
            transcript_path = hook_input.get("transcript_path", "")
            assistant_output = self._read_last_assistant_message(transcript_path)
            if not assistant_output:
                return {"comply": True}
            verdict = self._adjudicate_rule_compliance("stop", {"assistant_output": assistant_output})
            if verdict.get("violates"):
                rule_ids = ", ".join(verdict.get("rule_ids", [])) or "(unspecified)"
                return {
                    "comply": False,
                    "decision": "block",
                    "reason": (
                        f"REQ-013: Claude's final output violated engineering rule(s): {rule_ids}. "
                        f"{verdict.get('reasoning', '')}. Claude must redo the turn without the violation."
                    ),
                    "rule_ids": verdict.get("rule_ids", []),
                }
            return {"comply": True}

        if action_event != "pre_tool_use":
            return {"comply": True}

        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})

        # Read-only tools and internal Claude tools — always pass, no GPT call
        if tool_name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch", "Agent", "TodoWrite", "Skill", "ToolSearch"):
            return {"comply": True, "allow": True}

        # Edit/Write — if git tracks it, allow. Governance happens at commit time.
        # If git doesn't track it, it's external state → GPT evaluates.
        if tool_name in ("Edit", "Write"):
            file_path = tool_input.get("file_path", "")
            # BRAIN-RUNTIME-REQ-002 / REQ-003: validate edits to bugs.json and
            # agile_backlog.json BEFORE the allow — dedup + parent-hierarchy check.
            # Runs before the git-tracked allow so brain writes don't get a free pass.
            try:
                normalized = file_path.replace("\\", "/")
                if "brain/machine_artifacts/content/bugs.json" in normalized:
                    verdict = self._validate_brain_edit("bugs", tool_name, tool_input)
                    if not verdict.get("ok", True):
                        return {
                            "comply": False,
                            "decision": "block",
                            "reason": verdict.get("reason", "BRAIN-RUNTIME-REQ-002 violation"),
                        }
                elif "brain/machine_artifacts/content/agile_backlog.json" in normalized:
                    verdict = self._validate_brain_edit("backlog", tool_name, tool_input)
                    if not verdict.get("ok", True):
                        return {
                            "comply": False,
                            "decision": "block",
                            "reason": verdict.get("reason", "BRAIN-RUNTIME-REQ-003 violation"),
                        }
            except Exception as e:
                _log.warning("BRAIN-RUNTIME validator failed (non-fatal): %s", e)
            try:
                import subprocess
                result = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", file_path],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(BRAIN_DIR.parents[2])
                )
                if result.returncode == 0:
                    return {"comply": True, "allow": True}  # Git-tracked → allow
                # New file in a git repo dir → also allow (will be tracked on add)
                result2 = subprocess.run(
                    ["git", "rev-parse", "--is-inside-work-tree"],
                    capture_output=True, text=True, timeout=5,
                    cwd=os.path.dirname(file_path) if os.path.dirname(file_path) else "."
                )
                if result2.returncode == 0 and result2.stdout.strip() == "true":
                    return {"comply": True, "allow": True}  # Inside git repo → allow
            except Exception:
                pass
            # Outside git → GPT decides

        command = tool_input.get("command", "")

        # Governance infrastructure — always pass
        for pattern in self._GOVERNANCE_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return {"comply": True, "allow": True}

        # Git operations — always pass (governance happens at commit time, workflows gate deploys)
        if tool_name == "Bash" and re.search(r"(^|\s*&&\s*)git\s+", command):
            return {"comply": True, "allow": True}

        # Eval A: git commit
        if tool_name == "Bash" and re.search(r"^git\s+commit", command):
            rules_text = self._load_rules_text()
            result = self._evaluate_git_commit(command, rules_text)
            if result.get("error"):
                return {"comply": True, "allow": True}  # fail open
            if result.get("has_violations"):
                violations = result.get("violations", [])
                auto_fixable = [v for v in violations if v.get("auto_fixable")]
                needs_human = [v for v in violations if not v.get("auto_fixable")]
                return {
                    "comply": len(needs_human) == 0,
                    "allow": len(needs_human) == 0,
                    "auto_fixable": auto_fixable,
                    "needs_human": needs_human,
                    "reason": "; ".join(f"{v['rule_id']}: {v['description']}" for v in needs_human) if needs_human else "",
                }
            return {"comply": True, "allow": True}

        # Eval B: everything else — does it change external state?
        # FIRST: Check risk acceptances deterministically — Boss approvals are final.
        # GPT does not get to override Boss.
        try:
            ra_path = os.path.join(str(BRAIN_DIR), "risk_acceptance.json")
            with open(ra_path, encoding="utf-8") as f:
                ra = json.load(f)
            active = [e for e in ra.get("entries", []) if e.get("boss_decision") == "ACCEPTED"]
            detail = tool_input.get("command", tool_input.get("file_path", "")).lower()
            for e in active:
                scope = (e.get("scope", "") or e.get("description", "")).lower()
                # If any word in the scope matches the command, Boss approved it
                scope_words = [w for w in scope.split() if len(w) > 3]
                if scope_words and all(w in detail for w in scope_words):
                    _log.info("Action allowed by risk acceptance %s: %s", e["id"], e.get("title", ""))
                    return {"comply": True, "allow": True}
        except Exception:
            pass

        # REQ-013 universal governance: final adjudication before allow.
        # Every tool call that reaches this point is subjected to a rule-compliance check.
        verdict = self._adjudicate_rule_compliance(
            "pre_tool_use",
            {"tool_name": tool_name, "tool_input": tool_input},
        )
        if verdict.get("violates"):
            rule_ids = ", ".join(verdict.get("rule_ids", [])) or "(unspecified)"
            return {
                "comply": False,
                "allow": False,
                "reason": (
                    f"REQ-013: tool call blocked — violates rule(s) {rule_ids}. "
                    f"{verdict.get('reasoning', '')}"
                ),
                "rule_ids": verdict.get("rule_ids", []),
            }

        return {"comply": True, "allow": True}

    def _validate_brain_edit(self, which: str, tool_name: str, tool_input: dict) -> dict:
        """BRAIN-RUNTIME-REQ-002/003: validate a proposed Edit/Write to
        bugs.json or agile_backlog.json. Simulates the edit in memory, parses
        the resulting JSON, identifies newly-added records, and runs them
        through brain_write_validator. Returns {ok, reason}.
        On parse failure or unexpected shape, returns ok=True (don't block on
        inability to reason about the edit)."""
        try:
            from brain_write_validator import (
                validate_bug_insert, validate_backlog_insert,
            )
        except Exception as e:
            _log.warning("brain_write_validator import failed: %s", e)
            return {"ok": True}

        file_path = tool_input.get("file_path", "")
        try:
            with open(file_path, encoding="utf-8") as f:
                current_text = f.read()
        except Exception:
            return {"ok": True}  # can't read — don't block

        # Simulate the edit.
        if tool_name == "Write":
            new_text = tool_input.get("content", "")
        else:  # Edit
            old_s = tool_input.get("old_string", "")
            new_s = tool_input.get("new_string", "")
            if old_s not in current_text:
                return {"ok": True}  # edit won't apply — Claude Code will reject it anyway
            new_text = current_text.replace(old_s, new_s, 1)

        try:
            current = json.loads(current_text)
            proposed = json.loads(new_text)
        except Exception:
            return {"ok": True}  # can't parse — don't block on intermediate state

        # Determine current operating mode from settings.
        settings = self._read_settings() if hasattr(self, "_read_settings") else {}
        if not settings:
            try:
                settings = chathealthy_devops_boot._read_settings()
            except Exception:
                settings = {}
        mode_code = (settings.get("mode") or "2").strip()
        mode = {"1": "unattended", "2": "normal", "3": "idiot"}.get(mode_code, "normal")

        if which == "bugs":
            cur_bugs = current.get("bug") or []
            new_bugs = proposed.get("bug") or []
            cur_ids = {b.get("bugid") for b in cur_bugs if isinstance(b, dict) and b.get("bugid")}
            added = [b for b in new_bugs
                     if isinstance(b, dict) and b.get("bugid") and b.get("bugid") not in cur_ids]
            for bug in added:
                verdict = validate_bug_insert(bug, mode=mode, bugs_list=cur_bugs)
                if not verdict.get("ok"):
                    return {
                        "ok": False,
                        "reason": (
                            f"BRAIN-RUNTIME-REQ-002 block — new bug {bug.get('bugid', '?')}: "
                            f"{verdict.get('reason', 'validation failed')} "
                            f"(action: {verdict.get('action', 'UNKNOWN')})"
                        ),
                    }
            return {"ok": True}

        if which == "backlog":
            def _walk_reqs(backlog):
                acc = []
                for epic in (backlog.get("epics", {}).get("epic") or []):
                    if not isinstance(epic, dict):
                        continue
                    for feat in (epic.get("features", {}).get("feature") or []):
                        if not isinstance(feat, dict):
                            continue
                        for story in (feat.get("stories", {}).get("story") or []):
                            if not isinstance(story, dict):
                                continue
                            for req in (story.get("requirements", {}).get("requirement") or []):
                                if not isinstance(req, dict):
                                    continue
                                acc.append((req, story))
                return acc

            cur_pairs = _walk_reqs(current)
            new_pairs = _walk_reqs(proposed)
            cur_req_ids = {r.get("req_id") for r, _ in cur_pairs if r.get("req_id")}
            added = [(r, s) for r, s in new_pairs
                     if r.get("req_id") and r.get("req_id") not in cur_req_ids]
            for req, story in added:
                parent_id = story.get("story_id", "")
                verdict = validate_backlog_insert(
                    req, entry_type="requirement", parent_id=parent_id,
                    mode=mode, backlog=current,
                )
                if not verdict.get("ok"):
                    return {
                        "ok": False,
                        "reason": (
                            f"BRAIN-RUNTIME-REQ-003 block — new requirement {req.get('req_id', '?')}: "
                            f"{verdict.get('reason', 'validation failed')} "
                            f"(action: {verdict.get('action', 'UNKNOWN')})"
                        ),
                    }
            return {"ok": True}

        return {"ok": True}

    @staticmethod
    def _read_last_assistant_message(transcript_path: str) -> str:
        """Read the last assistant message text from a Claude Code transcript file.
        Returns empty string if the file doesn't exist, can't be parsed, or has no
        assistant message. Used by the Stop-event rule adjudicator."""
        if not transcript_path or not os.path.exists(transcript_path):
            return ""
        try:
            last_text = ""
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    # Claude Code transcript entries vary — look for assistant text.
                    if entry.get("type") == "assistant" or entry.get("role") == "assistant":
                        msg = entry.get("message", entry)
                        content = msg.get("content", "") if isinstance(msg, dict) else ""
                        if isinstance(content, list):
                            text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                            content = "\n".join(t for t in text_parts if t)
                        if isinstance(content, str) and content.strip():
                            last_text = content
            return last_text
        except Exception:
            return ""

    @classmethod
    def from_dict(cls, data: dict):
        return cls()


# ── Registry: JSON stem → child class ────────────────────────────────────────
# Only JSONs with code_controlled cells in the grid get a child class.
# The singleton uses this to dispatch.
WORKER_REGISTRY = {
    "bugs": bug_governance_constraints,
    "conversation_log": conversation_log_worker,
    "engineering_rules": engineering_rules_worker,
}


class chathealthy_devops_boot:
    """Governance brain — singleton. Loads rules, gates actions, audits output."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    # Every brain JSON must be registered. Abend if a JSON has no function.
    _JSON_FUNCTION_MAP = {
        "agile_backlog": "check_agile_backlog",
        "ai_operations": "check_ai_operations",
        "architecture": "check_architecture",
        "business_plan": "check_business_plan",
        "governance_matrix": None,  # The matrix itself — not a constraint source, it IS the governance
        "controlled_vocabularies": "check_controlled_vocabularies",
        "conversation_log": "check_conversation_log",
        "daily_punch_list_with_results_and_accomplishments": "check_daily_punch_list",
        "design": "check_design",
        "engineering_rules": "check_engineering_rules",
        "emergency_keywords": "check_emergency_keywords",
        "errors": None,  # Runtime error log from Windows service — not a constraint source
        "external_audits": "check_external_audits",
        "governance": "check_governance",
        "legal": "check_legal",
        "pipeline_v3_iteration_log": "check_pipeline_v3_iteration_log",
        "pipeline_v4_design_iterations": "check_pipeline_v4_design_iterations",
        # policies merged into governance.json as top-level attribute
        "project_manifest": "check_project_manifest",
        "prompts": "check_prompts",
        "bugs": "check_bugs",
        "regression_report": None,  # Test results snapshot — not a constraint source
        "risk_acceptance": "check_risk_acceptance",
        "schema": "check_schema",
        "security": "check_security",
        "sprint_plan": "check_sprint_plan",
        "token_usage": "check_token_usage",
        "traceability_matrix": "check_traceability_matrix",
        "unrealized_ideas": "check_unrealized_ideas",
        "work_log": "check_work_log",
        "version": None,  # Version metadata — not a constraint source
    }

    def __init__(self, load_full=False):
        if self._initialized:
            return
        self.brain = {}
        self._constraints = []
        self._state = {}  # Singleton state — persists across hook calls
        self._booted = False  # Boot runs once, on first UserPromptSubmit

        if load_full:
            self._load_brain()
            self._verify_coverage()
            self._extract_constraints()
        else:
            self._load_digest()
        self._initialized = True

    @staticmethod
    def _read_settings() -> dict:
        """Read .claude/ChatHealthySettings.json. Returns empty dict on failure."""
        try:
            if SETTINGS_PATH.exists():
                return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    @staticmethod
    def _write_settings(settings: dict):
        """Write .claude/ChatHealthySettings.json."""
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")

    def booted(self) -> Optional[bool]:
        """Check if boot has already run this session.
        Returns True if booted, False if not booted, None if settings file
        or flag is missing (error state — do not boot)."""
        if self._booted:
            return True
        settings = self._read_settings()
        if not settings or "booted" not in settings:
            return None  # Settings file or flag missing — error state
        return settings["booted"]

    @staticmethod
    def _mode_selected() -> bool:
        """BUG-GOV-009: True once the user has replied 1/2/3 after a boot.
        False after boot and before the user's mode reply — Claude's turn MUST
        present the verbatim mode prompt during this window."""
        settings = chathealthy_devops_boot._read_settings()
        return bool(settings.get("mode_selected", True))  # Default True so the
        # check is a no-op when the flag is absent (e.g. pre-upgrade state).

    # BUG-GOV-009: verbatim substrings that MUST appear in Claude's assistant
    # output on the first turn after boot. Match is case-insensitive, whitespace
    # tolerant, but all four substrings must be present.
    _MODE_PROMPT_REQUIRED = (
        "select operating mode",
        "1 = unattended",
        "2 = normal",
        "3 = idiot",
    )

    @classmethod
    def _assistant_output_has_mode_prompt(cls, assistant_output: str) -> bool:
        if not assistant_output:
            return False
        haystack = assistant_output.lower()
        return all(needle in haystack for needle in cls._MODE_PROMPT_REQUIRED)

    # ── Brain loading ──────────────────────────────────────────────────────

    def _load_brain(self):
        for json_file in sorted(BRAIN_DIR.glob("*.json")):
            try:
                with open(json_file, encoding="utf-8") as f:
                    self.brain[json_file.stem] = json.load(f)
            except Exception as e:
                _log.warning("Failed to load %s: %s", json_file.name, e)
        # BRAIN-RUNTIME-REQ-005 (T-requirement): refresh req_index.txt transient
        # so CLAUDE.md @-import has current backlog. Non-fatal on failure.
        try:
            self._write_req_index()
        except Exception as e:
            _log.warning("req_index refresh failed (non-fatal): %s", e)

    def _write_req_index(self) -> Optional[int]:
        """BRAIN-RUNTIME-REQ-005 (T-requirement): materialize a transient
        byte-stream view of agile_backlog.json at test_output/req_index.txt.

        Technical justification: REQ-001 (B-requirement) mandates brain content
        live only under brain/machine_artifacts/content/. Harness-level
        @-import of the full 3MB canonical agile_backlog.json is the direct
        way to satisfy REQ-001, but Claude Code's expanded-CLAUDE.md harness
        threshold (empirical ~3MB) caused a REQ-010 boot regression when
        attempted (BUG-GOV-010 original incident). Until Claude Code lifts
        that gap, the harness-compatible mechanism is: copy canonical brain
        content to a gitignored project-local cache, @-import from the cache,
        regenerate the cache each session_end so next session's @-import
        reads fresh bytes, delete-and-replace at session_end to satisfy the
        'delete after ingestion' constraint.

        Format: flat pipe-delimited bytes (NOT structured JSON) — one line per
        requirement. Path: test_output/req_index.txt (gitignored project folder).
        Canonical source stays untouched at brain/machine_artifacts/content/agile_backlog.json.

        Returns bytes written, or None if skipped/failed. Idempotent — skips if
        existing target file is newer than the canonical source."""
        backlog_path = BRAIN_DIR / "agile_backlog.json"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        index_path = CACHE_DIR / "req_index.txt"
        if not backlog_path.exists():
            return None
        if index_path.exists():
            if index_path.stat().st_mtime >= backlog_path.stat().st_mtime:
                return None

        data = self.brain.get("agile_backlog")
        if data is None:
            try:
                with open(backlog_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                _log.warning("req_index: could not load agile_backlog.json: %s", e)
                return None

        lines = [
            "# req_index — transient byte-stream view of agile_backlog.json",
            "# Generated per BRAIN-RUNTIME-REQ-005 (T-requirement).",
            "# Regenerated at session_end; gitignored; canonical source:",
            "# brain/machine_artifacts/content/agile_backlog.json",
            "# Format: epic_id | feature_id | story_id | req_id | priority | status | approval | orphan | requirement_text",
            "",
        ]
        req_count = 0
        for epic in (data.get("epics", {}).get("epic", []) or []):
            if not isinstance(epic, dict):
                continue
            epic_id = epic.get("epic_id", "")
            for feat in (epic.get("features", {}).get("feature", []) or []):
                if not isinstance(feat, dict):
                    continue
                feat_id = feat.get("feature_id", "")
                for story in (feat.get("stories", {}).get("story", []) or []):
                    if not isinstance(story, dict):
                        continue
                    story_id = story.get("story_id", "")
                    for req in (story.get("requirements", {}).get("requirement", []) or []):
                        if not isinstance(req, dict):
                            continue
                        req_count += 1
                        text = (req.get("requirement", "") or "").strip().replace("\n", " ").replace("|", "/")
                        lines.append(
                            f"{epic_id}|{feat_id}|{story_id}|"
                            f"{req.get('req_id', '')}|"
                            f"{req.get('priority', '')}|"
                            f"{req.get('status', '')}|"
                            f"{req.get('approval', '')}|"
                            f"{'true' if req.get('orphan') else 'false'}|"
                            f"{text}"
                        )

        if req_count == 0:
            # Defensive: walking produced zero requirements (empty/malformed
            # brain data). Don't overwrite a healthy on-disk copy with an
            # empty header-only file.
            _log.warning("req_index: walk produced 0 reqs; keeping existing file")
            return None

        content = "\n".join(lines) + "\n"
        tmp_path = index_path.with_suffix(".txt.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(index_path)
        size = index_path.stat().st_size
        _log.info("req_index: wrote %d bytes (%d reqs) to %s",
                  size, req_count, index_path)
        return size

    _RUNTIME_BRAIN_FILES = ("bugs.json", "engineering_rules.json", "agile_backlog.json")

    def _check_brain_freshness(self) -> str | None:
        """BRAIN-RUNTIME-REQ-004: detect mid-session edits to the three runtime
        brain JSONs. Persistent mtime snapshot lives in ChatHealthySettings.json
        under 'brain_mtimes'. Returns a reminder string if any governed brain file
        has been edited since the snapshot, else None. Always updates the snapshot."""
        snapshot: dict[str, float] = {}
        stale: list[str] = []
        for fname in self._RUNTIME_BRAIN_FILES:
            path = BRAIN_DIR / fname
            cur_mtime = path.stat().st_mtime if path.exists() else 0.0
            snapshot[fname] = cur_mtime

        settings = self._read_settings()
        last_known: dict = settings.get("brain_mtimes") or {}

        for fname, cur in snapshot.items():
            prev = last_known.get(fname, 0.0)
            if cur > prev > 0.0:
                # Edited since last snapshot (prev=0 means first run — don't flag).
                stale.append(fname)

        # Update snapshot so next prompt sees the latest.
        if snapshot != last_known:
            settings["brain_mtimes"] = snapshot
            self._write_settings(settings)

        if not stale:
            return None

        listing = "\n".join(f"  - brain/machine_artifacts/content/{f}" for f in stale)
        return (
            "BRAIN-RUNTIME-REQ-004: Stale context detected. The following brain "
            "files have been edited since your last prompt:\n"
            f"{listing}\n"
            "Your @-imported in-context copy is stale against the on-disk canonical. "
            "Before taking any further action that reads or reasons about these files, "
            "either /compact (harness re-reads CLAUDE.md and re-injects fresh @-imports) "
            "or Read the specific file(s) from disk so your reasoning uses current content."
        )

    def _verify_coverage(self):
        json_stems = set(self.brain.keys())
        registered = set(self._JSON_FUNCTION_MAP.keys())
        unregistered = json_stems - registered
        if unregistered:
            print(f"ABEND: Unregistered brain JSONs: {sorted(unregistered)}", file=sys.stderr)
            sys.exit(2)
        for stem, method_name in self._JSON_FUNCTION_MAP.items():
            if method_name is not None and not hasattr(self, method_name):
                print(f"ABEND: Method '{method_name}' for '{stem}.json' missing.", file=sys.stderr)
                sys.exit(2)

    # ── Digest cache ───────────────────────────────────────────────────────

    def _save_digest(self):
        digest = {
            "constraints": self._constraints,
            "brain_files": list(self.brain.keys()),
            "governance_matrix": self.brain.get("governance_matrix", {}),
        }
        with open(DIGEST_PATH, "w", encoding="utf-8") as f:
            json.dump(digest, f, indent=2)

    def _load_digest(self):
        if DIGEST_PATH.exists():
            try:
                with open(DIGEST_PATH, encoding="utf-8") as f:
                    cached = json.load(f)
                    self._constraints = cached.get("constraints", [])
                    # Restore matrix so dispatch_code_controlled works
                    if cached.get("governance_matrix"):
                        self.brain["governance_matrix"] = cached["governance_matrix"]
                return
            except Exception:
                pass
        self._load_brain()
        self._verify_coverage()
        self._extract_constraints()
        self._save_digest()

    def _extract_constraints(self):
        self._constraints = []
        for stem, method_name in self._JSON_FUNCTION_MAP.items():
            if method_name and stem in self.brain:
                result = getattr(self, method_name)(source="boot", destination="brain", action_event="session_start")
                if result.get("constraints"):
                    self._constraints.extend(result["constraints"])
    # ══════════════════════════════════════════════════════════════════════
    # Matrix-driven compliance
    # ══════════════════════════════════════════════════════════════════════

    def _get_matrix(self) -> dict:
        """Load the governance matrix from brain."""
        return self.brain.get("governance_matrix", {}).get("matrix", {})

    def _get_propensity(self, json_stem: str, action_event: str) -> str:
        """Read a single cell from the governance matrix. Returns CV-007 label.
        GOV-015: all environments equal — cell is a flat string."""
        matrix = self._get_matrix()
        row = matrix.get(json_stem, {})
        cell = row.get(action_event, "allow")
        # Handle legacy tiered cells
        if isinstance(cell, dict):
            return cell.get(ENV, "allow")
        return cell if isinstance(cell, str) else "allow"

    def _check_json(self, json_stem: str, action_event: str) -> dict:
        """Universal check function. Reads propensity from matrix, extracts constraints."""
        label = self._get_propensity(json_stem, action_event)
        constraints = self._extract_json_constraints(json_stem)
        result = _propensity_response(label, json_stem)
        result["constraints"] = constraints

        # Critical JSONs must not be empty if they're warning or abending
        if label in ("system_abend", "session_abend", "system_warning", "session_warning"):
            if not constraints and json_stem in ("engineering_rules", "policies", "security"):
                result["reason"] = f"{json_stem}.json is empty — cannot govern"
                if "abend" in label:
                    result["comply"] = False

        return result

    def _extract_json_constraints(self, json_stem: str) -> list[str]:
        """Extract constraint text from a brain JSON. Each JSON has its own structure."""
        data = self.brain.get(json_stem, {})
        if not data:
            return []

        # Rules-based JSONs
        if json_stem == "engineering_rules":
            return [r["rule"] for r in data.get("rules", []) if r.get("rule")]

        # Policies
        if json_stem == "policies":
            return [p.get("policy", "") or p.get("description", "") for p in data.get("policies", data.get("corporate_policies", [])) if p.get("policy") or p.get("description")]

        # Governance sign-offs + policies (policies is a child of governance)
        if json_stem == "governance":
            constraints = [e.get("description", "") for e in data.get("records", []) if e.get("description")]
            policies = data.get("policies", {})
            if isinstance(policies, dict):
                for p in policies.get("corporate_policies", []):
                    text = p.get("policy", "") or p.get("description", "")
                    if text:
                        constraints.append(text)
            return constraints

        # Architecture
        if json_stem == "architecture":
            c = []
            for key in ["pipeline_architecture", "deployment_architecture", "security_architecture"]:
                section = data.get(key, {})
                if isinstance(section, dict):
                    for r in section.get("rules", section.get("constraints", [])):
                        c.append(str(r))
            return c

        # Security
        if json_stem == "security":
            c = []
            for section in data.get("architecture", data.get("sections", [])):
                if isinstance(section, dict):
                    for item in section.get("controls", section.get("requirements", [])):
                        if isinstance(item, dict):
                            c.append(item.get("how", item.get("description", "")))
                        elif isinstance(item, str):
                            c.append(item)
            return c

        # Legal
        if json_stem == "legal":
            c = []
            for item in data.get("agreements", data.get("questions", [])):
                if isinstance(item, dict):
                    text = item.get("verdict", "") or item.get("status", "")
                    if text:
                        c.append(text)
            return c

        # Risk acceptance
        if json_stem == "risk_acceptance":
            return [f"ACCEPTED: {r.get('description', '')}" for r in data.get("risks", []) if r.get("description")]

        # Emergency keywords
        if json_stem == "emergency_keywords":
            categories = data.get("categories", {})
            count = sum(len(v) for v in categories.values()) if categories else 0
            return [f"Emergency keywords: {count} loaded"] if count else []

        # Token usage
        if json_stem == "token_usage":
            budget = data.get("budget", {})
            if budget.get("daily_limit"):
                return [f"Budget: ${budget['daily_limit']}/day"]
            return []

        # Agile backlog — pipeline schema per v4-032: epics.epic
        if json_stem == "agile_backlog":
            epics = data.get("epics", {}).get("epic", [])
            return [f"EPIC: {e.get('name', '')} ({e.get('status', '')})" for e in epics if isinstance(e, dict) and e.get("name")]

        # Default — no extractable constraints
        return []

    # Per-JSON check functions — all delegate to _check_json with the method column
    # These exist so _JSON_FUNCTION_MAP can reference them by name

    def check_engineering_rules(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("engineering_rules", action_event)

    def check_policies(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("policies", action_event)

    def check_governance(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("governance", action_event)

    def check_architecture(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("architecture", action_event)

    def check_security(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("security", action_event)

    def check_risk_acceptance(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("risk_acceptance", action_event)

    def check_legal(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("legal", action_event)

    def check_controlled_vocabularies(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("controlled_vocabularies", action_event)

    def check_agile_backlog(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("agile_backlog", action_event)

    def check_sprint_plan(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("sprint_plan", action_event)

    def check_ai_operations(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("ai_operations", action_event)

    def check_project_manifest(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("project_manifest", action_event)

    def check_schema(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("schema", action_event)

    def check_prompts(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("prompts", action_event)

    def check_emergency_keywords(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("emergency_keywords", action_event)

    def check_external_audits(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("external_audits", action_event)

    def check_bugs(self, source="", destination="", action_event="session_start", transcript_path="") -> dict:
        """Construct each bug, call run_governance_process. The class owns the rules."""
        bugs_data = self.brain.get("bugs", {})
        for bug_dict in bugs_data.get("bug", []):
            bug = bug_governance_constraints.from_dict(bug_dict)
            result = bug.run_governance_process(transcript_path=transcript_path)
            if not result.get("comply", True):
                return result
        return _propensity_response("allow", "bugs")

    def check_traceability_matrix(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("traceability_matrix", action_event)

    def check_business_plan(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("business_plan", action_event)

    def check_design(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("design", action_event)

    def check_token_usage(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("token_usage", action_event)

    def check_work_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("work_log", action_event)

    def check_conversation_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("conversation_log", action_event)

    def check_daily_punch_list(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("daily_punch_list_with_results_and_accomplishments", action_event)

    def check_unrealized_ideas(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("unrealized_ideas", action_event)

    def check_pipeline_v3_iteration_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("pipeline_v3_iteration_log", action_event)

    def check_pipeline_v4_design_iterations(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("pipeline_v4_design_iterations", action_event)

    # ══════════════════════════════════════════════════════════════════════
    # Four lifecycle methods
    # ══════════════════════════════════════════════════════════════════════

    def _announce(self, method, source, destination):
        """Print hook execution visibly."""
        print(f"🔒 GUARD | {method}() | source={source} | destination={destination}", file=sys.stderr)

    # ── BUG-GOV-002: Boss prompt constraint check ──────────────────────────

    @staticmethod
    def _boss_has_active_constraint(transcript_path: str, window_hours: int = 3) -> dict:
        """Check if Boss issued a constraint against state changes in the last N hours.
        Returns {"constrained": True/False, "constraint": "..."} """
        if not transcript_path or not os.path.exists(transcript_path):
            return {"constrained": False}

        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        constraint_phrases = [
            "don't make any changes",
            "do not make any changes",
            "don't change anything",
            "do not change anything",
            "no changes",
            "don't touch",
            "do not touch",
            "don't edit",
            "do not edit",
            "don't modify",
            "do not modify",
            "make no changes",
            "stop",
            "do nothing",
        ]

        try:
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    # Only check user/human messages
                    if entry.get("role") not in ("user", "human"):
                        continue
                    content = entry.get("content", "")
                    if not isinstance(content, str):
                        continue
                    msg_lower = content.lower()
                    for phrase in constraint_phrases:
                        if phrase in msg_lower:
                            return {
                                "constrained": True,
                                "constraint": content[:200],
                            }
        except Exception:
            pass
        return {"constrained": False}

    @staticmethod
    def check_resume_directive(transcript_path: str) -> dict:
        """Step 0: Check last 15 minutes of transcript for resume directive.
        If found, return the mode and last context so boot skips mode question."""
        if not transcript_path or not os.path.exists(transcript_path):
            return {"resume": False}

        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)

        resume_phrases = [
            "resume", "pick up where we left off", "continue",
            "keep going", "carry on", "where were we",
            "pick up where you left off", "start where we left off",
        ]

        mode_phrases = {
            "idiot mode": 3, "idiot": 3,
            "normal mode": 2, "normal": 2,
            "unattended mode": 1, "unattended": 1,
        }

        last_mode = None
        resume_found = False
        last_context = ""

        try:
            lines = []
            with open(transcript_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        lines.append(json.loads(line))
                    except Exception:
                        continue

            # Read backwards — most recent first
            for entry in reversed(lines):
                if entry.get("role") not in ("user", "human"):
                    continue
                content = entry.get("content", "")
                if not isinstance(content, str):
                    continue
                msg_lower = content.lower()

                # Check for resume directive
                for phrase in resume_phrases:
                    if phrase in msg_lower:
                        resume_found = True

                # Check for mode
                for phrase, mode_num in mode_phrases.items():
                    if phrase in msg_lower:
                        if last_mode is None:
                            last_mode = mode_num

                # Capture last context
                if not last_context and len(content) > 10:
                    last_context = content[:200]

                # Only look at recent messages
                if resume_found and last_mode is not None:
                    break

        except Exception:
            pass

        if resume_found:
            return {
                "resume": True,
                "mode": last_mode or 3,  # default idiot mode
                "last_context": last_context,
            }
        return {"resume": False}

    def boot(self) -> dict:
        """session_start — load brain, verify all JSONs, check matrix propensities."""
        self._announce("boot", "session_start", "brain")
        system_abends = []
        session_abends = []
        system_warnings = []
        session_warnings = []

        for stem, method_name in self._JSON_FUNCTION_MAP.items():
            if method_name is None or stem not in self.brain:
                continue
            result = getattr(self, method_name)(source="boot", destination=stem, action_event="session_start")
            label = result.get("propensity", "allow")

            if label == "system_abend":
                system_abends.append(f"{stem}: {result.get('reason', 'system non-compliant')}")
            elif label == "session_abend":
                session_abends.append(f"{stem}: {result.get('reason', 'session non-compliant')}")
            elif label == "system_warning":
                system_warnings.append(f"{stem}: {result.get('reason', 'system risk')}")
            elif label == "session_warning":
                session_warnings.append(stem)

        if system_abends:
            msg = "SYSTEM ABEND:\n" + "\n".join(f"  - {a}" for a in system_abends)
            print(msg, file=sys.stderr)
            return {"status": "system_abend", "system_abends": system_abends,
                    "continue": False, "stopReason": msg}

        if session_abends:
            msg = "SESSION ABEND:\n" + "\n".join(f"  - {a}" for a in session_abends)
            print(msg, file=sys.stderr)
            return {"status": "session_abend", "session_abends": session_abends,
                    "continue": False, "stopReason": msg}

        self._save_digest()
        self._booted = True
        settings = self._read_settings()
        settings["booted"] = True
        # BUG-GOV-009: deterministic enforcement of BOOT DIRECTIVE — MODE SELECTION.
        # Claude's next turn MUST present the verbatim mode prompt; prompt_result
        # will block the turn if it doesn't. User's next reply clears this.
        settings["mode_selected"] = False
        self._write_settings(settings)
        self._state["boot_complete"] = True

        # ARCH-ORPHAN-001-REQ-001: Scan for orphan bugs
        orphans = self._scan_orphans()

        summary = {
            "status": "booted",
            "brain_files": len(self.brain),
            "constraints": len(self._constraints),
            "system_warnings": system_warnings,
            "session_warnings": session_warnings,
            "orphan_bugs": orphans,
        }
        _log.info("Boot: %d files, %d constraints, %d sys_warn, %d sess_warn, %d orphans",
                  len(self.brain), len(self._constraints), len(system_warnings), len(session_warnings), len(orphans))
        return summary

    def _scan_orphans(self) -> list:
        """ARCH-ORPHAN-001-REQ-002: Scan bugs.json for ORPHAN_BUG entries. Pure file I/O."""
        try:
            bugs_path = BRAIN_DIR / "bugs.json"
            with open(bugs_path, encoding="utf-8") as f:
                data = json.load(f)
            orphans = []
            for bug in data.get("bug", []):
                if bug.get("orphan") is True:
                    orphans.append({
                        "bugid": bug.get("bugid", "?"),
                        "status": bug.get("status", "?"),
                        "title": (bug.get("title", "") or "")[:80],
                        "discovery_date": bug.get("discovery_date", "?"),
                    })
            return orphans
        except FileNotFoundError as e:
            print(f"BOOT WARNING: bugs.json not found: {e}", file=sys.stderr)
            import traceback; traceback.print_exc(file=sys.stderr)
            return []
        except json.JSONDecodeError as e:
            print(f"BOOT WARNING: bugs.json corrupt: {e}", file=sys.stderr)
            import traceback; traceback.print_exc(file=sys.stderr)
            return []
        except Exception as e:
            print(f"BOOT WARNING: orphan triage failed: {e}", file=sys.stderr)
            import traceback; traceback.print_exc(file=sys.stderr)
            return []

    def _serialize_singleton(self) -> dict:
        """Serialize essential singleton state for Claude's context.
        Includes: version, governance matrix, constraints, brain file list."""
        version_data = self.brain.get("version", {})
        current = version_data.get("current", {})
        matrix = self._get_matrix()

        # Compact matrix: only non-"allow" cells (the interesting ones)
        compact_matrix = {}
        for stem, row in matrix.items():
            non_allow = {col: val for col, val in row.items() if val != "allow"}
            if non_allow:
                compact_matrix[stem] = non_allow

        return {
            "version": current.get("version", "?"),
            "framework": current.get("framework", "?"),
            "build": current.get("build", "?"),
            "brain_files": list(self.brain.keys()),
            "constraint_count": len(self._constraints),
            "constraints": self._constraints,
            "governance_matrix_non_allow": compact_matrix,
            "state": {k: v for k, v in self._state.items() if not k.startswith("_")},
        }

    def inform_claude(self, boot_result: dict) -> str:
        """ARCH-ORPHAN-001-REQ-004/010: Build additionalContext for Claude on boot.
        Emits singleton state and the mode-selection directive ONLY.
        BUG-GOV-011: orphan-triage directive is emitted later — after REQ-010 is answered
        — so the two questions are sequential as REQ-011 requires."""
        lines = []

        # Serialize singleton state
        singleton_state = self._serialize_singleton()
        lines.append("GOVERNANCE SINGLETON STATE (serialized at boot):")
        lines.append(json.dumps(singleton_state, indent=2, default=str))
        lines.append("")

        # REQ-010: Mode selection directive (ONLY directive emitted at boot)
        lines.append("BOOT DIRECTIVE — MODE SELECTION (ARCH-ORPHAN-001-REQ-010):")
        lines.append("You MUST ask the user to select an operating mode before doing anything else.")
        lines.append("Present exactly this prompt:")
        lines.append("")
        lines.append("  Select operating mode:")
        lines.append("  1 = Unattended (Claude works independently, orphan bugs are deferred)")
        lines.append("  2 = Normal (Claude works with Boss, decisions require approval)")
        lines.append("  3 = Idiot (Boss reviews every action, Claude explains everything)")
        lines.append("")
        lines.append("Do NOT proceed until the user replies with 1, 2, or 3.")
        lines.append("Do NOT ask any other boot question in this turn — the orphan-triage directive")
        lines.append("(REQ-011) will be emitted in the NEXT turn, after the user answers REQ-010.")
        lines.append("")
        return "\n".join(lines)

    def _build_orphan_triage_directive(self, orphans: list) -> str:
        """BUG-GOV-011 / ARCH-DEVOPS-BOOT-001-REQ-011: Build the orphan-triage directive.
        Emitted on the turn AFTER the user replies with the mode selection, so Claude
        asks the two boot questions sequentially."""
        lines = []
        lines.append("BOOT DIRECTIVE — ORPHAN BUG TRIAGE (ARCH-DEVOPS-BOOT-001-REQ-011):")
        lines.append("Mode is now selected. Present this question VERBATIM on its own line:")
        lines.append("  Triage orphan bugs?")
        lines.append("Then wait for a yes/no answer.")
        if orphans:
            lines.append(f"If 'no': boot is complete. If 'yes': present the {len(orphans)} orphan bugs")
            lines.append("below and let the user choose how many to triage — one, several, or all.")
            lines.append("Triaging an orphan means assigning it a req_id or closing it. There is no")
            lines.append("per-orphan defer option.")
            lines.append("")
            lines.append("| ID | Type | Description | Date |")
            lines.append("|---|---|---|---|")
            for o in orphans:
                lines.append(f"| {o['id']} | {o['type']} | {o['rule']} | {o['date']} |")
        else:
            lines.append("(There are currently no orphan bugs. A 'yes' answer simply confirms the empty list.)")
        lines.append("")
        return "\n".join(lines)

    def dispatch_code_controlled(self, hook_input: dict, action_event: str) -> dict:
        """Dispatch to child class workers for code_controlled cells in the grid.
        Reads the matrix, finds code_controlled cells for this action_event,
        constructs the child class from WORKER_REGISTRY, calls run()."""
        matrix = self._get_matrix()
        results = []
        for json_stem, row in matrix.items():
            cell = row.get(action_event, "allow")
            if cell != "code_controlled":
                continue
            worker_cls = WORKER_REGISTRY.get(json_stem)
            if worker_cls is None:
                _log.warning("code_controlled for %s but no worker in WORKER_REGISTRY", json_stem)
                continue
            try:
                worker = worker_cls.from_dict(self.brain.get(json_stem, {}))
                result = worker.run(hook_input=hook_input, action_event=action_event)
                results.append({"json": json_stem, **result})
                if not result.get("comply", True):
                    return result  # First non-comply stops
            except Exception as e:
                _log.warning("Worker %s failed: %s", json_stem, e)
                results.append({"json": json_stem, "comply": True, "error": str(e)})
        return {"comply": True, "workers": results}

    def prompt(self, user_message: str, hook_input: dict = None) -> dict:
        """UserPromptSubmit — boot on first call, then dispatch code_controlled workers.
        Boot moved here from SessionStart to avoid OAuth race condition."""
        self._announce("prompt", "user_prompt_submit", f"user_message[:{min(50, len(user_message))}]")
        self._state["last_prompt"] = user_message

        # Check boot flag — None means settings file/flag missing (error state)
        boot_state = self.booted()
        if boot_state is None:
            return {
                "comply": True,
                "_boot_context": (
                    "ERROR: .claude/ChatHealthySettings.json or boot flag not found. "
                    "No boot attempted. SessionEnd may not have run on the previous session. "
                    "To fix: ensure .claude/ChatHealthySettings.json exists with {\"booted\": false}."
                ),
            }

        # BUG-GOV-009: if we're already booted and awaiting a mode reply,
        # record the user's selection so prompt_result's verbatim check relaxes
        # on the next turn. Accept a leading 1/2/3 token as the selection.
        # BUG-GOV-011: when mode is recorded on THIS turn, emit the orphan-triage
        # directive in this turn's additionalContext so REQ-011 is asked AFTER
        # REQ-010 is answered (sequential boot-question contract).
        mode_just_selected = False
        if boot_state and not self._mode_selected():
            stripped = (user_message or "").strip()
            first_token = stripped.split()[0] if stripped else ""
            first_token = first_token.rstrip(".,!):;").lower()
            if first_token in {"1", "2", "3"}:
                settings = self._read_settings()
                settings["mode_selected"] = True
                settings["mode"] = first_token
                self._write_settings(settings)
                mode_just_selected = True

        # First prompt triggers boot — singleton flag prevents re-boot
        if not boot_state:
            self._load_brain()
            self._verify_coverage()
            self._extract_constraints()
            boot_result = self.boot()
            claude_context = self.inform_claude(boot_result)
            # Dispatch workers as normal, then merge boot context
            worker_result = self.dispatch_code_controlled(hook_input or {}, "user_prompt_submit")
            # Merge any worker additionalContext with boot context
            worker_additional = ""
            for w in worker_result.get("workers", []):
                if w.get("additionalContext"):
                    worker_additional += w["additionalContext"] + "\n"
            combined_context = claude_context
            if worker_additional:
                combined_context += "\n" + worker_additional.strip()
            worker_result["_boot_context"] = combined_context
            worker_result["_booted"] = True
            return worker_result

        # Already booted — dispatch workers
        result = self.dispatch_code_controlled(hook_input or {}, "user_prompt_submit")

        # BUG-GOV-011: if mode was just recorded on this turn, emit the orphan-triage
        # directive now so Claude asks REQ-011 on this turn (sequentially after REQ-010).
        if mode_just_selected:
            orphans = self._scan_orphans()
            triage_context = self._build_orphan_triage_directive(orphans)
            worker_additional = ""
            for w in result.get("workers", []):
                if w.get("additionalContext"):
                    worker_additional += w["additionalContext"] + "\n"
            combined = triage_context
            if worker_additional:
                combined += "\n" + worker_additional.strip()
            result["_boot_context"] = combined

        # BRAIN-RUNTIME-REQ-004: detect mid-session edits to the three runtime brain
        # JSONs (bugs.json, engineering_rules.json, agile_backlog.json). If any have
        # been edited since last prompt, the @-imported copy in Claude's context is
        # stale. Emit a reminder so Claude knows to refresh before proceeding.
        try:
            stale_reminder = self._check_brain_freshness()
            if stale_reminder:
                existing = result.get("_boot_context", "") or ""
                result["_boot_context"] = (existing + "\n\n" + stale_reminder).strip()
        except Exception as e:
            _log.warning("BRAIN-RUNTIME-REQ-004 freshness check failed: %s", e)

        return result

    def tool_call(self, tool_name: str, tool_input: dict, transcript_path: str = "") -> dict:
        """PreToolUse — dispatch to code_controlled workers via the grid.
        GPT-4.1-mini adjudicates all non-read tool calls."""
        detail = tool_input.get("command", tool_input.get("file_path", ""))[:80]
        self._announce("tool_call", f"pre_tool_use:{tool_name}", detail)

        # Pass tool context into hook_input so workers can see it
        hook_input = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "transcript_path": transcript_path,
        }
        result = self.dispatch_code_controlled(hook_input, "pre_tool_use")

        # Convert worker result to allow/deny for Claude Code
        if not result.get("comply", True):
            return {"allow": False, "reason": result.get("reason", "Blocked by governance")}
        return {"allow": True}

    def prompt_result(self, hook_input: dict) -> dict:
        """Stop — dispatch code_controlled workers for the stop event.
        REQ-013: always adjudicate Claude's final output against engineering rules,
        independent of the governance matrix. This is a required universal-governance
        gate; modifying governance_matrix.json to add stop=code_controlled for
        engineering_rules would require Boss risk acceptance per v4-011, so we wire
        the adjudication directly here instead."""
        self._announce("prompt_result", "stop", "transcript")

        # BUG-GOV-009: after boot, Claude's turn-1 output MUST contain the verbatim
        # mode-selection prompt. If it doesn't, block and force a redo.
        try:
            if self.booted() and not self._mode_selected():
                transcript_path = hook_input.get("transcript_path", "")
                assistant_output = engineering_rules_worker._read_last_assistant_message(transcript_path)
                if not self._assistant_output_has_mode_prompt(assistant_output):
                    return {
                        "comply": False,
                        "decision": "block",
                        "reason": (
                            "BUG-GOV-009 / ARCH-ORPHAN-001-REQ-010: this is the first "
                            "assistant turn after boot, and Claude's output did not "
                            "present the verbatim mode-selection prompt. Claude MUST "
                            "respond with exactly this prompt and nothing else that "
                            "steers past it:\n\n"
                            "  Select operating mode:\n"
                            "  1 = Unattended (Claude works independently, orphan bugs are deferred)\n"
                            "  2 = Normal (Claude works with Boss, decisions require approval)\n"
                            "  3 = Idiot (Boss reviews every action, Claude explains everything)\n\n"
                            "Do NOT proceed until the user replies with 1, 2, or 3."
                        ),
                        "rule_ids": ["BUG-GOV-009", "ARCH-ORPHAN-001-REQ-010"],
                    }
        except Exception as e:
            _log.warning("BUG-GOV-009 mode-prompt check failed: %s", e)

        # REQ-013 direct adjudication of final assistant output.
        try:
            worker = engineering_rules_worker.from_dict({})
            verdict = worker.run(hook_input, "stop")
            if not verdict.get("comply", True):
                return verdict  # {comply: False, decision: "block", reason: "..."}
        except Exception as e:
            _log.warning("REQ-013 stop adjudication failed: %s", e)

        return self.dispatch_code_controlled(hook_input, "stop")

    # ── File path checker ──────────────────────────────────────────────────

    @staticmethod
    def _check_file_path(file_path: str) -> dict:
        if not file_path:
            return {"allow": False, "reason": "No file path"}
        normalized = file_path.replace("\\", "/")
        basename = os.path.basename(normalized)

        ALLOWED_EXT = {
            ".py", ".yaml", ".yml", ".json", ".md", ".html", ".htm",
            ".tsx", ".ts", ".js", ".jsx", ".css", ".scss",
            ".env", ".toml", ".cfg", ".ini", ".txt", ".csv",
            ".sh", ".bat", ".ps1", ".svg", ".xml",
            ".gitignore", ".dockerignore",
        }
        ALLOWED_NAMES = {"Dockerfile", "Caddyfile", "Makefile", "Procfile", ".gitignore", ".dockerignore", ".env", ".env.local"}
        ALLOWED_ROOT = {"CLAUDE.md", "ROADMAP.md", "README.md", ".gitignore"}
        ALLOWED_DIRS = {"Code/", "brain/", "Website/", ".claude/", ".github/", "docs/", "Analysis/"}

        if basename in ALLOWED_NAMES:
            return {"allow": True}
        _, ext = os.path.splitext(basename)
        if ext and ext.lower() not in ALLOWED_EXT:
            return {"allow": False, "reason": f"Extension '{ext}' not allowed"}

        rel_path = normalized
        for marker in ["chatHealthy/findCare/", "chatHealthy\\findCare\\"]:
            idx = normalized.find(marker)
            if idx >= 0:
                rel_path = normalized[idx + len(marker):]
                break

        if "/" not in rel_path and rel_path in ALLOWED_ROOT:
            return {"allow": True}
        if ".claude/" in rel_path or "memory/" in rel_path:
            return {"allow": True}
        for d in ALLOWED_DIRS:
            if rel_path.startswith(d):
                return {"allow": True}
        return {"allow": False, "reason": f"Not in allowed directory: {rel_path}"}


# ── CLI entry point ────────────────────────────────────────────────────────

LOG_PATH = Path(tempfile.gettempdir()) / "chathealthy_guard.log"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["prompt", "tool_call", "prompt_result", "session_start", "session_end"])
    args = parser.parse_args()

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    exit_code = 0
    try:
        if args.mode == "prompt":
            boot = chathealthy_devops_boot(load_full=False)
            user_msg = data.get("prompt", data.get("content", data.get("message", "")))
            result = boot.prompt(user_msg, hook_input=data)

            # Build additionalContext — includes boot context on first prompt
            additional = result.pop("_boot_context", "")
            for w in result.get("workers", []):
                if w.get("additionalContext"):
                    if additional:
                        additional += "\n"
                    additional += w["additionalContext"]
            if additional:
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": additional.strip(),
                    }
                }
                json.dump(output, sys.stdout)
            else:
                json.dump(result, sys.stdout)

        elif args.mode == "tool_call":
            boot = chathealthy_devops_boot(load_full=False)
            result = boot.tool_call(data.get("tool_name", ""), data.get("tool_input", {}), transcript_path=data.get("transcript_path", ""))
            if not result.get("allow", False):
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": result.get("reason", "Blocked"),
                    }
                }
                json.dump(output, sys.stdout)
                exit_code = 2

        elif args.mode == "prompt_result":
            # Guard against infinite loop — if stop hook is already active, exit immediately
            if not data.get("stop_hook_active"):
                boot = chathealthy_devops_boot(load_full=False)
                result = boot.prompt_result(hook_input=data)
                # REQ-013: if adjudicator flagged a rule violation, tell Claude Code to
                # block the Stop and force Claude to redo the turn without the violation.
                if not result.get("comply", True) and result.get("decision") == "block":
                    output = {
                        "decision": "block",
                        "reason": result.get("reason", "Rule violation detected in assistant output."),
                    }
                    json.dump(output, sys.stdout)
                else:
                    json.dump(result, sys.stdout)

        elif args.mode == "session_start":
            # REQ-007: primary booted-flag clearer. No auth-dependent work.
            settings = chathealthy_devops_boot._read_settings()
            settings["booted"] = False
            # BUG-GOV-009: also reset the mode gate so the next boot re-asks.
            settings["mode_selected"] = False
            chathealthy_devops_boot._write_settings(settings)
            json.dump({"status": "session_started", "booted_cleared": True}, sys.stdout)

        elif args.mode == "session_end":
            # Secondary clearer — only fires on clean /logout, /exit, /clear.
            settings = chathealthy_devops_boot._read_settings()
            settings["booted"] = False
            # BUG-GOV-009: clear mode gate on clean exit too.
            settings["mode_selected"] = False
            chathealthy_devops_boot._write_settings(settings)
            # BRAIN-RUNTIME-REQ-005: delete prior transient, then regenerate.
            # Load brain FIRST — only delete+regen if we have content to write.
            # This prevents a bad brain load from leaving CLAUDE.md's @-import
            # pointing at a non-existent file until the next prompt.
            deleted = []
            regenerated_bytes = None
            try:
                chathealthy_devops_boot._instance = None
                s = chathealthy_devops_boot(load_full=True)
                has_reqs = bool(s.brain.get("agile_backlog", {}).get("epics"))
                if has_reqs:
                    for p in (CACHE_DIR / "req_index.txt", CACHE_DIR / "req_index.txt.tmp"):
                        try:
                            if p.exists():
                                p.unlink()
                                deleted.append(str(p))
                        except Exception as e:
                            _log.warning("session_end: failed to delete %s: %s", p, e)
                    regenerated_bytes = s._write_req_index()
                else:
                    _log.warning(
                        "session_end: brain loaded without agile_backlog content "
                        "— preserving existing req_index.txt"
                    )
            except Exception as e:
                _log.warning("session_end: transient regeneration failed: %s", e)
            json.dump({
                "status": "session_ended",
                "booted_cleared": True,
                "transient_deleted": deleted,
                "transient_regenerated_bytes": regenerated_bytes,
            }, sys.stdout)

    except Exception:
        import traceback
        stack = traceback.format_exc()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"MODE: {args.mode}\n")
                f.write(f"TIME: {__import__('datetime').datetime.now().isoformat()}\n")
                f.write(stack)
                f.write(f"\n{'='*60}\n")
        except Exception:
            pass
        print(f"BUG-GOV-001: Guard crashed — stack logged to {LOG_PATH}", file=sys.stderr)
        # Don't block on crash — fail open so plugin doesn't die
        exit_code = 0
    finally:
        # ALWAYS return control to Cursor/Claude Code — never hang, never block
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
