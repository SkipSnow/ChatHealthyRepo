"""Wire-up regression test for TR-16 (V21).

Severity tracks operational risk:

  - Hook carries at least one enforcement entry in engineering_rules.json
    -> HARD assert. Missing wire-up means the rule cannot fire; the build fails.
  - Hook carries no enforcement entries
    -> ADVISORY warning when the wire-up is missing. The hook is reserved per
       TR-16 for future Phase-6 workers; today its dispatch is a no-op so an
       absent wire is not an operational defect.

This bifurcation auto-flips: the moment a Phase-6 worker adds an enforcement
entry to a previously-unused hook, that hook's wire-up becomes a hard
assertion. Workers cannot ship without their wiring being installed.

V20 declared this wiring 'out of scope for the F-002 test suite,' which let
.git/hooks/* silently regress when the project directory was migrated. V21
removes the carve-out; this file is the regression that prevents the same
class of failure from recurring.
"""

import json
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"

GIT_HOOKS = ("pre-commit", "commit-msg", "post-commit", "pre-push")
CLAUDE_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "Stop",
    "SessionEnd",
    "InstructionsLoaded",
)
MANAGER_FILE = "chathealthy_enforcement_manager.py"


def _wired(body, hook):
    """Body invokes chathealthy_enforcement_manager.py with `--hook <hook>`."""
    return MANAGER_FILE in body and f"--hook {hook}" in body


@pytest.fixture(scope="module")
def hooks_with_enforcements():
    """Set of hook names that carry at least one enforcement entry today."""
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    in_use = set()
    for rule in rules.get("rules", {}).get("rule", []):
        for enf in rule.get("enforcements", {}).get("enforcement", []):
            hook = enf.get("hook")
            if hook:
                in_use.add(hook)
    return in_use


def _git_hook_wired(hook):
    hook_path = REPO_ROOT / ".git" / "hooks" / hook
    if not hook_path.exists():
        return False, hook_path
    body = hook_path.read_text(encoding="utf-8", errors="replace")
    return _wired(body, hook), hook_path


def _claude_hook_wired(hook):
    settings_path = REPO_ROOT / ".claude" / "settings.json"
    if not settings_path.exists():
        return False, settings_path
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    entries = settings.get("hooks", {}).get(hook, [])
    found = bool(entries) and any(
        _wired(h.get("command") or "", hook)
        for entry in entries
        for h in entry.get("hooks", [])
    )
    return found, settings_path


@pytest.mark.parametrize("hook", GIT_HOOKS)
def test_git_hook_wired(hook, hooks_with_enforcements):
    wired, hook_path = _git_hook_wired(hook)
    expected = f"{MANAGER_FILE} --hook {hook}"
    if hook in hooks_with_enforcements:
        assert wired, (
            f"HARD: hook '{hook}' carries an enforcement in engineering_rules.json but "
            f"{hook_path} does not invoke `{expected}`. .git/hooks/* is per-repo and "
            f"must be installed after a fresh clone or directory migration."
        )
    elif not wired:
        warnings.warn(
            f"ADVISORY: hook '{hook}' is reserved per TR-16 but not yet wired. "
            f"Install {hook_path} invoking `{expected}` when a worker is added to this hook.",
            stacklevel=2,
        )


@pytest.mark.parametrize("hook", CLAUDE_HOOKS)
def test_claude_hook_wired(hook, hooks_with_enforcements):
    wired, settings_path = _claude_hook_wired(hook)
    expected = f"{MANAGER_FILE} --hook {hook}"
    if hook in hooks_with_enforcements:
        assert wired, (
            f"HARD: hook '{hook}' carries an enforcement in engineering_rules.json but "
            f"{settings_path} has no command invoking `{expected}`."
        )
    elif not wired:
        warnings.warn(
            f"ADVISORY: hook '{hook}' is reserved per TR-16 but not yet wired. "
            f"Add a command invoking `{expected}` under hooks.{hook} in {settings_path} "
            f"when a worker is added to this hook.",
            stacklevel=2,
        )
