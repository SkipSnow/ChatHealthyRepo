# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthy DevOps Boot — governance entry point for Claude Code.
#
# Four modes, mapped to Claude Code lifecycle hooks:
#   --mode boot          → SessionStart: load brain, cache digest
#   --mode prompt        → UserPromptSubmit: predict governance path
#   --mode tool_call     → PreToolUse: gate Bash/Edit/Write
#   --mode prompt_result → Stop: audit Claude's output
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

BRAIN_DIR = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "content"
BOOT_DIR = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "boot"
DIGEST_PATH = Path(tempfile.gettempdir()) / "chathealthy_brain_digest.json"

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
    Constructor validates bug data against governance constraints."""

    id: str = ""
    rule: str = ""
    description: str = ""
    type: str = ""  # constrained by CV-008 bug_type
    reason: str = ""
    severity: str = ""  # legacy — use type
    environments: list = []  # constrained by CV-010
    date: str = ""
    discovery_date: str = ""
    due_date: str = ""
    next_action: str = "analysis"
    status: str = "open"
    resolution_status: str = "in_analysis"  # constrained by CV-009
    risk_acceptance_id: str = None  # null until Boss authorizes
    pytest_id: str = ""
    pytest_success_criteria: str = ""
    success_criteria_to_close: str = ""
    source: str = ""
    ch_matrix_id: str = ""
    incident: str = ""
    risk_level: str = ""
    reason: str = ""

    def is_show_stopper(self) -> bool:
        return "SHOW STOPPER" in (self.rule or "") or "SHOW STOPPER" in (self.severity or "")

    def is_release_blocker(self) -> bool:
        return "RELEASE BLOCKER" in (self.rule or "") or "SPRINT BLOCKER" in (self.rule or "")

    def blocks_action(self, action_event: str) -> dict:
        """Check if this bug should block a governance action event."""
        if self.is_show_stopper() or self.is_release_blocker():
            return {
                "comply": False,
                "action": "session_abend",
                "reason": f"{self.id}: {self.severity or 'SHOW STOPPER'} — {(self.rule or self.description)[:150]}",
                "propensity": "session_abend",
            }
        return {"comply": True, "propensity": "allow"}

    def run_governance_process(self, transcript_path: str = "") -> dict:
        """Execute the governance process for this bug instance.
        Calls base class pre_run_checks first — Boss constraint and risk acceptance."""

        # Base class checks — Boss constraint, risk acceptance
        pre = self.pre_run_checks(transcript_path)
        if not pre.get("comply", True):
            return pre

        result = {
            "bug_id": self.id,
            "type": self.type,
            "resolution_status": self.resolution_status,
            "risk_acceptance_id": self.risk_acceptance_id,
            "comply": True,
        }

        # Show stoppers / release blockers without risk acceptance — escalate
        if not self.check_risk_acceptance() and (self.is_show_stopper() or self.is_release_blocker()):
            result["comply"] = False
            result["action"] = "escalate"
            result["reason"] = f"{self.id}: {self.type or 'SHOW STOPPER'} — no risk acceptance. Escalate to Boss."
            return result

        # Risk accepted — authorized
        if self.check_risk_acceptance():
            result["authorized_by"] = self.risk_acceptance_id
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
    """Pydantic worker for conversation_log.json.
    Polymorphic child — invoked by the singleton when the grid says code_controlled
    on user_prompt_submit. Logs the user's prompt to conversation_log.json."""

    MAX_CONTENT_LEN: int = 500
    SYSTEM_REMINDER_PATTERN: object = None  # set in __init__

    def __init__(self, **kwargs):
        super().__init__(**kwargs) if BaseModel is not object else None
        self.SYSTEM_REMINDER_PATTERN = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

    def _clean_content(self, content: str) -> str:
        if not content:
            return ""
        content = self.SYSTEM_REMINDER_PATTERN.sub("", content).strip()
        if content.startswith("data:") or "base64," in content:
            return "[binary content omitted]"
        if len(content) > self.MAX_CONTENT_LEN:
            content = content[:self.MAX_CONTENT_LEN] + f" [truncated — {len(content)} chars]"
        return content

    def _make_timestamps(self):
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        now_pst = now_utc + timedelta(hours=-7)
        return (
            now_pst.strftime("%Y-%m-%dT%H:%M:%S-07:00"),
            now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def run(self, hook_input: dict, action_event: str = "user_prompt_submit") -> dict:
        """Polymorphic run — called by singleton when grid says code_controlled."""
        if action_event == "user_prompt_submit":
            prompt = hook_input.get("prompt", hook_input.get("content", hook_input.get("message", "")))
            content = self._clean_content(prompt)
            if not content:
                return {"comply": True, "logged": False}

            log_path = BRAIN_DIR / "conversation_log.json"
            try:
                log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {
                    "collection": "conversation_log",
                    "path": "brain/machine_artifacts/content/conversation_log.json",
                    "purpose": "Rolling 24h conversation log.",
                    "produces_artifact": False,
                    "retention": "24_hours",
                    "utterances": [],
                }
                pst, utc = self._make_timestamps()
                last_num = max((u["utterance"] for u in log.get("utterances", [])), default=0)
                log["utterances"].append({
                    "utterance": last_num + 1,
                    "timestamp_pst": pst,
                    "timestamp_utc": utc,
                    "actor": "Skip",
                    "role": "user",
                    "content": content,
                })
                log_path.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
                return {"comply": True, "logged": True}
            except Exception as e:
                _log.warning("conversation_log_worker failed: %s", e)
                return {"comply": True, "logged": False, "error": str(e)}

        return {"comply": True}

    @classmethod
    def from_dict(cls, data: dict):
        return cls()


# ── Registry: JSON stem → child class ────────────────────────────────────────
# Only JSONs with code_controlled cells in the grid get a child class.
# The singleton uses this to dispatch.
WORKER_REGISTRY = {
    "bugs": bug_governance_constraints,
    "conversation_log": conversation_log_worker,
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
        "chathealthyai_brainboot": None,
        "governance_matrix": None,  # The matrix itself — not a constraint source, it IS the governance
        "controlled_vocabularies": "check_controlled_vocabularies",
        "conversation_log": "check_conversation_log",
        "daily_punch_list_with_results_and_accomplishments": "check_daily_punch_list",
        "design": "check_design",
        "development_rules": "check_development_rules",
        "emergency_keywords": "check_emergency_keywords",
        "external_audits": "check_external_audits",
        "governance": "check_governance",
        "legal": "check_legal",
        "operating_rules": "check_operating_rules",
        "pipeline_v3_compliance_log": "check_pipeline_v3_compliance_log",
        "pipeline_v3_iteration_log": "check_pipeline_v3_iteration_log",
        "pipeline_v4_design_iterations": "check_pipeline_v4_design_iterations",
        # policies merged into governance.json as top-level attribute
        "project_manifest": "check_project_manifest",
        "prompts": "check_prompts",
        "bugs": "check_bugs",
        "risk_acceptance": "check_risk_acceptance",
        "schema": "check_schema",
        "security": "check_security",
        "sprint_plan": "check_sprint_plan",
        "token_usage": "check_token_usage",
        "traceability_matrix": "check_traceability_matrix",
        "unrealized_ideas": "check_unrealized_ideas",
        "work_log": "check_work_log",
    }

    def __init__(self, load_full=False):
        if self._initialized:
            return
        self.brain = {}
        self._constraints = []
        self._state = {}  # Singleton state — persists across hook calls

        if load_full:
            self._load_brain()
            self._verify_coverage()
            self._extract_constraints()
        else:
            self._load_digest()
        self._initialized = True

    # ── Brain loading ──────────────────────────────────────────────────────

    def _load_brain(self):
        for json_file in sorted(BRAIN_DIR.glob("*.json")):
            try:
                with open(json_file, encoding="utf-8") as f:
                    self.brain[json_file.stem] = json.load(f)
            except Exception as e:
                _log.warning("Failed to load %s: %s", json_file.name, e)

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
        }
        with open(DIGEST_PATH, "w", encoding="utf-8") as f:
            json.dump(digest, f, indent=2)

    def _load_digest(self):
        if DIGEST_PATH.exists():
            try:
                with open(DIGEST_PATH, encoding="utf-8") as f:
                    self._constraints = json.load(f).get("constraints", [])
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

    def get_constraints_summary(self, max_chars=4000):
        text = "\n".join(f"- {c}" for c in self._constraints if c)
        return text[:max_chars] if len(text) <= max_chars else text[:max_chars] + "\n..."

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
            if not constraints and json_stem in ("operating_rules", "policies", "security"):
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
        if json_stem in ("operating_rules", "development_rules"):
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

        # Agile backlog
        if json_stem == "agile_backlog":
            return [f"EPIC: {e.get('name', '')} ({e.get('status', '')})" for e in data.get("epics", []) if e.get("name")]

        # Default — no extractable constraints
        return []

    # Per-JSON check functions — all delegate to _check_json with the method column
    # These exist so _JSON_FUNCTION_MAP can reference them by name

    def check_operating_rules(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("operating_rules", method)

    def check_development_rules(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("development_rules", method)

    def check_policies(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("policies", method)

    def check_governance(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("governance", method)

    def check_architecture(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("architecture", method)

    def check_security(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("security", method)

    def check_risk_acceptance(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("risk_acceptance", method)

    def check_legal(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("legal", method)

    def check_controlled_vocabularies(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("controlled_vocabularies", method)

    def check_agile_backlog(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("agile_backlog", method)

    def check_sprint_plan(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("sprint_plan", method)

    def check_ai_operations(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("ai_operations", method)

    def check_project_manifest(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("project_manifest", method)

    def check_schema(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("schema", method)

    def check_prompts(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("prompts", method)

    def check_emergency_keywords(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("emergency_keywords", method)

    def check_external_audits(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("external_audits", method)

    def check_bugs(self, source="", destination="", action_event="session_start", transcript_path="") -> dict:
        """Construct each bug, call run_governance_process. The class owns the rules."""
        bugs_data = self.brain.get("bugs", {})
        for bug_dict in bugs_data.get("bugs", []):
            bug = bug_governance_constraints.from_dict(bug_dict)
            result = bug.run_governance_process(transcript_path=transcript_path)
            if not result.get("comply", True):
                return result
        return _propensity_response("allow", "bugs")

    def check_traceability_matrix(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("traceability_matrix", method)

    def check_business_plan(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("business_plan", method)

    def check_design(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("design", method)

    def check_token_usage(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("token_usage", method)

    def check_work_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("work_log", method)

    def check_conversation_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("conversation_log", method)

    def check_daily_punch_list(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("daily_punch_list_with_results_and_accomplishments", method)

    def check_unrealized_ideas(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("unrealized_ideas", method)

    def check_pipeline_v3_compliance_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("pipeline_v3_compliance_log", method)

    def check_pipeline_v3_iteration_log(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("pipeline_v3_iteration_log", method)

    def check_pipeline_v4_design_iterations(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("pipeline_v4_design_iterations", method)

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
        self._state["boot_complete"] = True

        summary = {
            "status": "booted",
            "brain_files": len(self.brain),
            "constraints": len(self._constraints),
            "system_warnings": system_warnings,
            "session_warnings": session_warnings,
        }
        _log.info("Boot: %d files, %d constraints, %d sys_warn, %d sess_warn",
                  len(self.brain), len(self._constraints), len(system_warnings), len(session_warnings))
        return summary

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
        """UserPromptSubmit — dispatch code_controlled workers, then log."""
        self._announce("prompt", "user_prompt_submit", f"user_message[:{min(50, len(user_message))}]")
        self._state["last_prompt"] = user_message
        return self.dispatch_code_controlled(hook_input or {}, "user_prompt_submit")

    def tool_call(self, tool_name: str, tool_input: dict, transcript_path: str = "") -> dict:
        """PreToolUse — gate actions using the governance matrix.

        BUG-GOV-002: Before any state change, check if Boss has an active
        constraint in the transcript. Any pydantic run object inherits this.
        """
        detail = tool_input.get("command", tool_input.get("file_path", ""))[:80]
        self._announce("tool_call", f"pre_tool_use:{tool_name}", detail)

        # Read-only tools — always pass
        if tool_name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
            return {"allow": True}

        # Edit/Write — check file path
        if tool_name in ("Edit", "Write"):
            return self._check_file_path(tool_input.get("file_path", ""))

        # Bash — walk the matrix pre_tool_use column
        if tool_name == "Bash":
            command = tool_input.get("command", "")

            # Check show-stopper bugs — if any SHOW STOPPER bug is open, block
            bugs = self.brain.get("bugs", {})
            for bug in bugs.get("bugs", []):
                rule_text = bug.get("rule", "")
                bug_id = bug.get("id", "")
                if "SHOW STOPPER" in rule_text or "SPRINT BLOCKER" in rule_text:
                    # Check if this command could worsen the show stopper
                    if bug_id == "BUG-LOAD-001" and "copy_to_frontend" in command.lower():
                        # The destructive drop() — block unless using safe copy
                        if "copy_providers_only" not in command and "fix_frontend" not in command:
                            return {"allow": False, "reason": f"{bug_id}: CopyToFrontEnd has destructive drop(). Use copy_providers_only or fix_frontend_data.py"}

            # Check engineering rules that map to commands
            for rule in self.brain.get("operating_rules", {}).get("rules", []):
                rule_text = rule.get("rule", "")
                rule_id = rule.get("id", "")
                # v4-001C: Pipeline code runs on Azure
                if "pipeline" in rule_text.lower() and "azure" in rule_text.lower():
                    if re.search(r"python\s+.*Code[/\\]DataPipelines[/\\](?!tests[/\\])", command, re.IGNORECASE):
                        return {"allow": False, "reason": f"v4-001C: Pipeline code runs on Azure, not locally"}

            # Check development rules for enforcement patterns
            for rule in self.brain.get("development_rules", {}).get("rules", []):
                enforcement = rule.get("enforcement", {})
                if not enforcement:
                    continue
                rule_id = rule.get("id", "")
                # File scan enforcement — check if command touches scanned dirs
                if enforcement.get("type") == "file_scan":
                    pattern = enforcement.get("pattern", "")
                    if pattern and re.search(pattern, command, re.IGNORECASE):
                        return {"allow": False, "reason": f"{rule_id}: {rule.get('rule', '')[:100]}"}

            return {"allow": True}

        return {"allow": True}

    def prompt_result(self, transcript_path: str) -> dict:
        """Stop — audit Claude's output against brain constraints."""
        self._announce("prompt_result", "stop", "transcript")
        actions = []
        if transcript_path and os.path.exists(transcript_path):
            try:
                with open(transcript_path, encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if entry.get("tool_name"):
                            actions.append(f"{entry['tool_name']}: {json.dumps(entry.get('tool_input', {}))[:100]}")
            except Exception:
                pass

        if not actions:
            return {"compliant": True, "notes": "No actions to audit"}

        self._state["last_actions"] = actions

        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            constraints = self.get_constraints_summary(max_chars=2000)
            actions_desc = "\n".join(f"- {a}" for a in actions[-20:])

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=200,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a compliance auditor. Review AI agent actions for violations.\n\n"
                            f"Constraints:\n{constraints}\n\n"
                            "Respond JSON: {{\"compliant\": true/false, \"violations\": [\"...\"], \"notes\": \"...\"}}"
                        ),
                    },
                    {"role": "user", "content": f"Actions:\n{actions_desc}"},
                ],
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            return {"compliant": True, "notes": f"Audit unavailable: {e}"}

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
    parser.add_argument("--mode", required=True, choices=["boot", "prompt", "tool_call", "prompt_result"])
    args = parser.parse_args()

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    try:
        if args.mode == "boot":
            boot = chathealthy_devops_boot(load_full=True)

            # Step 0: Check for resume directive in transcript
            transcript = data.get("transcript_path", "")
            resume = boot.check_resume_directive(transcript)
            if resume.get("resume"):
                mode_names = {1: "Unattended", 2: "Normal", 3: "Idiot"}
                mode_name = mode_names.get(resume["mode"], "Idiot")
                print(f"RESUME: {mode_name} Mode. Last context: {resume.get('last_context', '')[:100]}", file=sys.stderr)
                result = boot.boot()
                result["resume"] = True
                result["mode"] = resume["mode"]
                result["mode_name"] = mode_name
                result["last_context"] = resume.get("last_context", "")
                json.dump(result, sys.stdout)
                sys.exit(0)

            result = boot.boot()
            json.dump(result, sys.stdout)
            if result.get("status") == "abend":
                sys.exit(2)
            sys.exit(0)

        elif args.mode == "prompt":
            boot = chathealthy_devops_boot(load_full=False)
            user_msg = data.get("prompt", data.get("content", data.get("message", "")))
            result = boot.prompt(user_msg, hook_input=data)
            json.dump(result, sys.stdout)
            sys.exit(0)

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
                sys.exit(2)
            sys.exit(0)

        elif args.mode == "prompt_result":
            boot = chathealthy_devops_boot(load_full=False)
            result = boot.prompt_result(data.get("transcript_path", ""))
            json.dump(result, sys.stdout)
            sys.exit(0)


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
        sys.exit(0)


if __name__ == "__main__":
    main()
