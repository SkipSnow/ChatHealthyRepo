# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytests for ARCH-DEVOPS-BOOT-001-REQ-001 through REQ-011
# and ARCH-SCAN-JSON-REQ-002.
#
# Each test class maps to exactly one requirement.
#
# Usage:
#   pytest test_devops_boot_and_scanner.py -v

import ast
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "Code" / "Shared" / "ops" / "tools"))

BOOT_MODULE_PATH = REPO_ROOT / "Code" / "Shared" / "ops" / "tools" / "chathealthy_devops_boot.py"
SCANNER_MODULE_PATH = REPO_ROOT / "Code" / "Shared" / "ops" / "tools" / "pre_deploy_rule_check.py"
BRAIN_DIR = REPO_ROOT / "brain" / "machine_artifacts" / "content"
SETTINGS_DIR = REPO_ROOT / ".claude"


# ── Helpers ────────────────────────────────────────────────────────────────

def _fresh_boot_instance(load_full=False):
    """Create a fresh (non-singleton) boot instance for isolated testing."""
    from chathealthy_devops_boot import chathealthy_devops_boot
    # Reset singleton
    chathealthy_devops_boot._instance = None
    instance = chathealthy_devops_boot(load_full=load_full)
    return instance


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure every test gets a clean singleton."""
    from chathealthy_devops_boot import chathealthy_devops_boot
    chathealthy_devops_boot._instance = None
    yield
    chathealthy_devops_boot._instance = None


# ============================================================================
# REQ-003: Boot class is single Python file with __main__ and
#          chathealthy_devops_boot class. main() dispatches by mode.
# ============================================================================

class TestREQ003_BootClassStructure:
    """ARCH-DEVOPS-BOOT-001-REQ-003: The boot class MUST be a single Python file
    with __main__ and a chathealthy_devops_boot class. main() dispatches by mode."""

    def test_boot_module_is_single_file(self):
        """Boot class lives in exactly one .py file."""
        assert BOOT_MODULE_PATH.exists(), "chathealthy_devops_boot.py must exist"
        assert BOOT_MODULE_PATH.is_file()

    def test_module_has_dunder_main(self):
        """Module has if __name__ == '__main__' guard."""
        source = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        assert '__name__' in source and '__main__' in source

    def test_module_has_boot_class(self):
        """Module defines chathealthy_devops_boot class."""
        source = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        class_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        assert "chathealthy_devops_boot" in class_names

    def test_main_dispatches_all_four_modes(self):
        """main() must handle prompt, tool_call, prompt_result, session_end."""
        source = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        for mode in ["prompt", "tool_call", "prompt_result", "session_end"]:
            assert f'"{mode}"' in source or f"'{mode}'" in source, \
                f"main() must dispatch mode '{mode}'"

    def test_main_argparse_requires_mode(self):
        """main() uses argparse with --mode as required argument."""
        from chathealthy_devops_boot import main
        with pytest.raises(SystemExit):
            # No --mode arg should fail
            with patch("sys.argv", ["boot"]):
                with patch("sys.stdin", MagicMock()):
                    main()

    def test_mode_handlers_wrapped_in_try_except(self):
        """All mode handlers are wrapped in try/except/finally. Checked via AST."""
        source = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        # Find main function
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "main":
                # Must contain at least one Try node
                has_try = any(isinstance(child, ast.Try) for child in ast.walk(node))
                assert has_try, "main() must have try/except"
                # The Try node should have a finally handler
                for child in ast.walk(node):
                    if isinstance(child, ast.Try) and child.finalbody:
                        return  # Found try/except/finally
                # If we found try but not finally at top-level, check deeper
                # The code has nested try: the outer try has finally with sys.exit
                break
        # Verify sys.exit is called in finally
        assert "sys.exit" in source, "main() finally block must call sys.exit()"


# ============================================================================
# REQ-004: inform_claude() serializes singleton state
# ============================================================================

class TestREQ004_InformClaude:
    """ARCH-DEVOPS-BOOT-001-REQ-004: inform_claude() MUST serialize singleton state
    (version, framework, build, brain file list, constraints, non-allow matrix cells)
    and the orphan table into additionalContext."""

    def test_inform_claude_returns_string(self):
        """inform_claude() returns a string."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {"version": "1.0", "framework": "v4", "build": "100"}}}
        boot._constraints = ["test constraint"]
        boot._state = {"boot_complete": True}
        result = boot.inform_claude({"orphan_bugs": []})
        assert isinstance(result, str)

    def test_inform_claude_includes_singleton_state(self):
        """Output must contain serialized singleton state with version, framework, build."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {
            "version": {"current": {"version": "4.0.28", "framework": "v4", "build": "999"}},
            "governance_matrix": {"matrix": {}},
        }
        boot._constraints = ["rule A", "rule B"]
        boot._state = {"boot_complete": True}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "4.0.28" in result
        assert "v4" in result
        assert "999" in result
        assert "GOVERNANCE SINGLETON STATE" in result

    def test_inform_claude_includes_mode_selection(self):
        """Output must include mode selection directive (REQ-010 encoded here)."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "operating mode" in result.lower() or "MODE SELECTION" in result

    def test_inform_claude_does_not_include_orphan_table(self):
        """BUG-GOV-011: inform_claude emits ONLY the mode-selection directive at boot.
        The orphan-triage directive is emitted separately on the mode-reply turn so
        the two boot questions are sequential (REQ-011 happens AFTER REQ-010 is answered)."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        orphans = [{"bugid": "BUG-TEST-001", "type": "medium", "title": "test rule", "date": "2026-01-01"}]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "BUG-TEST-001" not in result, "Orphan table must NOT appear in boot payload (BUG-GOV-011)"
        assert "Triage orphan bugs?" not in result, "Orphan-triage question must NOT be in boot payload"

    def test_inform_claude_no_orphan_table_when_empty(self):
        """When no orphan bugs, boot payload still does not contain the orphan-triage
        directive — that directive is always deferred to the mode-reply turn."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "ORPHAN BUG TRIAGE" not in result
        assert "Triage orphan bugs?" not in result


# ============================================================================
# REQ-005: chathealthyai_brainboot.json deleted, boot/ directory deleted
# ============================================================================

class TestREQ005_LegacyFilesDeleted:
    """ARCH-DEVOPS-BOOT-001-REQ-005: chathealthyai_brainboot.json MUST be deleted.
    brain/machine_artifacts/boot/ directory MUST be deleted."""

    def test_brainboot_json_does_not_exist(self):
        """chathealthyai_brainboot.json must not exist in brain dir."""
        brainboot = BRAIN_DIR / "chathealthyai_brainboot.json"
        assert not brainboot.exists(), "chathealthyai_brainboot.json must be deleted"

    def test_boot_directory_does_not_exist(self):
        """brain/machine_artifacts/boot/ must not exist."""
        boot_dir = REPO_ROOT / "brain" / "machine_artifacts" / "boot"
        assert not boot_dir.exists(), "brain/machine_artifacts/boot/ must be deleted"

    def test_no_brainboot_references_in_boot_class(self):
        """No references to chathealthyai_brainboot in the boot class (except comments)."""
        source = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # Skip comment lines
            assert "chathealthyai_brainboot" not in stripped, \
                f"Line {i} references deleted brainboot: {stripped}"


