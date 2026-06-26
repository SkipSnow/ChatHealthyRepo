"""Tests for ScanSeparationOfConcernsWorker (Rule-009).

One pytest file. Static fixtures live under
architecture/EngineeringRuleEnforcement/tests/fixtures/separation_of_concerns/

Coverage:
  REQ-B-001 (display authoring) - violation/clean + ClientRouter excluded_exact
  REQ-B-002 (React persistence) - violation/clean + WelcomeWidget excluded_exact
  REQ-B-003 (business logic)    - violation/clean
  REQ-B-004 (deterministic)     - static source check: worker imports no LLM
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import enforcement_worker as ew
import scan_separation_of_concerns_worker as soc

from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND


FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "separation_of_concerns"
)


# ─── helpers ─────────────────────────────────────────────────────────────

def _make_worker(scopes_by_check=None, hook="pre-commit"):
    """Build a worker with no real engineering_rules.json read - feed
    scopes directly as the (function_name, scope_type, terms) triples
    list the base class expects."""
    worker = soc.ScanSeparationOfConcernsWorker("Rule-009-ENF-001")
    worker.hook = hook
    triples: list[tuple[str, str, list[str]]] = []
    if scopes_by_check:
        for check, kind_to_list in scopes_by_check.items():
            for scope_type, terms in kind_to_list.items():
                triples.append((check, scope_type, list(terms)))
    worker.scopes = triples
    worker.emitted: list = []
    worker._emit_violation = worker.emitted.append  # type: ignore[assignment]
    return worker


def _stage(worker, *fixture_rel_paths):
    """Point the worker at fixture file paths via the env override."""
    abs_paths = [str(FIXTURE_DIR / rel) for rel in fixture_rel_paths]
    with patch.dict(os.environ, {
        "SCAN_FILES_ENFORCEMENT_TARGETS": os.pathsep.join(abs_paths),
    }):
        rc = worker.run()
    return rc, worker.emitted


# ─── REQ-B-001: display authoring ────────────────────────────────────────

class TestB001DisplayAuthoring:
    def test_violation_js_is_flagged(self):
        worker = _make_worker({
            "_scan_display_authoring": {
                "allowed_pattern": [r"violation\.js$"],
            },
        })
        rc, hits = _stage(worker, "display_authoring/violation.js")
        assert rc == EXIT_VIOLATIONS_FOUND
        markers = {v.message.split("matched: ")[1].rstrip(")") for v in hits}
        # innerHTML + createElement + textContent + template-literal-html
        assert "innerHTML" in markers
        assert "createElement" in markers
        assert "textContent" in markers
        assert "template-literal-html" in markers
        for v in hits:
            assert v.rule_id == "Rule-009"

    def test_violation_html_inline_script_is_flagged(self):
        worker = _make_worker({
            "_scan_display_authoring": {
                "allowed_pattern": [r"violation\.html$"],
            },
        })
        rc, hits = _stage(worker, "display_authoring/violation.html")
        assert rc == EXIT_VIOLATIONS_FOUND
        assert any("innerHTML" in v.message for v in hits)

    def test_clean_js_passes(self):
        worker = _make_worker({
            "_scan_display_authoring": {
                "allowed_pattern": [r"clean\.js$"],
            },
        })
        rc, hits = _stage(worker, "display_authoring/clean.js")
        assert rc == EXIT_OK
        assert hits == []

    def test_clientrouter_excluded_exact_skipped(self):
        # The courier fixture writes innerHTML with opaque content; same
        # pattern as the violation. excluded_exact stops the report.
        rel = "display_authoring/courier_clientrouter.js"
        worker = _make_worker({
            "_scan_display_authoring": {
                "allowed_pattern": [r"courier_clientrouter\.js$"],
                "excluded_exact":  [str(FIXTURE_DIR / rel)],
            },
        })
        rc, hits = _stage(worker, rel)
        assert rc == EXIT_OK
        assert hits == []


# ─── REQ-B-002: React persistence ────────────────────────────────────────

class TestB002ReactPersistence:
    def test_violation_tsx_is_flagged(self):
        worker = _make_worker({
            "_scan_react_persistence": {
                "allowed_pattern": [r"violation\.tsx$"],
            },
        })
        rc, hits = _stage(worker, "react_persistence/violation.tsx")
        assert rc == EXIT_VIOLATIONS_FOUND
        markers = {v.message.split("matched: ")[1].rstrip(")") for v in hits}
        assert "persistent-store-write" in markers
        assert "non-canonical-outbound-call" in markers

    def test_clean_tsx_passes(self):
        worker = _make_worker({
            "_scan_react_persistence": {
                "allowed_pattern": [r"clean\.tsx$"],
            },
        })
        rc, hits = _stage(worker, "react_persistence/clean.tsx")
        assert rc == EXIT_OK
        assert hits == []

    def test_welcomewidget_excluded_exact_skipped(self):
        # Approved out-of-band read: fetch('/welcome'). The worker would
        # flag it, but the excluded_exact entry suppresses the report.
        rel = "react_persistence/approved_welcome.tsx"
        worker = _make_worker({
            "_scan_react_persistence": {
                "allowed_pattern": [r"approved_welcome\.tsx$"],
                "excluded_exact":  [str(FIXTURE_DIR / rel)],
            },
        })
        rc, hits = _stage(worker, rel)
        assert rc == EXIT_OK
        assert hits == []


# ─── REQ-B-003: business logic ───────────────────────────────────────────

class TestB003BusinessLogic:
    def test_violation_js_is_flagged(self):
        worker = _make_worker({
            "_scan_business_logic": {
                "allowed_pattern": [r"violation\.js$"],
            },
        })
        rc, hits = _stage(worker, "business_logic/violation.js")
        assert rc == EXIT_VIOLATIONS_FOUND
        markers = {v.message.split("matched: ")[1].rstrip(")") for v in hits}
        # Tokens present in fixture: specialty, provider, npi, clinical_trial
        assert {"specialty", "provider", "npi", "clinical_trial"}.issubset(markers)

    def test_violation_html_inline_script_is_flagged(self):
        worker = _make_worker({
            "_scan_business_logic": {
                "allowed_pattern": [r"violation\.html$"],
            },
        })
        rc, hits = _stage(worker, "business_logic/violation.html")
        assert rc == EXIT_VIOLATIONS_FOUND
        markers = {v.message.split("matched: ")[1].rstrip(")") for v in hits}
        assert {"provider", "npi"}.issubset(markers)

    def test_clean_js_passes(self):
        worker = _make_worker({
            "_scan_business_logic": {
                "allowed_pattern": [r"clean\.js$"],
            },
        })
        rc, hits = _stage(worker, "business_logic/clean.js")
        assert rc == EXIT_OK
        assert hits == []


# ─── REQ-B-004: deterministic enforcement ────────────────────────────────

class TestB004Deterministic:
    def test_worker_source_does_not_import_any_llm(self):
        """Static source check: no LLM client / provider import appears in
        the worker. Satisfies REQ-B-004 (no LLM call in pre-commit path)."""
        worker_path = Path(soc.__file__).resolve()
        src = worker_path.read_text(encoding="utf-8")
        forbidden = (
            "openai", "anthropic", "langchain", "tiktoken",
            "google.generativeai", "litellm",
        )
        for needle in forbidden:
            assert needle not in src.lower(), (
                f"worker source must not import or reference {needle!r}"
            )
