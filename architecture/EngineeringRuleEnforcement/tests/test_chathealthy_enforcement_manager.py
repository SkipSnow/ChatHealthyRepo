"""Tests for ChatHealthyEnforcementManager (V17 §4.4.1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import chathealthy_enforcement_manager as ce
from chathealthy_enforcement_manager import ChatHealthyEnforcementManager


# ─────────────────────────────────────────────────────────────────────────────
# Constants and aggregation precedence (TR-2)
# ─────────────────────────────────────────────────────────────────────────────
class TestExitCodeConstants:
    def test_constants_match_TR2(self):
        assert ChatHealthyEnforcementManager.EXIT_OK == 0
        assert ChatHealthyEnforcementManager.EXIT_VIOLATIONS_FOUND == 1
        assert ChatHealthyEnforcementManager.EXIT_MANAGER_ERROR == 2
        assert ChatHealthyEnforcementManager.EXIT_WORKER_SPAWN_FAILURE == 3
        assert ChatHealthyEnforcementManager.EXIT_WORKER_TIMEOUT == 4
        assert ChatHealthyEnforcementManager.EXIT_WORKER_INTERNAL_ERROR == 5

    def test_default_timeout_seconds(self):
        assert ChatHealthyEnforcementManager.DEFAULT_TIMEOUT_SECONDS == 30

    def test_hooks_count(self):
        # 4 Git + 6 Claude Code = 10
        assert len(ChatHealthyEnforcementManager.HOOKS) == 10
        for hook in [
            "pre-commit", "commit-msg", "post-commit", "pre-push",
            "SessionStart", "UserPromptSubmit", "PreToolUse",
            "Stop", "SessionEnd", "InstructionsLoaded",
        ]:
            assert hook in ChatHealthyEnforcementManager.HOOKS


class TestAggregationPrecedence:
    """TR-2 aggregation: 2 > 3 > 5 > 4 > 1 > 0."""

    def _agg(self, codes):
        m = ChatHealthyEnforcementManager("pre-commit")
        return m._aggregate(codes)

    def test_empty_returns_ok(self):
        assert self._agg([]) == 0

    def test_all_ok(self):
        assert self._agg([0, 0, 0]) == 0

    def test_violations_beat_ok(self):
        assert self._agg([0, 1, 0]) == 1

    def test_timeout_beats_violations(self):
        assert self._agg([0, 1, 4]) == 4

    def test_internal_error_beats_timeout(self):
        assert self._agg([1, 4, 5]) == 5

    def test_spawn_failure_beats_internal_error(self):
        assert self._agg([1, 3, 5]) == 3

    def test_manager_error_beats_all(self):
        assert self._agg([0, 1, 2, 3, 4, 5]) == 2

    def test_specific_required_examples_from_brief(self):
        # _aggregate([0,1,2]) returns 2
        assert self._agg([0, 1, 2]) == 2
        # _aggregate([1,3,5]) returns 3
        assert self._agg([1, 3, 5]) == 3
        # _aggregate([4,1,0]) returns 4
        assert self._agg([4, 1, 0]) == 4
        # _aggregate([5,4,1]) returns 5
        assert self._agg([5, 4, 1]) == 5


# ─────────────────────────────────────────────────────────────────────────────
# Constructor validation
# ─────────────────────────────────────────────────────────────────────────────
class TestConstructor:
    def test_valid_hook_accepted(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        assert m.hook_name == "pre-commit"

    def test_unknown_hook_raises(self):
        with pytest.raises(ValueError):
            ChatHealthyEnforcementManager("not-a-hook")


# ─────────────────────────────────────────────────────────────────────────────
# Filter logic
# ─────────────────────────────────────────────────────────────────────────────
class TestFilterEnforcements:
    def test_filters_by_hook(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        rules = [
            {
                "id": "Rule-001",
                "enforcements": {
                    "enforcement": [
                        {"enforcement_id": "Rule-001-ENF-001", "hook": "pre-push"},
                        {"enforcement_id": "Rule-001-ENF-002", "hook": "pre-commit"},
                    ]
                },
            },
            {
                "id": "Rule-002",
                "enforcements": {
                    "enforcement": [
                        {"enforcement_id": "Rule-002-ENF-001", "hook": "pre-commit"},
                    ]
                },
            },
        ]
        out = m._filter_enforcements(rules, "pre-commit")
        ids = [e["enforcement_id"] for e in out]
        assert ids == ["Rule-001-ENF-002", "Rule-002-ENF-001"]
        # rule_id is decorated onto the entry.
        assert out[0]["rule_id"] == "Rule-001"
        assert out[1]["rule_id"] == "Rule-002"


# ─────────────────────────────────────────────────────────────────────────────
# Startup self-check (warning #2)
# ─────────────────────────────────────────────────────────────────────────────
class TestStartupSelfCheck:
    def test_passes_on_real_rules(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        assert m._startup_self_check() == 0

    def test_returns_manager_error_on_missing_rules(self, monkeypatch, tmp_path):
        m = ChatHealthyEnforcementManager("pre-commit")
        monkeypatch.setattr(m, "ENGINEERING_RULES_PATH", tmp_path / "missing.json")
        assert m._startup_self_check() == ChatHealthyEnforcementManager.EXIT_MANAGER_ERROR

    def test_returns_manager_error_on_corrupt_rules(self, monkeypatch, tmp_path):
        bad = tmp_path / "rules.json"
        bad.write_text("not json {", encoding="utf-8")
        m = ChatHealthyEnforcementManager("pre-commit")
        monkeypatch.setattr(m, "ENGINEERING_RULES_PATH", bad)
        assert m._startup_self_check() == ChatHealthyEnforcementManager.EXIT_MANAGER_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# Spawn worker — uses a tiny stub-worker script to exercise the manager
# ─────────────────────────────────────────────────────────────────────────────
STUB_WORKER_OK = '''
import sys
print("stub-worker ok", flush=True)
sys.exit(0)
'''

STUB_WORKER_VIOLATIONS = '''
import sys
print("stub-worker violations", flush=True)
sys.exit(1)
'''

STUB_WORKER_INTERNAL_ERROR = '''
import sys
sys.stderr.write("stub-worker internal error\\n")
sys.exit(2)
'''

STUB_WORKER_HIGH_EXIT = '''
import sys
sys.exit(7)  # promoted to EXIT_WORKER_INTERNAL_ERROR (5)
'''

STUB_WORKER_TIMEOUT = '''
import time
import sys
time.sleep(60)
sys.exit(0)
'''


@pytest.fixture
def stub_worker(tmp_path):
    """Yield a callable that writes a stub-worker script and returns its rel path."""
    repo_root = ce.PROJECT_ROOT

    written = []

    def make(script_body: str, name: str = "stub_worker.py") -> str:
        # Write the stub somewhere under the repo root so the manager's
        # subprocess.run with cwd=PROJECT_ROOT can find it relative.
        target_dir = repo_root / "architecture" / "EngineeringRuleEnforcement" / "tests" / "_tmp_stubs"
        target_dir.mkdir(parents=True, exist_ok=True)
        script_path = target_dir / name
        script_path.write_text(script_body, encoding="utf-8")
        written.append(script_path)
        rel = script_path.relative_to(repo_root).as_posix()
        return rel

    yield make

    for p in written:
        if p.exists():
            p.unlink()


class TestSpawnWorker:
    def test_clean_exit(self, stub_worker):
        rel = stub_worker(STUB_WORKER_OK)
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-001",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 10,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_OK

    def test_violations_exit(self, stub_worker):
        rel = stub_worker(STUB_WORKER_VIOLATIONS, "stub_v.py")
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-002",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 10,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_VIOLATIONS_FOUND

    def test_worker_internal_error_exit2(self, stub_worker):
        rel = stub_worker(STUB_WORKER_INTERNAL_ERROR, "stub_e.py")
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-003",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 10,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_WORKER_INTERNAL_ERROR

    def test_worker_high_exit_code_promoted(self, stub_worker):
        rel = stub_worker(STUB_WORKER_HIGH_EXIT, "stub_h.py")
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-004",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 10,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_WORKER_INTERNAL_ERROR

    def test_worker_timeout_returns_timeout(self, stub_worker):
        rel = stub_worker(STUB_WORKER_TIMEOUT, "stub_t.py")
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-005",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 1,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_WORKER_TIMEOUT

    def test_missing_executable_returns_spawn_failure(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-006",
            "executable_path": "architecture/EngineeringRuleEnforcement/code/does_not_exist.py",
            "requires_lock": False,
            "timeout": 10,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_WORKER_SPAWN_FAILURE

    def test_missing_executable_path_field(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-007",
            "requires_lock": False,
        })
        assert rc == ChatHealthyEnforcementManager.EXIT_WORKER_SPAWN_FAILURE


# ─────────────────────────────────────────────────────────────────────────────
# Lock acquisition smoke test (warning #5: Windows file-lock edge cases)
# ─────────────────────────────────────────────────────────────────────────────
class TestLockAcquisition:
    def test_acquire_release_round_trip(self):
        m = ChatHealthyEnforcementManager("pre-commit")
        lock = m._acquire_lock("Rule-999-ENF-LOCK-A")
        assert lock.is_locked
        m._release_lock(lock)
        assert not lock.is_locked

    def test_lock_serializes_two_competitors(self, tmp_path):
        """Two manager instances racing on the same id MUST serialize."""
        m1 = ChatHealthyEnforcementManager("pre-commit")
        m2 = ChatHealthyEnforcementManager("pre-commit")
        lock1 = m1._acquire_lock("Rule-999-ENF-LOCK-B")
        try:
            assert lock1.is_locked
            # Second acquire on a non-blocking variant should fail-fast.
            from filelock import FileLock, Timeout as FLT
            lock_path = m2._LOCK_DIR / "Rule-999-ENF-LOCK-B.lock"
            second = FileLock(str(lock_path))
            with pytest.raises(FLT):
                second.acquire(timeout=0.5)
        finally:
            m1._release_lock(lock1)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: manager.run() against the real engineering_rules.json
# ─────────────────────────────────────────────────────────────────────────────
class TestRunEndToEnd:
    def test_unhooked_event_returns_ok(self):
        # No enforcement entries are wired to commit-msg in V1, so the
        # manager should sail through with EXIT_OK.
        m = ChatHealthyEnforcementManager("commit-msg")
        rc = m.run()
        assert rc == ChatHealthyEnforcementManager.EXIT_OK

    def test_subprocess_cold_start_measurement(self, stub_worker, capsys):
        """Warning #1: measure subprocess cold-start time on this machine."""
        rel = stub_worker(STUB_WORKER_OK, "stub_cold.py")
        m = ChatHealthyEnforcementManager("pre-commit")
        t0 = time.perf_counter()
        rc = m._spawn_worker({
            "enforcement_id": "Rule-999-ENF-COLD",
            "executable_path": rel,
            "requires_lock": False,
            "timeout": 10,
        })
        elapsed = time.perf_counter() - t0
        assert rc == 0
        # Persist the measurement to a file the audit step will read.
        out_path = (
            ce.PROJECT_ROOT
            / "architecture"
            / "EngineeringRuleEnforcement"
            / "tests"
            / "_cold_start_seconds.txt"
        )
        out_path.write_text(f"{elapsed:.3f}\n", encoding="utf-8")
