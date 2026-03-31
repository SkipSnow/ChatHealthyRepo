# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# PromptSystemMaker — builds runtime configuration from brain artifacts.
#
# Loads system prompt rules, tool definitions, and emergency keywords
# from JSON files. main.py consumes the output — no config in code.
#
# Source: brain/machine_artifacts/ (JSON files)
# Changed: extracted from main.py inline strings

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("findcare.system_manufacturing")


class PromptSystemMaker:
    """Builds runtime configuration from brain artifacts and environment.

    Responsible for:
    - System prompt assembly from rules
    - Tool definitions (Anthropic schema)
    - Emergency keywords
    - Welcome message
    """

    def __init__(self, brain_dir: str = None, env_prefix: str = "dev"):
        self._brain = Path(brain_dir) if brain_dir else self._find_brain()
        self._env = env_prefix
        self._prompt_config = None
        self._tools = None
        self._emergency_keywords = None

    @staticmethod
    def _find_brain() -> Path:
        """Walk up from this file to find the brain directory."""
        d = Path(__file__).parent
        for _ in range(5):
            brain = d / "brain"
            if brain.is_dir():
                return brain
            d = d.parent
        return Path("brain")

    # ------------------------------------------------------------------
    # Emergency keywords
    # Source: brain/machine_artifacts/emergency_keywords.json
    # ------------------------------------------------------------------
    def load_emergency_keywords(self) -> list[str]:
        """Load flat list of emergency keywords from brain artifact."""
        if self._emergency_keywords is not None:
            return self._emergency_keywords

        path = self._brain / "machine_artifacts" / "emergency_keywords.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            keywords = []
            for category_list in data.get("categories", {}).values():
                keywords.extend(category_list)
            self._emergency_keywords = keywords
            _log.info("Loaded %d emergency keywords from %s", len(keywords), path)
            return keywords
        except Exception as exc:
            _log.warning("Failed to load emergency keywords: %s — using defaults", exc)
            self._emergency_keywords = [
                "chest pain", "heart attack", "can't breathe", "stroke",
                "severe bleeding", "unconscious", "overdose", "suicide",
                "seizure", "anaphylaxis", "choking",
            ]
            return self._emergency_keywords

    # ------------------------------------------------------------------
    # Tool definitions — Anthropic format
    # Source: brain/machine_artifacts/anthropic_tools.json (or built inline)
    # ------------------------------------------------------------------
    def load_tool_definitions(self) -> list[dict]:
        """Load Anthropic tool schemas. Tries brain artifact first, falls back to built-in."""
        if self._tools is not None:
            return self._tools

        path = self._brain / "machine_artifacts" / "anthropic_tools.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._tools = json.load(f)
                _log.info("Loaded %d tool definitions from %s", len(self._tools), path)
                return self._tools
            except Exception as exc:
                _log.warning("Failed to load tool definitions: %s — using built-in", exc)

        self._tools = self._build_tool_definitions()
        return self._tools

    def _build_tool_definitions(self) -> list[dict]:
        """Built-in tool definitions. Fallback if brain artifact missing."""
        return [
            {
                "name": "find_providers",
                "description": "Search for healthcare providers in a specific US state. FindCare covers DE, MS, VA.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "specialty_query": {"type": "string"},
                        "state": {"type": "string"},
                        "city": {"type": "string"},
                        "county": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["specialty_query", "state"],
                },
            },
            {
                "name": "record_user_details",
                "description": "Record user contact details. Must complete two-tier consent flow first.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "name": {"type": "string"},
                        "notes": {"type": "string"},
                        "message": {"type": "string"},
                        "consent_verbatim": {"type": "boolean"},
                        "consent_summary": {"type": "boolean"},
                    },
                    "required": ["email", "notes", "consent_verbatim"],
                },
            },
            {
                "name": "record_unknown_question",
                "description": "Record unanswerable question. Present response_template VERBATIM.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "question_class": {"type": "string", "enum": ["healthcare_capability", "medical_advice", "irrelevant"]},
                        "consent": {"type": "boolean"},
                    },
                    "required": ["question", "question_class"],
                },
            },
            {
                "name": "find_specialty_codes",
                "description": "Look up medical specialty taxonomy codes (NUCC).",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "search_clinical_trials",
                "description": "Search for actively recruiting clinical trials on ClinicalTrials.gov.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "condition": {"type": "string"},
                        "location": {"type": "string"},
                        "user_location": {"type": "string", "description": "User location for travel time — any location worldwide."},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["condition"],
                },
            },
            {
                "name": "get_skip_snow_context",
                "description": "Return Skip Snow's professional background, career summary, and LinkedIn profile.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "get_chathealthy_context",
                "description": "Return ChatHealthy.ai company context: business plan and operating principles.",
                "input_schema": {"type": "object", "properties": {}},
            },
            {
                "name": "lookup_provider_external",
                "description": "Look up provider details from NPI Registry and research links.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "provider_name": {"type": "string"},
                        "npi": {"type": "string"},
                        "state": {"type": "string"},
                    },
                    "required": ["provider_name"],
                },
            },
        ]

    # ------------------------------------------------------------------
    # System prompt — assembled from rules
    # ------------------------------------------------------------------
    def build_system_prompt(self, name: str = "Skip Snow", website: str = "ChatHealthy.AI",
                            emergency_response: str = "", follow_up_check: bool = False) -> str:
        """Build the system prompt from rules. No hardcoded strings in main.py."""
        rules = [
            f"You are acting as {name} on {website}'s website. "
            f"Represent {name} and {website} faithfully. Be professional and engaging.",

            f"RULE 0 — EMERGENCY: If the user describes ANY medical emergency, "
            f"STOP. Do not use any tool. Respond ONLY with: '{emergency_response}'",

            f"RULE 1 — CONTEXT TOOLS: "
            f"Call get_skip_snow_context for {name}'s background. "
            f"Call get_chathealthy_context for {website}'s mission. "
            f"Only when explicitly asked. Never for greetings. Answer ONLY from tool results. "
            f"CRITICAL: Output the 'connect' field EXACTLY as-is — it contains pre-formatted markdown links.",

            f"RULE 2 — UNANSWERABLE: You know ONLY: {name}'s background, {website}'s platform, "
            f"providers in DE/MS/VA, specialties, clinical trials. NOTHING else. "
            f"For anything outside these sources, call record_unknown_question. "
            f"Present the template VERBATIM. Do NOT answer from training data.",

            f"RULE 3 — MEDICAL ADVICE: Decline all. Cannot prescribe, diagnose, or recommend treatment. "
            f"CAN help navigate: find providers, specialists, clinical trials.",

            f"RULE 4 — PROVIDER FORMAT: Always bullet list, never markdown table. "
            f"Format: - **Name**\\n  - Address\\n  - County\\n  - Phone\\n  - NPI: number",

            f"RULE 5 — PROVIDER DETAIL: When user asks about a specific provider, "
            f"call lookup_provider_external.",

            f"RULE 6 — CLINICAL TRIAL TRAVEL: After first results, ask about travel distance. "
            f"User location can be ANYWHERE worldwide. Google Routes handles international. "
            f"Do NOT assume US-only. Do NOT include travel on first call.",
        ]

        if follow_up_check:
            rules.append(
                f"RULE 7 — FOLLOW-UP: Ask: 'Would you like someone from {website} to follow up?' "
                f"If yes, collect name/email, complete consent flow, call record_user_details."
            )

        return "\n\n## STRICT ANSWER RULES — NO EXCEPTIONS\n" + "\n".join(rules)
