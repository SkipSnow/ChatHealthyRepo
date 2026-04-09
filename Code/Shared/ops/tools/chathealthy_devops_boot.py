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
        "policies": "check_policies",
        "project_manifest": "check_project_manifest",
        "prompts": "check_prompts",
        "functional_discrepancy_reports": "check_functional_discrepancy_reports",
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
            return [p.get("policy", "") or p.get("description", "") for p in data.get("policies", []) if p.get("policy") or p.get("description")]

        # Governance sign-offs
        if json_stem == "governance":
            return [e.get("description", "") for e in data.get("sign_offs", []) if e.get("description")]

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

    def check_functional_discrepancy_reports(self, source="", destination="", action_event="session_start") -> dict:
        return self._check_json("functional_discrepancy_reports", method)

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

    def prompt(self, user_message: str) -> dict:
        """UserPromptSubmit — predict governance path for this prompt."""
        self._announce("prompt", "user_prompt_submit", f"user_message[:{min(50, len(user_message))}]")
        self._state["last_prompt"] = user_message

        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
            constraints = self.get_constraints_summary(max_chars=2000)

            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=300,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a governance advisor. A user sent a message to an AI coding agent. "
                            "Predict what the agent will need to do and flag any constraints.\n\n"
                            f"Constraints:\n{constraints}\n\n"
                            "Respond JSON: {{\"intent\": \"...\", \"predicted_actions\": [\"...\"], "
                            "\"warnings\": [\"...\"], \"proceed\": true/false}}"
                        ),
                    },
                    {"role": "user", "content": user_message[:1000]},
                ],
            )
            result = json.loads(resp.choices[0].message.content)
            self._state["predicted_path"] = result
            return result
        except Exception as e:
            return {"intent": "unknown", "warnings": [str(e)], "proceed": True}

    def tool_call(self, tool_name: str, tool_input: dict) -> dict:
        """PreToolUse — gate actions. Static file checks + defense-in-depth."""
        detail = tool_input.get("command", tool_input.get("file_path", ""))[:80]
        self._announce("tool_call", f"pre_tool_use:{tool_name}", detail)
        # Read-only — always pass
        if tool_name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
            return {"allow": True}

        # Edit/Write — check file path
        if tool_name in ("Edit", "Write"):
            return self._check_file_path(tool_input.get("file_path", ""))

        # Bash — defense in depth (native permissions are first layer)
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if re.search(r"python\s+.*Code[/\\]DataPipelines[/\\](?!tests[/\\])", command, re.IGNORECASE):
                return {"allow": False, "reason": "v4-001C: Pipeline code runs on Azure, not locally"}
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
            result = boot.boot()
            json.dump(result, sys.stdout)
            if result.get("status") == "abend":
                sys.exit(2)
            sys.exit(0)

        elif args.mode == "prompt":
            boot = chathealthy_devops_boot(load_full=False)
            user_msg = data.get("content", data.get("message", ""))
            result = boot.prompt(user_msg)
            json.dump(result, sys.stdout)
            sys.exit(0)

        elif args.mode == "tool_call":
            boot = chathealthy_devops_boot(load_full=False)
            result = boot.tool_call(data.get("tool_name", ""), data.get("tool_input", {}))
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
