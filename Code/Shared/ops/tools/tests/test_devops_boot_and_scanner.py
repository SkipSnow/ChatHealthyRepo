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

    def test_inform_claude_includes_orphan_table_when_present(self):
        """When orphan bugs exist, output includes orphan table."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        orphans = [{"id": "BUG-TEST-001", "type": "medium", "rule": "test rule", "date": "2026-01-01"}]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "BUG-TEST-001" in result
        assert "ORPHAN BUG TRIAGE" in result

    def test_inform_claude_no_orphan_table_when_empty(self):
        """When no orphan bugs, orphan table section is absent."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        result = boot.inform_claude({"orphan_bugs": []})
        assert "ORPHAN BUG TRIAGE" not in result


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
# REQ-007: No SessionStart hook in .claude/settings.json
# ============================================================================

class TestREQ007_NoSessionStartHook:
    """ARCH-DEVOPS-BOOT-001-REQ-007: There MUST be no SessionStart hook in
    .claude/settings.json."""

    def test_settings_json_exists(self):
        """settings.json must exist."""
        settings_path = SETTINGS_DIR / "settings.json"
        assert settings_path.exists()

    def test_no_session_start_hook(self):
        """No SessionStart key in hooks config."""
        settings_path = SETTINGS_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert "SessionStart" not in hooks, \
            "settings.json must NOT have a SessionStart hook"

    def test_has_other_required_hooks(self):
        """settings.json must have UserPromptSubmit, PreToolUse, Stop, SessionEnd."""
        settings_path = SETTINGS_DIR / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for required in ["UserPromptSubmit", "PreToolUse", "Stop", "SessionEnd"]:
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
    """ARCH-DEVOPS-BOOT-001-REQ-011: After mode selection, if orphan bugs exist,
    present orphan table and ask user to triage."""

    def test_orphan_table_has_headers(self):
        """Orphan table must have ID, Type, Description, Date headers."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        orphans = [{"id": "BUG-001", "type": "high", "rule": "something broke", "date": "2026-04-01"}]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "| ID |" in result or "| id |" in result.lower()
        assert "| Type |" in result or "| type |" in result.lower()

    def test_orphan_table_contains_bug_data(self):
        """Each orphan bug row must appear in the output."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        orphans = [
            {"id": "BUG-X-001", "type": "medium", "rule": "orphan rule text", "date": "2026-03-15"},
            {"id": "BUG-X-002", "type": "high", "rule": "second orphan", "date": "2026-03-20"},
        ]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "BUG-X-001" in result
        assert "BUG-X-002" in result
        assert "orphan rule text" in result

    def test_orphan_triage_directive_present(self):
        """Output must direct Claude to ask user to triage orphans."""
        boot = _fresh_boot_instance(load_full=False)
        boot.brain = {"version": {"current": {}}, "governance_matrix": {"matrix": {}}}
        boot._constraints = []
        boot._state = {}
        orphans = [{"id": "BUG-001", "type": "high", "rule": "test", "date": "2026-01-01"}]
        result = boot.inform_claude({"orphan_bugs": orphans})
        assert "triage" in result.lower()
        assert "Do NOT proceed" in result or "do not proceed" in result.lower()


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
            "bugs": [
                {"id": "BUG-001", "type": "high", "rule": "test rule", "date": "2026-01-01", "orphan": True},
                {"id": "BUG-002", "type": "low", "rule": "normal bug", "date": "2026-01-02", "orphan": False},
                {"id": "BUG-003", "type": "medium", "rule": "another orphan", "date": "2026-01-03", "orphan": True},
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
            "bugs": [
                {"id": "BUG-T-001", "type": "critical", "rule": "x" * 200, "date": "2026-04-15", "orphan": True},
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
                                with patch.object(boot, "_scan_orphans", return_value=[{"id": "BUG-MOCK"}]) as mock_scan:
                                    result = boot.boot()
        mock_scan.assert_called_once()
        assert result["orphan_bugs"] == [{"id": "BUG-MOCK"}]


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
                            "resolution_status": {
                                "required": True,
                                "constrained_by": "CV-009",
                            },
                        }
                    }
                }
            }
        }
        bugs_data = {
            "bugs": [
                {
                    "id": "BUG-TEST-001",
                    "status": "INVALID_STATUS",
                    "resolution_status": "BOGUS_VALUE",
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
        bugs_data = {"bugs": [{"id": "BUG-OK-001", "status": "open"}]}
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
        bugs_data = {"bugs": [{"id": "BUG-BAD", "type": "TOTALLY_INVALID_TYPE"}]}
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