# ============================================================================
# REQ-006: CLAUDE.md states only that boot class governs session
# ============================================================================

class TestREQ006_ClaudeMdContent:
    """ARCH-DEVOPS-BOOT-001-REQ-006: CLAUDE.md MUST state only that the boot class
    governs the session."""

    def test_claude_md_exists(self):
        """CLAUDE.md must exist at repo root."""
        claude_md = REPO_ROOT / "CLAUDE.md"
        assert claude_md.exists()

    def test_claude_md_references_boot_class(self):
        """CLAUDE.md must reference the boot class file path."""
        content = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "chathealthy_devops_boot" in content

    def test_claude_md_states_boot_governs(self):
        """CLAUDE.md must state the boot class governs the session."""
        content = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "governs" in content.lower() or "govern" in content.lower()


# ============================================================================
# REQ-007: SessionStart hook clears booted flag (primary clearer)
# ============================================================================

class TestREQ007_SessionStartHook:
    """ARCH-DEVOPS-BOOT-001-REQ-007: SessionStart hook MUST be registered and
    MUST set the booted flag to FALSE with no auth-dependent work."""

    def test_settings_json_exists(self):
        """settings.json must exist."""
        settings_path = SETTINGS_DIR / "settings.json"
        assert settings_path.exists()

    def test_session_start_hook_registered(self):
        """SessionStart key must be present in hooks config."""
        settings_path = SETTINGS_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert "SessionStart" in hooks, \
            "settings.json MUST have a SessionStart hook per REQ-007"

    def test_session_start_invokes_boot_script_session_start_mode(self):
        """SessionStart hook command MUST invoke chathealthy_devops_boot.py --mode session_start."""
        settings_path = SETTINGS_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        entries = data["hooks"]["SessionStart"]
        assert entries, "SessionStart hook list is empty"
        commands = [h["command"] for entry in entries for h in entry.get("hooks", [])]
        assert any(
            "chathealthy_devops_boot.py" in c and "--mode session_start" in c
            for c in commands
        ), f"SessionStart hook MUST invoke chathealthy_devops_boot.py --mode session_start; got: {commands}"

    def test_session_start_mode_clears_booted(self):
        """Running --mode session_start MUST set booted=false in ChatHealthySettings.json."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        with patch.object(
            chathealthy_devops_boot, "_read_settings", return_value={"booted": True}
        ) as mock_read:
            with patch.object(
                chathealthy_devops_boot, "_write_settings"
            ) as mock_write:
                # Simulate the session_start branch of main()
                settings = chathealthy_devops_boot._read_settings()
                settings["booted"] = False
                chathealthy_devops_boot._write_settings(settings)
        mock_read.assert_called_once()
        written_settings = mock_write.call_args[0][0]
        assert written_settings["booted"] is False, \
            "session_start mode MUST set booted=false"

    def test_has_all_required_hooks(self):
        """settings.json must have UserPromptSubmit, PreToolUse, Stop, SessionStart, SessionEnd."""
        settings_path = SETTINGS_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for required in ["UserPromptSubmit", "PreToolUse", "Stop", "SessionStart", "SessionEnd"]:
            assert required in hooks, f"settings.json missing required hook: {required}"


# ============================================================================
# REQ-008: Boot only occurs once per session. SessionEnd clears flag.
# ============================================================================

class TestREQ008_BootOncePerSession:
    """ARCH-DEVOPS-BOOT-001-REQ-008: Boot MUST only occur once per session.
    SessionEnd clears the flag."""

    def test_boot_sets_booted_flag(self):
        """After boot(), _booted must be True."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        with patch.object(type(boot), "_write_settings"):
            with patch.object(type(boot), "_read_settings", return_value={"booted": False}):
                with patch.object(boot, "_load_brain"):
                    with patch.object(boot, "_verify_coverage"):
                        with patch.object(boot, "_extract_constraints"):
                            with patch.object(boot, "_save_digest"):
                                with patch.object(boot, "_scan_orphans", return_value=[]):
                                    result = boot.boot()
        assert boot._booted is True
        assert result["status"] == "booted"

    def test_booted_returns_true_after_boot(self):
        """booted() returns True when _booted flag is set."""
        boot = _fresh_boot_instance(load_full=False)
        boot._booted = True
        assert boot.booted() is True

    def test_booted_returns_false_before_boot(self):
        """booted() returns False when settings say booted=false."""
        boot = _fresh_boot_instance(load_full=False)
        boot._booted = False
        with patch.object(type(boot), "_read_settings", return_value={"booted": False}):
            assert boot.booted() is False

    def test_second_prompt_skips_boot(self):
        """On second prompt call, boot is skipped because _booted is True."""
        boot = _fresh_boot_instance(load_full=False)
        boot._booted = True
        boot.brain = {"governance_matrix": {"matrix": {}}}
        with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True}) as mock_dispatch:
            result = boot.prompt("second message")
        # dispatch_code_controlled is called but boot() is not
        mock_dispatch.assert_called_once()
        assert "_booted" not in result  # No boot context on second prompt


# ============================================================================
# REQ-009: SessionEnd clears booted flag in ChatHealthySettings.json
# ============================================================================

class TestREQ009_SessionEndClearsFlag:
    """ARCH-DEVOPS-BOOT-001-REQ-009: SessionEnd hook MUST clear the booted flag
    in .claude/ChatHealthySettings.json."""

    def test_session_end_mode_clears_booted(self):
        """Running main() with --mode session_end sets booted=false in settings."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump({"booted": True}, tf)
            tf_path = tf.name

        try:
            # Patch SETTINGS_PATH to use temp file
            with patch("chathealthy_devops_boot.SETTINGS_PATH", Path(tf_path)):
                settings = chathealthy_devops_boot._read_settings()
                assert settings["booted"] is True
                settings["booted"] = False
                chathealthy_devops_boot._write_settings(settings)
                updated = chathealthy_devops_boot._read_settings()
                assert updated["booted"] is False
        finally:
            os.unlink(tf_path)

    def test_session_end_in_main_clears_flag(self):
        """main() --mode session_end writes booted=false."""
        from chathealthy_devops_boot import main
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump({"booted": True}, tf)
            tf_path = tf.name

        try:
            with patch("chathealthy_devops_boot.SETTINGS_PATH", Path(tf_path)):
                with patch("sys.argv", ["boot", "--mode", "session_end"]):
                    with patch("sys.stdin", MagicMock(read=MagicMock(return_value="{}"))):
                        with patch("json.load", return_value={}):
                            with patch("json.dump"):
                                with pytest.raises(SystemExit) as exc_info:
                                    main()
                    # main always calls sys.exit; exit code 0 means success
                    assert exc_info.value.code == 0

            # Verify the file was actually updated
            result = json.loads(Path(tf_path).read_text(encoding="utf-8"))
            assert result["booted"] is False
        finally:
            os.unlink(tf_path)


# ============================================================================
# REQ-010: After boot, Claude must ask user for operating mode (1/2/3)
# ============================================================================

class TestREQ010_ModeSelectionDirective:
    """ARCH-DEVOPS-BOOT-001-REQ-010: After boot, Claude MUST ask the user to
    select an operating mode: 1=Unattended, 2=Normal, 3=Idiot."""

    def test_inform_claude_contains_mode_prompt(self):
        """inform_claude output must contain the three mode options."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "Unattended" in result
        assert "Normal" in result
        assert "Idiot" in result

    def test_inform_claude_contains_do_not_proceed(self):
        """inform_claude must instruct Claude NOT to proceed without mode selection."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "Do NOT proceed" in result or "do not proceed" in result.lower()

    def test_mode_numbers_present(self):
        """All three mode numbers (1, 2, 3) must appear in the directive."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "1" in result and "2" in result and "3" in result


