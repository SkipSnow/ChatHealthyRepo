"""Tests for EnforcementWorker (V17 §4.4.2 / §4.5)."""

from __future__ import annotations

import io
import json
import sys

import pytest

import enforcement_worker as ew
from enforcement_worker import (
    EnforcementWorker,
    ViolationRecord,
    WorkerInternalError,
    SCOPE_LIST_TYPES,
    EXIT_OK,
    EXIT_VIOLATIONS_FOUND,
    EXIT_WORKER_ERROR,
    EXIT_WORKER_INTERNAL_ERROR,
)

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'FrontEndApplicationLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.exceptions import ChatHealthyException


# A tiny concrete subclass for exercising the base class.
class _DummyWorker(EnforcementWorker):
    SCOPE_DEFAULTS = {"_check_a": True, "_check_b": False}

    def _check_a(self, file_path):  # noqa: D401 — test stub
        return []

    def _check_b(self, file_path):
        return []

    def run(self) -> int:
        return EXIT_OK


@pytest.fixture
def fake_entry(monkeypatch, tmp_path):
    """Stash a synthetic engineering_rules.json with a known dummy entry."""
    rules = {
        "$schema": "https://dev.chathealthy.ai/schemas/EngineeringRulesSchema.json",
        "rules": {"rule": [
            {
                "id": "Rule-999",
                "name": "dummy",
                "description": "test fixture",
                "rule_statements": {"rule_statement": [
                    {"statement_number": 1, "statement_body": "x", "req_id": "y"}
                ]},
                "enforcements": {"enforcement": [
                    {
                        "enforcement_id": "Rule-999-ENF-001",
                        "hook": "pre-commit",
                        "executable_path": "x.py",
                        "requires_lock": False,
                        "scopes": [
                            ["_check_a", "allowed_exact", ["alpha", "beta"]],
                            ["_check_a", "allowed_pattern", ["^foo/"]],
                            ["_check_a", "excluded_exact", ["banned.txt"]],
                            ["_check_a", "excluded_pattern", ["^secret/"]],
                            ["_check_b", "excluded_exact", ["nope.txt"]],
                        ],
                    }
                ]},
            }
        ]}
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setattr(ew, "ENGINEERING_RULES_PATH", path)
    return path


class TestViolationRecord:
    def test_round_trip(self):
        v = ViolationRecord("Rule-001-ENF-001", "Rule-001", "x.py", "msg", "warn")
        d = v.to_dict()
        assert d == {
            "enforcement_id": "Rule-001-ENF-001",
            "rule_id": "Rule-001",
            "resource": "x.py",
            "message": "msg",
            "severity": "warn",
        }

    def test_invalid_severity_rejected(self):
        # Rule-003: deliberate raises are ChatHealthyException, not a
        # built-in. The rejection itself is unchanged.
        from chathealthy_frontend_lib.exceptions import ChatHealthyException
        with pytest.raises(ChatHealthyException) as exc:
            ViolationRecord("a", "b", "c", "d", "fatal")
        assert "severity must be" in str(exc.value)


class TestScopeListTypes:
    def test_canonical_four(self):
        assert set(SCOPE_LIST_TYPES) == {
            "allowed_exact", "allowed_pattern",
            "excluded_exact", "excluded_pattern",
        }


class TestLoadEnforcement:
    def test_loads_own_entry(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        assert w.entry["enforcement_id"] == "Rule-999-ENF-001"
        assert w.executable_path == "x.py"
        assert w.rule_id == "Rule-999"
        assert w.hook == "pre-commit"

    def test_unknown_enforcement_raises(self, fake_entry):
        with pytest.raises(WorkerInternalError):
            _DummyWorker("Rule-999-ENF-NOPE")


class TestScopeFunctionNameValidation:
    def test_typo_in_function_name_fails_loudly(self, monkeypatch, tmp_path):
        rules = {
            "$schema": "https://dev.chathealthy.ai/schemas/EngineeringRulesSchema.json",
            "rules": {"rule": [{
                "id": "Rule-999", "name": "n", "description": "d",
                "rule_statements": {"rule_statement": [
                    {"statement_number": 1, "statement_body": "x", "req_id": "y"}
                ]},
                "enforcements": {"enforcement": [{
                    "enforcement_id": "Rule-999-ENF-001",
                    "hook": "pre-commit",
                    "executable_path": "x.py",
                    "requires_lock": False,
                    "scopes": [["_typo_method", "allowed_exact", ["a"]]],
                }]}
            }]}
        }
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        monkeypatch.setattr(ew, "ENGINEERING_RULES_PATH", path)
        with pytest.raises(AttributeError):
            _DummyWorker("Rule-999-ENF-001")


class TestIsInScope:
    """TR-5: excluded_exact → excluded_pattern → allowed_exact → allowed_pattern → SCOPE_DEFAULT."""

    def test_excluded_exact_wins_over_allowed_exact(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        # "banned.txt" is excluded_exact, no allowed_exact match → False.
        assert w.is_in_scope("banned.txt", "_check_a") is False

    def test_excluded_pattern_wins_over_allowed_pattern(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        # ^secret/ excluded → False even though no allowed match.
        assert w.is_in_scope("secret/keys.txt", "_check_a") is False

    def test_allowed_exact_returns_true(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        assert w.is_in_scope("alpha", "_check_a") is True

    def test_allowed_pattern_returns_true(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        assert w.is_in_scope("foo/bar.py", "_check_a") is True

    def test_no_match_falls_through_to_default_true(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        # _check_a default is True
        assert w.is_in_scope("random.txt", "_check_a") is True

    def test_no_match_falls_through_to_default_false(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        # _check_b default is False
        assert w.is_in_scope("random.txt", "_check_b") is False

    def test_excluded_for_check_b_returns_false(self, fake_entry):
        w = _DummyWorker("Rule-999-ENF-001")
        assert w.is_in_scope("nope.txt", "_check_b") is False

    def test_function_name_isolates_checks(self, fake_entry):
        # An exclusion on _check_a for "secret/x" must NOT apply to _check_b.
        w = _DummyWorker("Rule-999-ENF-001")
        assert w.is_in_scope("secret/x", "_check_a") is False  # excluded
        # _check_b has no rules for "secret/x"; falls through to default False.
        assert w.is_in_scope("secret/x", "_check_b") is False


class TestEmitViolation:
    def test_emits_one_json_per_line(self, fake_entry, capsys):
        w = _DummyWorker("Rule-999-ENF-001")
        v = ViolationRecord("Rule-999-ENF-001", "Rule-999", "f.py", "boom", "error")
        w._emit_violation(v)
        out = capsys.readouterr().out
        line = out.strip()
        parsed = json.loads(line)
        assert parsed["kind"] == "violation"
        assert parsed["enforcement_id"] == "Rule-999-ENF-001"
        assert parsed["resource"] == "f.py"
        assert parsed["severity"] == "error"


class TestEmitTelemetry:
    def test_envelope_shape(self, fake_entry, capsys):
        w = _DummyWorker("Rule-999-ENF-001")
        w._emit_telemetry("start", 0, 0, 0, 0)
        out = capsys.readouterr().out.strip()
        parsed = json.loads(out)
        assert parsed["kind"] == "telemetry"
        for key in (
            "enforcement_id", "hook", "phase", "duration_ms",
            "files_scanned", "violation_count", "exit_code",
        ):
            assert key in parsed


class TestMainExitCodes:
    def test_clean_run_returns_zero(self, fake_entry):
        rc = _DummyWorker.main(["Rule-999-ENF-001"])
        assert rc == EXIT_OK

    def test_uncaught_exception_returns_two(self, fake_entry):
        class _BoomWorker(_DummyWorker):
            def run(self):
                raise ChatHealthyException(
                    mode="runtime_error",
                    component="test_enforcement_worker",
                    message="boom")
        rc = _BoomWorker.main(["Rule-999-ENF-001"])
        assert rc == EXIT_WORKER_ERROR

    def test_internal_error_returns_five(self, fake_entry):
        class _CfgWorker(_DummyWorker):
            def run(self):
                raise WorkerInternalError("schema missing")
        rc = _CfgWorker.main(["Rule-999-ENF-001"])
        assert rc == EXIT_WORKER_INTERNAL_ERROR

    def test_violations_returns_one(self, fake_entry):
        class _ViolWorker(_DummyWorker):
            def run(self):
                return EXIT_VIOLATIONS_FOUND
        rc = _ViolWorker.main(["Rule-999-ENF-001"])
        assert rc == EXIT_VIOLATIONS_FOUND


class TestNoInProcessTimeout:
    """Workers MUST NOT install signal handlers / watchdog threads (V17 §4.3.2)."""

    def test_worker_module_imports_no_signal(self):
        import scan_files_enforcement_worker as sfew
        # The worker must not have imported `signal` (no in-process timeout).
        # We allow other modules to have signal in sys.modules (Python may
        # import it transitively); we check the worker's own module dict.
        assert "signal" not in sfew.__dict__

    def test_worker_module_has_no_watchdog_thread_constructs(self):
        import scan_files_enforcement_worker as sfew
        assert "threading" not in sfew.__dict__
