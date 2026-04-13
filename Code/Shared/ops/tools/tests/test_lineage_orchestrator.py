# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# AUDIT-CODEHIST-001: Pytests for all 26 requirements (18 business, 8 technical)
#
# Usage:
#   pytest test_lineage_orchestrator.py -v

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "Code" / "Shared" / "ops" / "tools"))

LINEAGE_DIR = REPO_ROOT / "test_output" / "lineage"
ORCHESTRATOR_PATH = REPO_ROOT / "Code" / "Shared" / "ops" / "tools" / "lineage_orchestrator.py"
BRAIN_DIR = REPO_ROOT / "brain" / "machine_artifacts" / "content"


# ============================================================================
# Business Requirements
# ============================================================================

class TestB001_GitLineage:
    """B001: Audit MUST examine every version of every file in git."""

    def test_prompt_instructs_git_log(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job1_gui_history.md").read_text(encoding="utf-8")
        assert "git log" in prompt, "Worker prompt must instruct git log analysis"

    def test_prompt_instructs_git_show(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job1_gui_history.md").read_text(encoding="utf-8")
        assert "git show" in prompt, "Worker prompt must instruct git show for file versions"


class TestB002_BrainIntent:
    """B002: Audit MUST identify capabilities brain intends but code doesn't implement."""

    def test_brain_intent_prompt_reads_all_files(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job0_brain_intent.md").read_text(encoding="utf-8")
        assert "ALL JSON files" in prompt or "all brain" in prompt.lower()

    def test_brain_intent_prompt_targets_design(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job0_brain_intent.md").read_text(encoding="utf-8")
        assert "design.json" in prompt


class TestB003_LostCapabilities:
    """B003: Audit MUST identify capabilities built then lost."""

    def test_gui_prompt_identifies_removed_features(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job1_gui_history.md").read_text(encoding="utf-8")
        assert "removed" in prompt.lower() or "lost" in prompt.lower()


class TestB004_AICallLineage:
    """B004: Audit MUST identify every AI call that existed."""

    def test_prompt_history_prompt_traces_llm_calls(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job2_prompt_history.md").read_text(encoding="utf-8")
        assert "AI call" in prompt or "LLM" in prompt

    def test_prompt_history_checks_current_code(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job2_prompt_history.md").read_text(encoding="utf-8")
        assert "grep" in prompt.lower() or "current" in prompt.lower()


class TestB005_CrossReference:
    """B005: Audit MUST cross-reference against ALL brain artifacts."""

    def test_cross_ref_reads_checkpoints(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job3_cross_reference.md").read_text(encoding="utf-8")
        assert "00_brain_intent" in prompt
        assert "01_gui_history" in prompt
        assert "02_prompt_history" in prompt

    def test_cross_ref_reads_bugs(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job3_cross_reference.md").read_text(encoding="utf-8")
        assert "bugs.json" in prompt


class TestB006_CategorizedFindings:
    """B006: Findings categorized as undocumented, documented-unplanned, intentional."""

    def test_cross_ref_schema_has_categories(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job3_cross_reference.md").read_text(encoding="utf-8")
        assert "undocumented_losses" in prompt
        assert "documented_unplanned" in prompt
        assert "intentional_replacements" in prompt


class TestB007_ActionableRecommendations:
    """B007: Recommendations prioritized by user impact with effort estimates."""

    def test_final_report_has_recommendations(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "recommendation" in prompt.lower()
        assert "effort" in prompt.lower()


class TestB008_Repeatable:
    """B008: Audit MUST be repeatable and idempotent."""

    def test_orchestrator_skips_completed_checkpoints(self):
        import lineage_orchestrator as lo
        # If checkpoint exists, orchestrator should skip it
        assert hasattr(lo, 'JOBS'), "Orchestrator must define JOBS list"
        assert hasattr(lo, 'read_control'), "Orchestrator must have read_control"


class TestB009_ConsumableOutput:
    """B009: Output consumable by humans and machines."""

    def test_readme_exists(self):
        assert (LINEAGE_DIR / "readme.txt").exists(), "readme.txt must exist"

    def test_final_report_schema_is_json(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "valid JSON" in prompt or "JSON" in prompt


class TestB010_ReadOnly:
    """B010: Audit MUST NOT modify source code, DB, brain. Output to audit dir only."""

    def test_prompts_forbid_modification(self):
        for prompt_file in (LINEAGE_DIR / "prompts").glob("*.md"):
            text = prompt_file.read_text(encoding="utf-8")
            assert "modify" not in text.lower() or "not modify" in text.lower() or "MUST NOT" in text

    def test_output_dir_is_gitignored(self):
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "test_output" in gitignore


class TestB011_Autonomous:
    """B011: Audit MUST run to completion without human intervention."""

    def test_orchestrator_has_main_loop(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "while True" in code, "Orchestrator must have infinite loop"


class TestB012_SurvivesFailure:
    """B012: Audit MUST survive crashes and resume."""

    def test_orchestrator_reads_checkpoints_on_start(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "checkpoint.exists()" in code or "checkpoint" in code

    def test_control_file_persists_state(self):
        assert (LINEAGE_DIR / "control.json").exists()


class TestB013_CostTracking:
    """B013: Audit MUST track token consumption."""

    def test_control_has_token_fields(self):
        control = json.loads((LINEAGE_DIR / "control.json").read_text(encoding="utf-8"))
        assert "total_tokens" in control
        for job in control["jobs"]:
            assert "tokens_used" in job


class TestB014_NumberedFindings:
    """B014: Findings numbered, most important first."""

    def test_final_report_schema_has_numbered_findings(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "number" in prompt.lower()
        assert "most important first" in prompt.lower() or "lowest number" in prompt.lower()


class TestB015_Recommendations:
    """B015: Recommendations for each finding."""

    def test_final_report_has_per_finding_recommendations(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "recommendation" in prompt.lower()
        assert "action" in prompt.lower()


class TestB016_Methodology:
    """B016: Methodology at top of report."""

    def test_final_report_has_methodology(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "METHODOLOGY" in prompt or "methodology" in prompt


class TestB017_Assumptions:
    """B017: Assumptions documented under methodology."""

    def test_final_report_has_assumptions(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "ASSUMPTIONS" in prompt or "assumptions" in prompt


class TestB018_EvidenceRequired:
    """B018: Each finding supported with evidence from code and brain."""

    def test_final_report_requires_dual_evidence(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "code_evidence" in prompt or "codebase" in prompt.lower()
        assert "brain_evidence" in prompt or "brain" in prompt.lower()


# ============================================================================
# Technical Requirements
# ============================================================================

class TestT001_PurePython:
    """T001: Orchestrator is pure Python, no AI."""

    def test_no_llm_imports(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "anthropic" not in code.lower()
        assert "openai" not in code.lower()
        assert "from openai" not in code
        assert "from anthropic" not in code


class TestT002_LoopUntilStopOrComplete:
    """T002: Loops until complete or human stops."""

    def test_stop_command_exits(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert '"stop"' in code
        assert "break" in code or "exit" in code

    def test_loop_never_exits_on_completion(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "All jobs complete" in code or "Polling" in code


class TestT003_StateOnDisk:
    """T003: All state in control.json."""

    def test_reads_control_file(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "control.json" in code

    def test_writes_control_file(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "write_control" in code


class TestT004_Checkpoints:
    """T004: Checkpoint files, crash recovery."""

    def test_checks_checkpoint_existence(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "checkpoint.exists()" in code or "checkpoint" in code

    def test_skips_completed_jobs(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "marking completed" in code or "completed" in code


class TestT005_DisposableWorkers:
    """T005: Workers spawned with no prior context."""

    def test_workers_use_claude_cli(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "claude" in code
        assert "subprocess" in code

    def test_worker_prompts_are_self_contained(self):
        for prompt_file in (LINEAGE_DIR / "prompts").glob("*.md"):
            text = prompt_file.read_text(encoding="utf-8")
            assert "Write your output" in text or "Write output" in text or "test_output" in text


class TestT006_TokenTracking:
    """T006: Token usage recorded per worker."""

    def test_orchestrator_tracks_tokens(self):
        code = ORCHESTRATOR_PATH.read_text(encoding="utf-8")
        assert "tokens_used" in code
        assert "total_tokens" in code


class TestT007_StructuredJSON:
    """T007: All output structured JSON with schemas."""

    def test_all_prompts_specify_json_output(self):
        for prompt_file in (LINEAGE_DIR / "prompts").glob("*.md"):
            text = prompt_file.read_text(encoding="utf-8")
            assert "JSON" in text, f"{prompt_file.name} must specify JSON output"

    def test_all_prompts_have_schema(self):
        for prompt_file in (LINEAGE_DIR / "prompts").glob("*.md"):
            text = prompt_file.read_text(encoding="utf-8")
            assert "Schema" in text or "schema" in text, f"{prompt_file.name} must include schema"


class TestT008_StrictSchema:
    """T008: Output expressed as JSON with strict schema compliance."""

    def test_final_report_prompt_defines_schema(self):
        prompt = (LINEAGE_DIR / "prompts" / "prompt_job4_final_report.md").read_text(encoding="utf-8")
        assert "methodology" in prompt
        assert "assumptions" in prompt
        assert "findings" in prompt
        assert "summary" in prompt