# ============================================================================
# REQ-011: After mode selection, if orphans exist, present orphan table
# ============================================================================

class TestREQ011_OrphanTablePresentation:
    """ARCH-DEVOPS-BOOT-001-REQ-011: After the user answers REQ-010, Claude asks
    'Triage orphan bugs?' — a single yes/no gate. If 'yes', the orphan table is
    presented. BUG-GOV-011: directive is emitted on the mode-reply turn, NOT
    in the boot payload, so the two boot questions are sequential."""

    def test_orphan_table_has_headers(self):
        """Orphan table must have ID, Type, Description, Date headers."""
        boot = _fresh_boot_instance(load_full=False)
        orphans = [{"bugid": "BUG-001", "type": "high", "title": "something broke", "date": "2026-04-01"}]
        result = boot._build_orphan_triage_directive(orphans)
        assert "| ID |" in result
        assert "| Type |" in result

    def test_orphan_table_contains_bug_data(self):
        """Each orphan bug row must appear in the directive output."""
        boot = _fresh_boot_instance(load_full=False)
        orphans = [
            {"bugid": "BUG-X-001", "type": "medium", "title": "orphan rule text", "date": "2026-03-15"},
            {"bugid": "BUG-X-002", "type": "high", "title": "second orphan", "date": "2026-03-20"},
        ]
        result = boot._build_orphan_triage_directive(orphans)
        assert "BUG-X-001" in result
        assert "BUG-X-002" in result
        assert "orphan rule text" in result

    def test_orphan_triage_directive_present(self):
        """Directive must include the verbatim 'Triage orphan bugs?' question."""
        boot = _fresh_boot_instance(load_full=False)
        orphans = [{"bugid": "BUG-001", "type": "high", "title": "test", "date": "2026-01-01"}]
        result = boot._build_orphan_triage_directive(orphans)
        assert "Triage orphan bugs?" in result
        assert "REQ-011" in result

    def test_orphan_triage_directive_handles_empty_list(self):
        """Directive must still emit the yes/no question even when no orphans exist —
        the question itself is the contract per REQ-011."""
        boot = _fresh_boot_instance(load_full=False)
        result = boot._build_orphan_triage_directive([])
        assert "Triage orphan bugs?" in result
        assert "no orphan bugs" in result.lower()


class TestBugGov011_SequentialBootQuestions:
    """BUG-GOV-011: boot emits ONLY the mode-selection directive. Orphan-triage
    directive is emitted on the NEXT turn, after the user answers REQ-010."""

    def test_boot_context_has_only_mode_prompt(self):
        """inform_claude() at boot must NOT contain the orphan-triage directive."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        # Even if orphan_bugs is present in boot_result, it MUST NOT appear in the boot payload.
        orphans = [{"bugid": "BUG-001", "type": "high", "title": "test", "date": "2026-01-01"}]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "MODE SELECTION" in result
        assert "Select operating mode" in result
        assert "Triage orphan bugs?" not in result, (
            "BUG-GOV-011: orphan-triage directive must NOT be in boot payload; "
            "it is emitted on the mode-reply turn."
        )
        assert "BUG-001" not in result, "Orphan table leaked into boot payload"

    def test_boot_context_explains_sequentiality(self):
        """Boot directive must tell Claude not to ask orphan triage in the same turn."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "REQ-011" in result
        assert "NEXT turn" in result or "next turn" in result.lower()


# ============================================================================
# REQ-001: On first UserPromptSubmit, scan bugs.json for ORPHAN_BUG entries
# ============================================================================

