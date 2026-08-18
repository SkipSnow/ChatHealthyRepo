"""Unit tests for Rule-065-ENF-009's worker.

Two kinds, and the split matters.

Most of the worker is deterministic and is tested as such: prose stripping,
the override, the manifest check, and the rule that decides which verdicts
count. Those run in milliseconds, need no network and no key, and they are
where the real bugs have been -- a RuntimeError where ChatHealthyException was
required, a substring scan that convicted eight bystanders, a crash that a
grep -c could not tell from a clean pass.

The prompt itself is the other kind. It cannot be asserted deterministically,
so it is tested the only way a judgement can be: give it code that is right
and code that is deliberately wrong, and require it to tell them apart. Those
tests cost money and take minutes, so they run only when CH_PROMPT_TEST=1.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

import pytest

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / ".git").is_dir():
        _REPO = _parent
        break

_WORKER_PATH = (_REPO / "architecture" / "EngineeringRuleEnforcement" / "code"
                / "scan_data_migration_compliance_worker.py")
_OVERRIDE_IDS = "CH_ENF009_OVERRIDE_REQUIREMENTS"
_OVERRIDE_REASON = "CH_ENF009_OVERRIDE_REASON"
_PROMPT_TEST_ENV = "CH_PROMPT_TEST"


def _worker_module():
    sys.path.insert(0, str(_WORKER_PATH.parent))
    sys.path.insert(0, str(_REPO / "ChatHealthyLib" / "src"))
    spec = importlib.util.spec_from_file_location("enf009", _WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def enf009():
    return _worker_module()


@pytest.fixture(autouse=True)
def no_ambient_override():
    """No test inherits a waiver from the shell that launched pytest."""
    saved = {k: os.environ.pop(k, None) for k in (_OVERRIDE_IDS, _OVERRIDE_REASON)}
    yield
    for key, value in saved.items():
        if value is not None:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


# ── prose stripping ──────────────────────────────────────────────────────────

_WITH_PROSE = '''"""A module docstring making a claim."""


class Thing:
    """A class docstring promising a check."""

    def act(self, name):
        """This docstring says it refuses duplicates."""
        # A comment claiming the role has no delete grant.
        return self.store[name].insert_many([{"n": 1}])
'''


def test_stripping_removes_every_claim_and_keeps_every_statement(enf009):
    stripped = enf009.ScanDataMigrationComplianceWorker._code_only(_WITH_PROSE)
    for claim in ("making a claim", "promising a check", "refuses duplicates",
                  "no delete grant"):
        assert claim not in stripped, f"prose survived: {claim}"
    assert "insert_many" in stripped
    assert "class Thing" in stripped
    assert "def act" in stripped


def test_stripping_leaves_a_body_behind_when_a_docstring_was_the_whole_body(enf009):
    """A function whose only statement is its docstring must still parse."""
    source = 'def empty():\n    """Only a docstring."""\n'
    stripped = enf009.ScanDataMigrationComplianceWorker._code_only(source)
    assert "Only a docstring" not in stripped
    compile(stripped, "<stripped>", "exec")


def test_stripping_does_not_silently_alter_behaviour(enf009):
    """The stripped module must still contain the calls that decide compliance."""
    real = (_REPO / "pipeline" / "Code" / "data_migration.py").read_text(encoding="utf-8")
    stripped = enf009.ScanDataMigrationComplianceWorker._code_only(real)
    for call in ("insert_many", "migration_not_authorized",
                 "migration_target_exists", "migration_source_absent",
                 "human_click"):
        assert call in stripped, f"stripping lost {call}"
    for prose in ("Rule-065 forbids", "copy faithfully", "back door"):
        assert prose not in stripped


# ── the override ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env,expected", [
    ({}, set()),
    ({_OVERRIDE_IDS: "REQ-B-002"}, set()),
    ({_OVERRIDE_REASON: "a reason"}, set()),
    ({_OVERRIDE_IDS: "*", _OVERRIDE_REASON: "everything"}, set()),
    ({_OVERRIDE_IDS: "ALL", _OVERRIDE_REASON: "everything"}, set()),
    ({_OVERRIDE_IDS: "all", _OVERRIDE_REASON: "everything"}, set()),
    ({_OVERRIDE_IDS: "REQ-B-002", _OVERRIDE_REASON: "retiring next sprint"},
     {"REQ-B-002"}),
    ({_OVERRIDE_IDS: " REQ-B-002 , REQ-B-009 ", _OVERRIDE_REASON: "both known"},
     {"REQ-B-002", "REQ-B-009"}),
])
def test_the_override_waives_only_what_is_fully_specified(enf009, env, expected):
    """Refusing is the default. A waiver needs named ids AND a reason, and a
    blanket value is not a waiver at all."""
    os.environ.update(env)
    ids, reason = enf009.ScanDataMigrationComplianceWorker._override()
    assert ids == expected
    assert bool(reason) == bool(expected)


# ── which verdicts count ─────────────────────────────────────────────────────

def _verdict(enf009, **kwargs):
    fields = {"req_id": "REQ-B-001", "enforced": True, "determined": True,
              "confidence": 95, "reason": "because the code says so"}
    fields.update(kwargs)
    return enf009.RequirementVerdict(**fields)


@pytest.mark.parametrize("determined,confidence,counts", [
    (True, 100, True),
    (True, 90, True),
    (True, 89, False),
    (True, 0, False),
    (False, 100, False),
    (False, 90, False),
])
def test_only_determined_and_confident_verdicts_settle(enf009, determined,
                                                       confidence, counts):
    """The bar is 90 and it is a floor, not a suggestion: an undetermined
    verdict never settles no matter how confident it claims to be."""
    verdict = _verdict(enf009, determined=determined, confidence=confidence)
    settles = verdict.determined and verdict.confidence >= enf009._MIN_CONFIDENCE
    assert settles is counts


def test_confidence_outside_zero_to_one_hundred_is_rejected(enf009):
    for bad in (-1, 101):
        with pytest.raises(Exception):
            _verdict(enf009, confidence=bad)


def test_the_gate_refuses_rather_than_passes_when_nothing_is_settled(enf009):
    """The property that matters most: no verdict means no commit."""
    assert enf009.EXIT_VIOLATIONS_FOUND != enf009.EXIT_OK
    assert enf009._AUDIT_ROUNDS >= 1
    assert enf009._MIN_CONFIDENCE == 90


# ── the manifest check ───────────────────────────────────────────────────────

def test_the_manifest_check_passes_on_the_live_manifest(enf009):
    worker = enf009.ScanDataMigrationComplianceWorker("Rule-065-ENF-009")
    assert worker._manifest_violation() == ""


def test_the_manifest_check_names_the_file_when_it_is_undeclared(enf009, tmp_path,
                                                                monkeypatch):
    """An unregistered migrator is the back door this check exists for."""
    manifest = json.loads(
        (_REPO / enf009._MANIFEST).read_text(encoding="utf-8"))
    for target in manifest.get("DeploymentTargetRecord", []):
        for binding in target.get("environments", []):
            binding["packages"] = [
                p for p in (binding.get("packages") or [])
                if p.get("package_id") != "data_migration"
            ]
    fake_repo = tmp_path
    (fake_repo / pathlib.Path(enf009._MANIFEST).parent).mkdir(parents=True, exist_ok=True)
    (fake_repo / enf009._MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")

    worker = enf009.ScanDataMigrationComplianceWorker("Rule-065-ENF-009")
    monkeypatch.setattr(worker, "_repo_root", lambda: fake_repo)
    problem = worker._manifest_violation()
    assert enf009._MIGRATION_FILE in problem
    assert "declared in no target or package" in problem


# ── the prompt itself ────────────────────────────────────────────────────────

_COMPLIANT = (_REPO / "pipeline" / "Code" / "data_migration.py")

_SABOTAGE = {
    "authorization removed": (
        'if (authorization.get("verdict") != "approve"',
        'if (False and authorization.get("verdict") != "approve"'),
    "collision check removed": (
        "        if self.exists_at_destination():",
        "        if False and self.exists_at_destination():"),
    "destination renamed": (
        "destination = self._destination()",
        'destination = self._destination_database()[self._collection_name + "_copy"]'),
}


def _audit_source(enf009, source: str) -> dict:
    worker = enf009.ScanDataMigrationComplianceWorker("Rule-065-ENF-009")
    requirements = worker._requirements()
    return worker._audit(enf009.ScanDataMigrationComplianceWorker._code_only(source),
                         requirements)


@pytest.mark.skipif(os.environ.get(_PROMPT_TEST_ENV) != "1",
                    reason="costs money and minutes; run with CH_PROMPT_TEST=1")
def test_the_prompt_answers_every_requirement_it_is_given(enf009):
    """Whatever it decides, it must decide all of them at the required
    confidence. Silence on a requirement is the failure mode that would let
    code through unexamined."""
    worker = enf009.ScanDataMigrationComplianceWorker("Rule-065-ENF-009")
    requirements = worker._requirements()
    assert requirements, "no requirements read from the backlog"
    verdicts = _audit_source(enf009, _COMPLIANT.read_text(encoding="utf-8"))
    missing = [r[0] for r in requirements if r[0] not in verdicts]
    assert not missing, f"unanswered after {enf009._AUDIT_ROUNDS} rounds: {missing}"
    assert all(v.confidence >= enf009._MIN_CONFIDENCE for v in verdicts.values())


@pytest.mark.skipif(os.environ.get(_PROMPT_TEST_ENV) != "1",
                    reason="costs money and minutes; run with CH_PROMPT_TEST=1")
@pytest.mark.parametrize("label", sorted(_SABOTAGE))
def test_the_prompt_catches_deliberately_broken_code(enf009, label):
    """The only test of a prompt that means anything: break the code in a way
    a requirement forbids, and require the auditor to notice. A prompt that
    passes sabotage is worse than no prompt, because it certifies."""
    old, new = _SABOTAGE[label]
    source = _COMPLIANT.read_text(encoding="utf-8")
    assert old in source, f"sabotage {label!r} no longer matches the file"
    verdicts = _audit_source(enf009, source.replace(old, new, 1))
    failed = [v for v in verdicts.values() if not v.enforced]
    assert failed, (
        f"sabotage {label!r} went unnoticed; every requirement came back "
        f"enforced")