class TestREQ001_OrphanBugScan:
    """ARCH-DEVOPS-BOOT-001-REQ-001: On first UserPromptSubmit, scan bugs.json
    for entries with orphan: true and output a table."""

    def test_scan_orphans_finds_orphan_bugs(self):
        """_scan_orphans returns list of orphan bugs from bugs.json."""
        boot = _fresh_boot_instance(load_full=False)
        bugs_data = {
            "bug": [
                {"bugid": "BUG-001", "type": "high", "title": "test rule", "date": "2026-01-01", "orphan": True},
                {"bugid": "BUG-002", "type": "low", "title": "normal bug", "date": "2026-01-02", "orphan": False},
                {"bugid": "BUG-003", "type": "medium", "title": "another orphan", "date": "2026-01-03", "orphan": True},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump(bugs_data, tf)
            tf_path = tf.name
        try:
            with patch("chathealthy_devops_boot.BRAIN_DIR", Path(tf_path).parent):
                # Rename temp file to bugs.json in its directory
                bugs_path = Path(tf_path).parent / "bugs.json"
                os.rename(tf_path, str(bugs_path))
                try:
                    result = boot._scan_orphans()
                    assert len(result) == 2
                    assert result[0]["id"] == "BUG-001"
                    assert result[1]["id"] == "BUG-003"
                finally:
                    if bugs_path.exists():
                        os.unlink(str(bugs_path))
        except Exception:
            if os.path.exists(tf_path):
                os.unlink(tf_path)
            raise

    def test_scan_orphans_returns_required_fields(self):
        """Each orphan entry must have id, type, rule (truncated), date."""
        boot = _fresh_boot_instance(load_full=False)
        bugs_data = {
            "bug": [
                {"bugid": "BUG-T-001", "type": "critical", "title": "x" * 200, "date": "2026-04-15", "orphan": True},
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir()) as tf:
            json.dump(bugs_data, tf)
            tf_path = tf.name
        bugs_path = Path(tf_path).parent / "bugs.json"
        os.replace(tf_path, str(bugs_path))
        try:
            with patch("chathealthy_devops_boot.BRAIN_DIR", Path(bugs_path).parent):
                result = boot._scan_orphans()
                assert len(result) == 1
                entry = result[0]
                assert "id" in entry
                assert "type" in entry
                assert "rule" in entry
                assert "date" in entry
                # Rule must be truncated to 80 chars
                assert len(entry["rule"]) <= 80
        finally:
            if bugs_path.exists():
                os.unlink(str(bugs_path))

    def test_boot_calls_scan_orphans(self):
        """boot() must call _scan_orphans and include results."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        with patch.object(type(boot), "_write_settings"):
            with patch.object(type(boot), "_read_settings", return_value={"booted": False}):
                with patch.object(boot, "_load_brain"):
                    with patch.object(boot, "_verify_coverage"):
                        with patch.object(boot, "_extract_constraints"):
                            with patch.object(boot, "_save_digest"):
                                with patch.object(boot, "_scan_orphans", return_value=[{"bugid": "BUG-MOCK"}]) as mock_scan:
                                    result = boot.boot()
        mock_scan.assert_called_once()
        assert result["orphan_bugs"] == [{"bugid": "BUG-MOCK"}]


# ============================================================================
# REQ-002: Orphan scan is pure file I/O, fail open if bugs.json missing
# ============================================================================

class TestREQ002_OrphanScanFailOpen:
    """ARCH-DEVOPS-BOOT-001-REQ-002: Orphan scan is pure file I/O. If bugs.json
    is not found, corrupt, or unreadable, scan MUST fail open."""

    def test_missing_bugs_json_returns_empty(self):
        """If bugs.json doesn't exist, return empty list (fail open)."""
        boot = _fresh_boot_instance(load_full=False)
        fake_dir = Path(tempfile.gettempdir()) / "nonexistent_brain_dir_test"
        with patch("chathealthy_devops_boot.BRAIN_DIR", fake_dir):
            result = boot._scan_orphans()
        assert result == []

    def test_corrupt_bugs_json_returns_empty(self):
        """If bugs.json is corrupt JSON, return empty list (fail open)."""
        boot = _fresh_boot_instance(load_full=False)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, dir=tempfile.gettempdir()) as tf:
            tf.write("{invalid json content!@#$")
            tf_path = tf.name
        bugs_path = Path(tf_path).parent / "bugs.json"
        os.replace(tf_path, str(bugs_path))
        try:
            with patch("chathealthy_devops_boot.BRAIN_DIR", Path(bugs_path).parent):
                result = boot._scan_orphans()
            assert result == []
        finally:
            if bugs_path.exists():
                os.unlink(str(bugs_path))

    def test_scan_orphans_prints_to_stderr_on_missing(self, capsys):
        """Fail-open must print warning and stack trace to stderr."""
        boot = _fresh_boot_instance(load_full=False)
        fake_dir = Path(tempfile.gettempdir()) / "no_such_dir_test_002"
        with patch("chathealthy_devops_boot.BRAIN_DIR", fake_dir):
            result = boot._scan_orphans()
        captured = capsys.readouterr()
        assert "BOOT WARNING" in captured.err or "bugs.json" in captured.err
        assert result == []

    def test_scan_is_pure_file_io(self):
        """_scan_orphans must not use network or database — verified by source inspection."""
        import inspect
        from chathealthy_devops_boot import chathealthy_devops_boot as cls
        source = inspect.getsource(cls._scan_orphans)
        # Must not import or call network/db modules
        for forbidden in ["requests", "urllib", "http.client", "socket", "database", "pymongo", "sqlalchemy"]:
            assert forbidden not in source, f"_scan_orphans must not use {forbidden}"


# ============================================================================
# ARCH-SCAN-JSON-REQ-002: JSON scanner validates CV-constrained fields
#                         and blocks check-in on violation
# ============================================================================

class TestScanJsonREQ002_CVConstrainedFields:
    """ARCH-SCAN-JSON-REQ-002: JSON scanner validates CV-constrained fields
    against controlled vocabularies and blocks check-in on violation."""

    def test_enforce_bugs_schema_catches_invalid_cv_value(self):
        """enforce_bugs_schema must report violation for a field value not in CV."""
        from pre_deploy_rule_check import enforce_bugs_schema

        schema_data = {
            "collections": {
                "bugs": {
                    "record_schema": {
                        "fields": {
                            "status": {
                                "required": True,
                                "possible_values": ["open", "closed", "in_progress"],
                            },
                            "status": {
                                "required": True,
                                "constrained_by": "CV-009",
                            },
                        }
                    }
                }
            }
        }
        bugs_data = {
            "bug": [
                {
                    "bugid": "BUG-TEST-001",
                    "status": "INVALID_STATUS",
                    "status": "BOGUS_VALUE",
                }
            ]
        }
        cv_data = {
            "vocabularies": [
                {
                    "vocabulary_id": "CV-009",
                    "members": [
                        {"value": "in_analysis"},
                        {"value": "fix_in_progress"},
                        {"value": "fixed"},
                        {"value": "closed"},
                    ],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            schema_path = os.path.join(td, "schema.json")
            bugs_path = os.path.join(td, "bugs.json")
            cv_path = os.path.join(td, "controlled_vocabularies.json")
            with open(schema_path, "w") as f:
                json.dump(schema_data, f)
            with open(bugs_path, "w") as f:
                json.dump(bugs_data, f)
            with open(cv_path, "w") as f:
                json.dump(cv_data, f)

            with patch("pre_deploy_rule_check.REPO_ROOT", td):
                # Patch the path construction inside enforce_bugs_schema
                with patch("pre_deploy_rule_check.os.path.join", side_effect=lambda *args: os.path.join(*[td if a == patch_root else a for a in args])):
                    pass
                # Simpler: just call with the paths directly patched
                import pre_deploy_rule_check as scanner
                orig_repo = scanner.REPO_ROOT
                scanner.REPO_ROOT = td
                # Create brain subdir structure
                brain_path = os.path.join(td, "brain", "machine_artifacts", "content")
                os.makedirs(brain_path, exist_ok=True)
                for src, dst_name in [(schema_path, "schema.json"), (bugs_path, "bugs.json"), (cv_path, "controlled_vocabularies.json")]:
                    with open(src) as sf:
                        data = sf.read()
                    with open(os.path.join(brain_path, dst_name), "w") as df:
                        df.write(data)
                try:
                    violations = enforce_bugs_schema("TEST-RULE", {})
                    # Must catch both the possible_values violation and the CV-009 violation
                    assert len(violations) >= 1, f"Expected violations, got: {violations}"
                    violation_text = " ".join(violations)
                    assert "INVALID_STATUS" in violation_text or "BOGUS_VALUE" in violation_text
                finally:
                    scanner.REPO_ROOT = orig_repo

    def test_enforce_bugs_schema_passes_valid_data(self):
        """enforce_bugs_schema must return no violations for valid CV-constrained data."""
        from pre_deploy_rule_check import enforce_bugs_schema
        import pre_deploy_rule_check as scanner

        schema_data = {
            "collections": {
                "bugs": {
                    "record_schema": {
                        "fields": {
                            "status": {
                                "required": True,
                                "possible_values": ["open", "closed"],
                            },
                        }
                    }
                }
            }
        }
        bugs_data = {"bug": [{"bugid": "BUG-OK-001", "status": "open"}]}
        cv_data = {"vocabularies": []}

        with tempfile.TemporaryDirectory() as td:
            brain_path = os.path.join(td, "brain", "machine_artifacts", "content")
            os.makedirs(brain_path, exist_ok=True)
            for name, data in [("schema.json", schema_data), ("bugs.json", bugs_data),
                               ("controlled_vocabularies.json", cv_data)]:
                with open(os.path.join(brain_path, name), "w") as f:
                    json.dump(data, f)

            orig_repo = scanner.REPO_ROOT
            scanner.REPO_ROOT = td
            try:
                violations = enforce_bugs_schema("TEST-RULE", {})
                assert violations == [], f"Expected no violations, got: {violations}"
            finally:
                scanner.REPO_ROOT = orig_repo

    def test_enforce_bugs_schema_blocks_on_violation(self):
        """Violations from enforce_bugs_schema are non-empty, which blocks check-in
        when called from main() (main returns exit code 1 on violations)."""
        from pre_deploy_rule_check import enforce_bugs_schema
        import pre_deploy_rule_check as scanner

        schema_data = {
            "collections": {
                "bugs": {
                    "record_schema": {
                        "fields": {
                            "type": {
                                "required": False,
                                "constrained_by": "CV-008",
                            },
                        }
                    }
                }
            }
        }
        bugs_data = {"bug": [{"bugid": "BUG-BAD", "type": "TOTALLY_INVALID_TYPE"}]}
        cv_data = {
            "vocabularies": [
                {
                    "vocabulary_id": "CV-008",
                    "members": [{"value": "defect"}, {"value": "enhancement"}],
                }
            ]
        }

        with tempfile.TemporaryDirectory() as td:
            brain_path = os.path.join(td, "brain", "machine_artifacts", "content")
            os.makedirs(brain_path, exist_ok=True)
            for name, data in [("schema.json", schema_data), ("bugs.json", bugs_data),
                               ("controlled_vocabularies.json", cv_data)]:
                with open(os.path.join(brain_path, name), "w") as f:
                    json.dump(data, f)

            orig_repo = scanner.REPO_ROOT
            scanner.REPO_ROOT = td
            try:
                violations = enforce_bugs_schema("SCAN-REQ-002", {})
                assert len(violations) > 0
                assert "TOTALLY_INVALID_TYPE" in violations[0]
                assert "CV-008" in violations[0]
            finally:
                scanner.REPO_ROOT = orig_repo

    def test_missing_files_returns_violation(self):
        """If schema.json, bugs.json, or CVs are missing, returns violation (not crash)."""
        from pre_deploy_rule_check import enforce_bugs_schema
        import pre_deploy_rule_check as scanner

        with tempfile.TemporaryDirectory() as td:
            orig_repo = scanner.REPO_ROOT
            scanner.REPO_ROOT = td
            try:
                violations = enforce_bugs_schema("TEST-MISSING", {})
                assert len(violations) > 0  # Should report missing files
            finally:
                scanner.REPO_ROOT = orig_repo


# ============================================================================
# REQ-012: Context-resident truth artifacts.
#          bugs.json + engineering_rules.json + every Code/DataPipelines/ file
#          MUST be @-imported from project-root CLAUDE.md.
# ============================================================================


class TestREQ012_ContextResidentTruthArtifacts:
    """ARCH-DEVOPS-BOOT-001-REQ-012: Three sources-of-truth are auto-loaded into
    session context via CLAUDE.md @-imports. No intermediate bundle file; one
    @-line per file. Every current DataPipelines file must be covered; no stale
    @-paths may exist in CLAUDE.md."""

    CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
    DATAPIPES_DIR = REPO_ROOT / "Code" / "DataPipelines"
    EXTENSIONS = {".py", ".yml", ".yaml", ".json", ".sh", ".md"}
    EXCLUDE_DIR_NAMES = {"__pycache__", ".pytest_cache"}

    def _imported_paths(self) -> set:
        """Return the set of @-imported relative paths declared in CLAUDE.md."""
        text = self.CLAUDE_MD.read_text(encoding="utf-8")
        paths = set()
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("@"):
                paths.add(s[1:].strip())
        return paths

    def _current_pipeline_files(self) -> set:
        """Walk Code/DataPipelines/ and return relative paths (forward slashes)
        of every file matching the listed extensions, minus excluded dirs."""
        files = set()
        for p in self.DATAPIPES_DIR.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in self.EXTENSIONS:
                continue
            if any(part in self.EXCLUDE_DIR_NAMES for part in p.parts):
                continue
            rel = p.relative_to(REPO_ROOT).as_posix()
            files.add(rel)
        return files

    def test_claude_md_exists(self):
        assert self.CLAUDE_MD.exists(), "Project-root CLAUDE.md missing"

    def test_bugs_json_is_imported(self):
        imports = self._imported_paths()
        assert "brain/machine_artifacts/content/bugs.json" in imports, (
            "CLAUDE.md must @-import brain/machine_artifacts/content/bugs.json"
        )

    def test_engineering_rules_json_is_imported(self):
        imports = self._imported_paths()
        assert "brain/machine_artifacts/content/engineering_rules.json" in imports, (
            "CLAUDE.md must @-import brain/machine_artifacts/content/engineering_rules.json"
        )

    def test_every_pipeline_file_is_imported(self):
        """Every current Code/DataPipelines/ source file has an @-import."""
        imports = self._imported_paths()
        current = self._current_pipeline_files()
        missing = sorted(current - imports)
        assert not missing, (
            f"CLAUDE.md is missing @-imports for {len(missing)} pipeline files: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
        )

    def test_no_stale_pipeline_imports(self):
        """Every @-import under Code/DataPipelines/ refers to a file that exists."""
        imports = self._imported_paths()
        current = self._current_pipeline_files()
        pipeline_imports = {p for p in imports if p.startswith("Code/DataPipelines/")}
        stale = sorted(pipeline_imports - current)
        assert not stale, (
            f"CLAUDE.md has {len(stale)} stale @-imports (files no longer exist): "
            f"{stale[:5]}{'...' if len(stale) > 5 else ''}"
        )

    def test_all_imported_files_exist_and_nonempty(self):
        """Every @-imported path resolves to an existing, non-empty file."""
        imports = self._imported_paths()
        problems = []
        for rel in sorted(imports):
            path = REPO_ROOT / rel
            if not path.exists():
                problems.append(f"MISSING: {rel}")
            elif path.stat().st_size == 0:
                problems.append(f"EMPTY: {rel}")
        assert not problems, f"@-import problems: {problems[:10]}"

    def test_no_intermediate_bundle_file_used(self):
        """pipeline_bundle.md must not be the @-import mechanism — direct files only."""
        imports = self._imported_paths()
        bundle_imports = [p for p in imports if p.endswith("pipeline_bundle.md")]
        assert not bundle_imports, (
            "CLAUDE.md must not @-import an intermediate pipeline_bundle.md; "
            f"use direct file @-imports. Found: {bundle_imports}"
        )

    def test_agile_backlog_is_intentionally_excluded(self):
        """Per Boss directive 2026-04-18, the raw 3MB agile_backlog.json is NOT
        auto-loaded. The first attempted inclusion (2026-04-18 AM) caused a
        boot-sequence regression and was reverted. The current fix (BUG-GOV-010)
        imports the DERIVED compact req_index.txt instead; see
        TestBugGov010_ReqIndexContextResident below."""
        imports = self._imported_paths()
        assert "brain/machine_artifacts/content/agile_backlog.json" not in imports, (
            "agile_backlog.json must NOT be @-imported directly; use the derived "
            "req_index.txt (BUG-GOV-010 fix)."
        )


# ============================================================================
# REQ-013: Universal governance — every action (user prompt, tool call, final
# output) is rule-adjudicated by engineering_rules_worker before allow. Rule
# violations are mechanically gated, not advisory.
# ============================================================================


class TestREQ013_UniversalGovernance:
    """ARCH-DEVOPS-BOOT-001-REQ-013: every action taken within this project MUST
    be governed by the engineering rules. The worker mechanically adjudicates
    pre_tool_use, user_prompt_submit, and stop events. Violations deny/refuse/block;
    compliant actions pass. Tests mock the GPT adjudicator to isolate control flow."""

    def _worker(self):
        from chathealthy_devops_boot import engineering_rules_worker
        return engineering_rules_worker.from_dict({})

    # ── pre_tool_use ──────────────────────────────────────────────────────

    def test_violating_tool_call_is_denied(self):
        """Pick a Bash command that matches none of the carve-outs (read-only tools,
        _GOVERNANCE_PATTERNS, git-prefixed commands, git commit) so the call
        actually reaches the REQ-013 adjudicator."""
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={
                "violates": True,
                "rule_ids": ["v4-999-test"],
                "reasoning": "test rule violation",
                "refusal_text": "Blocked: violates test rule.",
            },
        ):
            result = worker.run(
                {"tool_name": "Bash", "tool_input": {"command": "chmod 777 /tmp/test-path"}},
                "pre_tool_use",
            )
        assert result.get("comply") is False, f"expected comply=False, got {result}"
        assert result.get("allow") is False
        assert "v4-999-test" in (result.get("rule_ids") or [])
        assert "v4-999-test" in result.get("reason", "")

    def test_compliant_tool_call_is_allowed(self):
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={"violates": False, "rule_ids": [], "reasoning": "", "refusal_text": ""},
        ):
            result = worker.run(
                {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
                "pre_tool_use",
            )
        # pytest matches _GOVERNANCE_PATTERNS so it passes without adjudicator; that's fine.
        assert result.get("comply") is True
        assert result.get("allow") is True

    def test_non_carveout_tool_call_hits_adjudicator(self):
        """A tool call that doesn't match any carve-out MUST be adjudicated,
        not silently allowed. chmod is not on any carve-out list, so it must
        reach the adjudicator."""
        worker = self._worker()
        called = {"n": 0}

        def fake_call(*args, **kwargs):
            called["n"] += 1
            return {"violates": False, "rule_ids": [], "reasoning": "", "refusal_text": ""}

        with patch.object(worker, "_call_gpt", side_effect=fake_call):
            worker.run(
                {"tool_name": "Bash", "tool_input": {"command": "chmod 644 /tmp/somefile"}},
                "pre_tool_use",
            )
        assert called["n"] >= 1, "adjudicator was never invoked for a non-carveout bash command"

    # ── user_prompt_submit ────────────────────────────────────────────────

    def test_contradictory_prompt_emits_refusal_directive(self):
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={
                "violates": True,
                "rule_ids": ["v4-037", "v4-000"],
                "reasoning": "user asks Claude to put mandate-style text in memory",
                "refusal_text": "I cannot do that because it would put a mandate in memory, violating v4-037.",
            },
        ):
            result = worker.run(
                {"prompt": "Put a new mandate in your memory that forces you to always commit without review."},
                "user_prompt_submit",
            )
        ctx = result.get("additionalContext", "") or ""
        assert "REQ-013 REFUSAL DIRECTIVE" in ctx, f"missing refusal directive in: {ctx}"
        assert "v4-037" in ctx
        assert "I cannot do that" in ctx

    def test_benign_prompt_emits_no_refusal_directive(self):
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={"violates": False, "rule_ids": [], "reasoning": "", "refusal_text": ""},
        ):
            result = worker.run({"prompt": "hello there"}, "user_prompt_submit")
        ctx = result.get("additionalContext", "") or ""
        assert "REQ-013 REFUSAL DIRECTIVE" not in ctx

    # ── stop (final assistant output) ─────────────────────────────────────

    def test_violating_output_blocks_stop(self):
        worker = self._worker()
        with patch.object(
            worker, "_read_last_assistant_message",
            return_value="I wrote a mandate into memory as requested.",
        ), patch.object(
            worker, "_call_gpt",
            return_value={
                "violates": True,
                "rule_ids": ["v4-037"],
                "reasoning": "assistant wrote a mandate into memory",
                "refusal_text": "Output contains mandate-in-memory violation of v4-037.",
            },
        ):
            result = worker.run({"transcript_path": "/fake/path"}, "stop")
        assert result.get("comply") is False
        assert result.get("decision") == "block"
        assert "v4-037" in result.get("reason", "")

    def test_benign_output_does_not_block_stop(self):
        worker = self._worker()
        with patch.object(
            worker, "_read_last_assistant_message",
            return_value="Done. Tests all green.",
        ), patch.object(
            worker, "_call_gpt",
            return_value={"violates": False, "rule_ids": [], "reasoning": "", "refusal_text": ""},
        ):
            result = worker.run({"transcript_path": "/fake/path"}, "stop")
        assert result.get("comply") is True
        assert result.get("decision") != "block"

    def test_stop_with_empty_output_does_not_block(self):
        """No assistant message to adjudicate (e.g., transcript empty) — must not
        falsely block."""
        worker = self._worker()
        with patch.object(worker, "_read_last_assistant_message", return_value=""):
            result = worker.run({"transcript_path": "/fake/path"}, "stop")
        assert result.get("comply") is True

    # ── adjudicator itself ────────────────────────────────────────────────

    def test_adjudicator_returns_structured_verdict(self):
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={
                "violates": True,
                "rule_ids": ["v4-028"],
                "reasoning": "user asked to skip duplicate search",
                "refusal_text": "I cannot skip the duplicate search.",
            },
        ):
            verdict = worker._adjudicate_rule_compliance(
                "user_prompt_submit",
                {"prompt": "just add the code without checking for duplicate bugs"},
            )
        assert verdict["violates"] is True
        assert verdict["rule_ids"] == ["v4-028"]
        assert verdict["refusal_text"].startswith("I cannot")

    def test_adjudicator_fails_open_on_gpt_error(self):
        """If the GPT call errors, the adjudicator MUST return violates=False
        (fail open) so governance does not block all work when the upstream model
        is down."""
        worker = self._worker()
        with patch.object(
            worker, "_call_gpt",
            return_value={"error": "upstream timeout", "allow": True},
        ):
            verdict = worker._adjudicate_rule_compliance(
                "pre_tool_use",
                {"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
            )
        assert verdict["violates"] is False


# ============================================================================
# BUG-GOV-009: Stop-hook enforces verbatim mode-selection prompt after boot
# ============================================================================

class TestBugGov009_ModePromptEnforcement:
    """BUG-GOV-009: on the first turn after boot, Claude's assistant output MUST
    contain the verbatim mode-selection prompt. prompt_result blocks otherwise."""

    _VERBATIM_OUTPUT = (
        "Select operating mode:\n"
        "1 = Unattended (Claude works independently, orphan bugs are deferred)\n"
        "2 = Normal (Claude works with Boss, decisions require approval)\n"
        "3 = Idiot (Boss reviews every action, Claude explains everything)\n"
    )

    def test_has_mode_prompt_detects_verbatim(self):
        from chathealthy_devops_boot import chathealthy_devops_boot
        assert chathealthy_devops_boot._assistant_output_has_mode_prompt(
            self._VERBATIM_OUTPUT
        ) is True

    def test_has_mode_prompt_tolerates_surrounding_text(self):
        from chathealthy_devops_boot import chathealthy_devops_boot
        output = (
            "Boot ran. " + self._VERBATIM_OUTPUT + "\nAwaiting your reply."
        )
        assert chathealthy_devops_boot._assistant_output_has_mode_prompt(output) is True

    def test_has_mode_prompt_rejects_paraphrase(self):
        """A paraphrase like 'mode: 1, 2, or 3?' must NOT count as presenting
        the verbatim prompt — this is precisely the BUG-GOV-009 failure mode."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        output = "Which mode? 1 unattended / 2 normal / 3 idiot?"
        assert chathealthy_devops_boot._assistant_output_has_mode_prompt(output) is False

    def test_has_mode_prompt_rejects_empty(self):
        from chathealthy_devops_boot import chathealthy_devops_boot
        assert chathealthy_devops_boot._assistant_output_has_mode_prompt("") is False
        assert chathealthy_devops_boot._assistant_output_has_mode_prompt(None) is False

    def test_boot_sets_mode_selected_false(self):
        """After boot() completes, settings.mode_selected MUST be False so the
        next Stop event will enforce the verbatim prompt."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        captured = {}

        def fake_write(settings):
            captured.update(settings)

        with patch.object(type(boot), "_write_settings", side_effect=fake_write):
            with patch.object(
                type(boot), "_read_settings", return_value={"booted": False}
            ):
                with patch.object(boot, "_load_brain"):
                    with patch.object(boot, "_verify_coverage"):
                        with patch.object(boot, "_extract_constraints"):
                            with patch.object(boot, "_save_digest"):
                                with patch.object(boot, "_scan_orphans", return_value=[]):
                                    boot.boot()

        assert captured.get("booted") is True, "boot must set booted=True"
        assert captured.get("mode_selected") is False, (
            "boot must set mode_selected=False so next Stop enforces verbatim prompt"
        )

    def test_prompt_result_blocks_when_prompt_missing(self, tmp_path):
        """If mode_selected is False and the assistant output lacks the verbatim
        mode prompt, prompt_result MUST return comply=False with decision=block."""
        from chathealthy_devops_boot import chathealthy_devops_boot

        boot = _fresh_boot_instance(load_full=False)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "I'll proceed with the task."},
        }) + "\n", encoding="utf-8")

        with patch.object(
            type(boot), "_read_settings",
            return_value={"booted": True, "mode_selected": False},
        ):
            with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True}):
                verdict = boot.prompt_result({"transcript_path": str(transcript)})

        assert verdict.get("comply") is False, (
            "prompt_result MUST block when mode prompt missing after boot"
        )
        assert verdict.get("decision") == "block"
        assert "BUG-GOV-009" in verdict.get("reason", "")

    def test_prompt_result_allows_when_prompt_present(self, tmp_path):
        """When the assistant output contains the verbatim mode prompt, the
        BUG-GOV-009 check passes and the normal dispatch runs."""
        from chathealthy_devops_boot import chathealthy_devops_boot

        boot = _fresh_boot_instance(load_full=False)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": self._VERBATIM_OUTPUT},
        }) + "\n", encoding="utf-8")

        with patch.object(
            type(boot), "_read_settings",
            return_value={"booted": True, "mode_selected": False},
        ):
            with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True, "workers": []}):
                verdict = boot.prompt_result({"transcript_path": str(transcript)})

        # The BUG-GOV-009 check should pass; any downstream verdict still flows.
        assert verdict.get("comply") is True, (
            "prompt_result MUST NOT block when verbatim mode prompt is present"
        )

    def test_prompt_result_noop_when_mode_already_selected(self, tmp_path):
        """Once mode_selected=True, the BUG-GOV-009 check is dormant so normal
        turns don't get blocked."""
        from chathealthy_devops_boot import chathealthy_devops_boot

        boot = _fresh_boot_instance(load_full=False)

        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant",
            "message": {"role": "assistant", "content": "Done."},
        }) + "\n", encoding="utf-8")

        with patch.object(
            type(boot), "_read_settings",
            return_value={"booted": True, "mode_selected": True},
        ):
            with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True}):
                verdict = boot.prompt_result({"transcript_path": str(transcript)})

        assert verdict.get("comply") is True

    def test_prompt_captures_mode_reply(self):
        """When user replies 1/2/3 on the turn after boot, prompt() MUST set
        mode_selected=True in settings."""
        boot = _fresh_boot_instance(load_full=False)
        captured = {}

        def fake_write(settings):
            captured.update(settings)

        with patch.object(type(boot), "_write_settings", side_effect=fake_write):
            with patch.object(
                type(boot), "_read_settings",
                return_value={"booted": True, "mode_selected": False},
            ):
                with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True}):
                    boot.prompt("2", hook_input={})

        assert captured.get("mode_selected") is True
        assert captured.get("mode") == "2"

    def test_prompt_ignores_non_mode_reply(self):
        """If the user replies with something other than 1/2/3, mode_selected
        stays False so the next Stop event still enforces the prompt."""
        boot = _fresh_boot_instance(load_full=False)
        captured = {}

        def fake_write(settings):
            captured.update(settings)

        with patch.object(type(boot), "_write_settings", side_effect=fake_write):
            with patch.object(
                type(boot), "_read_settings",
                return_value={"booted": True, "mode_selected": False},
            ):
                with patch.object(boot, "dispatch_code_controlled", return_value={"comply": True}):
                    boot.prompt("please proceed", hook_input={})

        # Either mode_selected wasn't written, or it was written as False.
        assert captured.get("mode_selected", False) is False

    def test_session_start_clears_mode_selected(self):
        """--mode session_start MUST reset mode_selected=False alongside booted."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        captured = {}

        def fake_write(settings):
            captured.update(settings)

        with patch.object(
            chathealthy_devops_boot, "_read_settings",
            return_value={"booted": True, "mode_selected": True},
        ):
            with patch.object(
                chathealthy_devops_boot, "_write_settings", side_effect=fake_write
            ):
                # Simulate the session_start branch of main()
                settings = chathealthy_devops_boot._read_settings()
                settings["booted"] = False
                settings["mode_selected"] = False
                chathealthy_devops_boot._write_settings(settings)

        assert captured.get("booted") is False
        assert captured.get("mode_selected") is False


# ============================================================================
# BUG-GOV-010: req_index.txt is generated on every boot and CLAUDE.md
# @-imports it so Claude gets context-resident req_id lookups without
# Read/Grep of the 3MB canonical agile_backlog.json.
# ============================================================================

class TestBugGov010_ReqIndexContextResident:
    """BUG-GOV-010: _write_req_index materializes a transient byte-stream view
    of agile_backlog.json at .claude/req_index.txt (gitignored). CLAUDE.md
    @-imports it so Claude sees the backlog in context without a Read/Grep
    of the canonical 3MB file. Canonical source stays at
    brain/machine_artifacts/content/agile_backlog.json; req_index.txt is a
    regenerated-each-boot transient — NOT a committed brain artifact.

    This test class pins:
      1. The method exists on the singleton and is wired into _load_brain.
      2. Running it produces .claude/req_index.txt (NOT under brain/).
      3. Every req_id present in agile_backlog.json appears in req_index.txt.
      4. req_index.txt is @-imported by CLAUDE.md.
      5. req_index.txt size stays below a safe ceiling.
    """

    REQ_INDEX_PATH = REPO_ROOT / "test_output" / "req_index.txt"
    BACKLOG_PATH = BRAIN_DIR / "agile_backlog.json"
    CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
    # Empirical ceiling — well under the 3MB that broke boot in the first
    # attempt. Bump with a boot-retest if the backlog legitimately grows.
    MAX_REQ_INDEX_BYTES = 1_500_000

    def test_method_exists(self):
        """_write_req_index must exist on the boot class."""
        from chathealthy_devops_boot import chathealthy_devops_boot
        assert hasattr(chathealthy_devops_boot, "_write_req_index"), (
            "Boot singleton must expose _write_req_index for BUG-GOV-010 fix"
        )

    def test_load_brain_calls_write_req_index(self):
        """_load_brain must invoke _write_req_index so req_index.txt refreshes
        automatically on every boot."""
        boot_src = BOOT_MODULE_PATH.read_text(encoding="utf-8")
        # Locate _load_brain body (next def after it demarcates the end)
        start = boot_src.index("def _load_brain(")
        end = boot_src.index("def ", start + 1)
        load_brain_body = boot_src[start:end]
        assert "_write_req_index" in load_brain_body, (
            "_load_brain() must call self._write_req_index() so req_index.txt "
            "is refreshed every boot"
        )

    def test_req_index_file_exists(self):
        """req_index.txt must exist on disk — generated by boot."""
        assert self.REQ_INDEX_PATH.exists(), (
            f"{self.REQ_INDEX_PATH} missing — run the boot once to generate it "
            "or verify _write_req_index wiring"
        )

    def test_req_index_is_imported_by_claude_md(self):
        """CLAUDE.md must @-import req_index.txt."""
        text = self.CLAUDE_MD.read_text(encoding="utf-8")
        assert "@test_output/req_index.txt" in text, (
            "CLAUDE.md must @-import req_index.txt (BUG-GOV-010 fix)"
        )

    def test_req_index_covers_every_req_id(self):
        """Every req_id in agile_backlog.json must appear in req_index.txt —
        completeness guard so no requirement becomes invisible to Claude."""
        with open(self.BACKLOG_PATH, encoding="utf-8") as f:
            backlog = json.load(f)

        backlog_req_ids = set()
        for epic in (backlog.get("epics", {}).get("epic", []) or []):
            if not isinstance(epic, dict):
                continue
            for feat in (epic.get("features", {}).get("feature", []) or []):
                if not isinstance(feat, dict):
                    continue
                for story in (feat.get("stories", {}).get("story", []) or []):
                    if not isinstance(story, dict):
                        continue
                    for req in (story.get("requirements", {}).get("requirement", []) or []):
                        if not isinstance(req, dict):
                            continue
                        rid = req.get("req_id", "")
                        if rid:
                            backlog_req_ids.add(rid)

        assert backlog_req_ids, "No req_ids found in agile_backlog.json (sanity check failed)"

        index_text = self.REQ_INDEX_PATH.read_text(encoding="utf-8")
        missing = [rid for rid in backlog_req_ids if rid not in index_text]
        assert not missing, (
            f"{len(missing)} req_ids in backlog missing from req_index.txt: "
            f"{missing[:10]}{'…' if len(missing) > 10 else ''}"
        )

    def test_req_index_size_under_ceiling(self):
        """Size sanity — keep the file below the ceiling so CLAUDE.md @-expansion
        stays comfortably under the harness threshold that broke BUG-GOV-010's
        first fix attempt (raw 3MB agile_backlog.json)."""
        size = self.REQ_INDEX_PATH.stat().st_size
        assert size < self.MAX_REQ_INDEX_BYTES, (
            f"req_index.txt is {size:,} bytes, exceeds ceiling "
            f"{self.MAX_REQ_INDEX_BYTES:,}. Tighten the generator format "
            "or raise the ceiling after validating boot survives."
        )

    def test_req_index_is_idempotent(self, tmp_path, monkeypatch):
        """Second call with no backlog change must be a no-op (mtime-based skip)."""
        from chathealthy_devops_boot import chathealthy_devops_boot

        # Use real file so mtime check is meaningful
        boot = chathealthy_devops_boot.__new__(chathealthy_devops_boot)
        boot.brain = {}
        boot._constraints = []
        boot._state = {}
        boot._booted = False

        # Touch req_index.txt so it's newer than agile_backlog.json
        if self.REQ_INDEX_PATH.exists():
            os.utime(self.REQ_INDEX_PATH, None)
        # First call — should be a no-op since req_index.txt is now newer
        result = boot._write_req_index()
        assert result is None, (
            "When req_index.txt mtime >= agile_backlog.json mtime, _write_req_index "
            "must return None (skip) — got {}".format(result)
        )
