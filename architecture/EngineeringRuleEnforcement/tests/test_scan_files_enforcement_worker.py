"""Tests for ScanFilesEnforcementWorker (V18 §4.9 / §4.9.3 / §4.9.3.1).

Schema resolution is now web-fetch with a carve-out for the JSON Schema
2020-12 meta-schema. All HTTP calls are mocked — these tests must NEVER
make real network calls.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import enforcement_worker as ew
import scan_files_enforcement_worker as sfew
from enforcement_worker import (
    EXIT_OK,
    EXIT_VIOLATIONS_FOUND,
    WorkerInternalError,
)


FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "json_validation"
)
TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ew.PROJECT_ROOT


# Canonical URLs used in tests (V18 §4.9.3 + §4.9.3.1).
META_SCHEMA_URL = "https://json-schema.org/draft/2020-12/schema"
FIXTURE_SCHEMA_URL = (
    "https://dev.chathealthy.ai/schemas/JsonValidationFixtureSchema.json"
)
GHOST_SCHEMA_URL = (
    "https://dev.chathealthy.ai/schemas/ThisSchemaDoesNotExistOnDisk.json"
)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a fake urllib response object (context-manager friendly).
# ─────────────────────────────────────────────────────────────────────────────
def _fake_response(body_bytes: bytes) -> MagicMock:
    """Return a MagicMock that mimics urllib.request.urlopen()'s context-manager."""
    resp = MagicMock()
    resp.read.return_value = body_bytes
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _load_fixture_schema() -> dict:
    return json.loads((FIXTURE_DIR / "fixture_schema.json").read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Test entry under a synthetic engineering_rules.json (so the worker can be
# instantiated without depending on the real file's V1 entry).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def synthetic_rules(monkeypatch, tmp_path):
    rules = {
        "$schema": "https://dev.chathealthy.ai/schemas/EngineeringRulesSchema.json",
        "rules": {"rule": [{
            "id": "Rule-008",
            "name": "Brain JSONs must conform to human-approved schemas",
            "description": "test",
            "rule_statements": {"rule_statement": [
                {"statement_number": 1, "statement_body": "x", "req_id": "y"}
            ]},
            "enforcements": {"enforcement": [{
                "enforcement_id": "Rule-008-ENF-001",
                "hook": "pre-commit",
                "executable_path":
                    "architecture/EngineeringRuleEnforcement/code/scan_files_enforcement_worker.py",
                "requires_lock": False,
                "scopes": [
                    ["_scan_http", "allowed_pattern", [
                        "^http://(localhost|127\\.0\\.0\\.1)",
                        "^http://www\\.w3\\.org/",
                        "^http://169\\.254\\.169\\.254/"
                    ]],
                    ["_scan_http", "excluded_pattern", [
                        "^test_", "conftest\\.py$",
                        "^architecture/EngineeringRuleEnforcement/tests/",
                        "^Website/schemas/standard/",
                    ]],
                    ["_validate_json", "excluded_exact", ["package-lock.json"]],
                    ["_validate_json", "excluded_pattern", [
                        "^architecture/EngineeringRuleEnforcement/tests/fixtures/excluded_/",
                        "^Website/schemas/standard/",
                    ]],
                    ["_validate_json", "allowed_pattern", ["\\.json$"]],
                ],
            }]}
        }]}
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    monkeypatch.setattr(ew, "ENGINEERING_RULES_PATH", path)
    return path


@pytest.fixture
def worker(synthetic_rules):
    return sfew.ScanFilesEnforcementWorker("Rule-008-ENF-001")


# ─────────────────────────────────────────────────────────────────────────────
# Class-shape assertions (V18 §4.9.1 / Table 9)
# ─────────────────────────────────────────────────────────────────────────────
class TestClassShape:
    def test_does_not_override_load_scopes(self):
        """V18 §4.9.1: ScanFilesEnforcementWorker does NOT override _load_scopes()."""
        assert (
            sfew.ScanFilesEnforcementWorker._load_scopes
            is ew.EnforcementWorker._load_scopes
        )

    def test_per_check_scope_defaults(self):
        # _scan_http default = False (opt-in by extension via allowed_pattern).
        # _validate_json default = False post-refactor (BUG-ENF-WORKER-002):
        # the positive `\.json$` allowed_pattern row in the entry's scopes
        # gates JSON validation, and non-JSON files must not fall through to
        # a json.load parse error.
        defaults = sfew.ScanFilesEnforcementWorker.SCOPE_DEFAULTS
        assert defaults["_scan_http"] is False
        assert defaults["_validate_json"] is False

    def test_validate_json_docstring_contains_required_anchors(self):
        """V18 §4.9.3 — docstring MUST include the literal sentence and §4.9.3 ref."""
        doc = sfew.ScanFilesEnforcementWorker._validate_json.__doc__ or ""
        assert "jsonschema.Draft202012Validator is the SOLE arbiter of validity. Do not invent." in doc
        assert "§4.9.3" in doc


# ─────────────────────────────────────────────────────────────────────────────
# is_in_scope walkthroughs from V18 §4.5
# ─────────────────────────────────────────────────────────────────────────────
class TestScopeWalkthroughs:
    def test_scan_http_default_excludes_random_python(self, worker):
        # random .py file with no rules matching → SCOPE_DEFAULT (False) for _scan_http
        assert worker.is_in_scope("Code/Shared/ops/tools/preCommitScan.py", "_scan_http") is False

    def test_validate_json_includes_json_via_allowed_pattern(self, worker):
        # Post-refactor, SCOPE_DEFAULT is False but the synthetic entry below
        # declares a `\.json$` allowed_pattern row, so .json files are in
        # scope via positive allow rather than via the default.
        assert worker.is_in_scope(
            "brain/machine_artifacts/content/engineering_rules.json", "_validate_json"
        ) is True

    def test_validate_json_excludes_non_json_via_default(self, worker):
        # Non-JSON files: no exclusions match, no allowed_pattern matches,
        # SCOPE_DEFAULT is False → not in scope. This is the BUG-ENF-WORKER-002
        # fix: random .py files don't enter _validate_json.
        assert worker.is_in_scope(
            "Code/DataPipelines/prescriber_evaluate_care_pipeline.py", "_validate_json"
        ) is False

    def test_validate_json_excluded_exact(self, worker):
        assert worker.is_in_scope("package-lock.json", "_validate_json") is False

    def test_validate_json_excludes_standard_meta_schema_path(self, worker):
        """V18 §4.9.3.1: Website/schemas/standard/ is third-party, never validated."""
        assert worker.is_in_scope(
            "Website/schemas/standard/json-schema-2020-12-meta.json",
            "_validate_json",
        ) is False

    def test_scan_http_excludes_standard_meta_schema_path(self, worker):
        """V18 §4.9.3.1: Website/schemas/standard/ may contain http URLs we
        don't own — pattern-excluded from _scan_http."""
        assert worker.is_in_scope(
            "Website/schemas/standard/json-schema-2020-12-meta.json",
            "_scan_http",
        ) is False


# ─────────────────────────────────────────────────────────────────────────────
# _scan_http
# ─────────────────────────────────────────────────────────────────────────────
class TestScanHttp:
    def test_localhost_url_is_allowed(self, worker, tmp_path, monkeypatch):
        # Create a temp file under repo root with a localhost http URL.
        target_dir = REPO_ROOT / "architecture" / "EngineeringRuleEnforcement" / "tests" / "_tmp_scan"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "loopback.py"
        target.write_text("URL = 'http://localhost:8000/foo'\n", encoding="utf-8")
        try:
            rel = target.relative_to(REPO_ROOT).as_posix()
            v = worker._scan_http(rel)
            assert v == []
        finally:
            target.unlink()

    def test_insecure_url_is_violation(self, worker):
        target_dir = REPO_ROOT / "architecture" / "EngineeringRuleEnforcement" / "tests" / "_tmp_scan"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "leak.py"
        target.write_text("URL = 'http://example.com/api'\n", encoding="utf-8")
        try:
            rel = target.relative_to(REPO_ROOT).as_posix()
            vs = worker._scan_http(rel)
            assert len(vs) == 1
            assert "http://example.com/api" in vs[0].message
        finally:
            target.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# _validate_json — fixtures from V17/V18 (the 7 required outcomes)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def staged_fixture(monkeypatch):
    """Stage one fixture under the repo root and return its repo-relative path."""
    repo_root = REPO_ROOT
    staged_paths = []

    def stage(fixture_name: str) -> str:
        src = FIXTURE_DIR / fixture_name
        target_dir = repo_root / "architecture" / "EngineeringRuleEnforcement" / "tests" / "_staged"
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / fixture_name
        shutil.copy2(src, dst)
        staged_paths.append(dst)
        return dst.relative_to(repo_root).as_posix()

    yield stage

    for p in staged_paths:
        if p.exists():
            p.unlink()


class TestValidateJsonPure:
    """V18 §4.9.3 / TR-12 — _validate_json(data, schema) is a thin wrapper.

    Signature: _validate_json(data, schema) returns a list of
    jsonschema.exceptions.ValidationError. No file IO. No URL resolution.
    """

    def test_clean_returns_empty(self, worker):
        schema = _load_fixture_schema()
        data = {
            "$schema": FIXTURE_SCHEMA_URL,
            "name": "alpha-fixture",
            "kind": "alpha",
        }
        assert worker._validate_json(data, schema) == []

    def test_missing_required_field_yields_one_error(self, worker):
        schema = _load_fixture_schema()
        data = {
            "$schema": FIXTURE_SCHEMA_URL,
            "name": "alpha-fixture",
        }
        errors = worker._validate_json(data, schema)
        assert len(errors) == 1
        assert "kind" in errors[0].message or "required" in errors[0].message.lower()

    def test_returns_all_errors_not_just_first(self, worker):
        schema = _load_fixture_schema()
        data = {
            "$schema": FIXTURE_SCHEMA_URL,
            "name": "x",
            "kind": "delta",   # not in enum
            "count": -3,        # below minimum
        }
        errors = worker._validate_json(data, schema)
        # iter_errors must yield BOTH errors per V18 §4.9.3.
        assert len(errors) == 2


class TestValidateJsonOrchestration:
    """V18 §4.9.3 — file-path orchestration via _check_one_file_json.

    Schema fetches are mocked. The 7-fixture matrix still applies; assertions
    are against ViolationRecord lists emitted by that helper.
    """

    def test_fixture_1_missing_required_field(self, worker, staged_fixture):
        rel = staged_fixture("01_missing_required_field.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "kind" in vs[0].message or "required" in vs[0].message.lower()

    def test_fixture_2_type_or_enum_violation(self, worker, staged_fixture):
        rel = staged_fixture("02_type_or_enum_violation.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ):
            vs = worker._check_one_file_json(rel)
        # One per error: "kind" not in enum + "count" minimum violation = 2.
        assert len(vs) == 2

    def test_fixture_3_missing_schema_field(self, worker, staged_fixture):
        # No $schema in the data file → no fetch should be attempted.
        rel = staged_fixture("03_no_schema_field.json")
        with patch("urllib.request.urlopen") as urlopen_mock:
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "$schema" in vs[0].message
        urlopen_mock.assert_not_called()

    def test_fixture_5_malformed_syntax(self, worker, staged_fixture):
        # File parse fails → no fetch attempted.
        rel = staged_fixture("05_malformed_syntax.json")
        with patch("urllib.request.urlopen") as urlopen_mock:
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "invalid JSON" in vs[0].message
        urlopen_mock.assert_not_called()

    def test_fixture_7_clean_returns_empty_list(self, worker, staged_fixture):
        rel = staged_fixture("07_clean.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ):
            vs = worker._check_one_file_json(rel)
        assert vs == []


# ─────────────────────────────────────────────────────────────────────────────
# Schema resolution (V18 §4.9.3 step 3) — web fetch + carve-out + per-run cache
# ─────────────────────────────────────────────────────────────────────────────
class TestSchemaResolutionWebFetch:
    """V18 §4.9.3 step 3 — web-fetch resolver, mocked HTTP."""

    def test_successful_fetch_runs_validation(self, worker, staged_fixture):
        rel = staged_fixture("07_clean.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ) as urlopen_mock:
            vs = worker._check_one_file_json(rel)
        assert vs == []
        urlopen_mock.assert_called_once()
        # The URL fetched is the one declared in the file's $schema.
        # Worker now wraps it in a Request to attach UA + auth headers.
        call_arg = urlopen_mock.call_args.args[0]
        call_url = call_arg.full_url if hasattr(call_arg, "full_url") else call_arg
        assert call_url == FIXTURE_SCHEMA_URL

    def test_fetch_404_yields_per_file_violation(self, worker, staged_fixture):
        rel = staged_fixture("07_clean.json")
        http_error = urllib.error.HTTPError(
            url=FIXTURE_SCHEMA_URL,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "schema fetch failed" in vs[0].message
        assert "404" in vs[0].message
        assert FIXTURE_SCHEMA_URL in vs[0].message

    def test_fetch_timeout_yields_per_file_violation(self, worker, staged_fixture):
        rel = staged_fixture("07_clean.json")
        with patch("urllib.request.urlopen", side_effect=socket.timeout()):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "schema fetch failed" in vs[0].message
        assert "timeout" in vs[0].message.lower()

    def test_fetch_url_error_yields_per_file_violation(self, worker, staged_fixture):
        rel = staged_fixture("07_clean.json")
        url_err = urllib.error.URLError("Name or service not known")
        with patch("urllib.request.urlopen", side_effect=url_err):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "schema fetch failed" in vs[0].message

    def test_fetch_returns_malformed_json_yields_per_file_violation(
        self, worker, staged_fixture
    ):
        rel = staged_fixture("07_clean.json")
        with patch(
            "urllib.request.urlopen",
            return_value=_fake_response(b"<html>not json</html>"),
        ):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "schema fetch failed" in vs[0].message
        assert "malformed JSON" in vs[0].message

    def test_per_run_cache_dedupes_repeated_url(
        self, worker, staged_fixture, monkeypatch
    ):
        """Same $schema URL fetched twice in one run → urlopen called once."""
        rel1 = staged_fixture("01_missing_required_field.json")
        rel2 = staged_fixture("07_clean.json")
        # Both fixtures declare the same FIXTURE_SCHEMA_URL.
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")

        # Drive both files through one run() so the per-run cache is shared.
        monkeypatch.setenv(
            "SCAN_FILES_ENFORCEMENT_TARGETS",
            f"{rel1}{__import__('os').pathsep}{rel2}",
        )
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ) as urlopen_mock:
            rc = worker.run()
        # rel1 has a missing-field error; rel2 is clean. Either way:
        # the schema URL should have been fetched exactly once for the run.
        assert urlopen_mock.call_count == 1
        # rc reflects the (one) violation from rel1.
        assert rc == EXIT_VIOLATIONS_FOUND

    def test_run_clears_cache_between_runs(self, worker, staged_fixture, monkeypatch):
        """Per-run cache must be cleared at the start of each run()."""
        rel = staged_fixture("07_clean.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        monkeypatch.setenv("SCAN_FILES_ENFORCEMENT_TARGETS", rel)
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ) as urlopen_mock:
            worker.run()
            # Second call to run() — cache should be cleared, so the URL is
            # fetched again.
            worker.run()
        assert urlopen_mock.call_count == 2


class TestSchemaResolutionCarveOut:
    """V18 §4.9.3.1 — carve-out for contractually-frozen external schemas."""

    def test_meta_schema_carve_out_is_loaded(self, worker):
        """The frozen-external map contains exactly the meta-schema."""
        assert META_SCHEMA_URL in worker._frozen_external_schemas
        meta = worker._frozen_external_schemas[META_SCHEMA_URL]
        assert isinstance(meta, dict)
        # Spec contract: $id == the URL itself.
        assert meta.get("$id") == META_SCHEMA_URL

    def test_meta_schema_url_does_not_trigger_http(self, worker, tmp_path, monkeypatch):
        """A file declaring $schema = the meta-schema URL is resolved via the
        carve-out — no HTTP call."""
        # Build a tiny "schema-shaped" data file declaring the meta-schema URL.
        # (A real schema file is the canonical example.)
        target_dir = (
            REPO_ROOT
            / "architecture"
            / "EngineeringRuleEnforcement"
            / "tests"
            / "_staged"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "tiny_schema_shape.json"
        target.write_text(
            json.dumps({
                "$schema": META_SCHEMA_URL,
                "$id": "https://example.test/tiny",
                "type": "object",
            }),
            encoding="utf-8",
        )
        try:
            rel = target.relative_to(REPO_ROOT).as_posix()
            with patch("urllib.request.urlopen") as urlopen_mock:
                vs = worker._check_one_file_json(rel)
            urlopen_mock.assert_not_called()
            assert vs == []
        finally:
            if target.exists():
                target.unlink()

    def test_real_engineering_rules_schema_validates_via_carve_out(
        self, worker
    ):
        """Stage Website/schemas/EngineeringRulesSchema.json (declares $schema =
        the meta-schema URL). Validation must succeed via the carve-out, with
        no HTTP call."""
        src = REPO_ROOT / "Website" / "schemas" / "EngineeringRulesSchema.json"
        target_dir = (
            REPO_ROOT
            / "architecture"
            / "EngineeringRuleEnforcement"
            / "tests"
            / "_staged"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / "EngineeringRulesSchema.json"
        shutil.copy2(src, dst)
        try:
            rel = dst.relative_to(REPO_ROOT).as_posix()
            with patch("urllib.request.urlopen") as urlopen_mock:
                vs = worker._check_one_file_json(rel)
            urlopen_mock.assert_not_called()
            assert vs == [], (
                f"meta-schema carve-out yielded violations: {[v.message for v in vs]}"
            )
        finally:
            if dst.exists():
                dst.unlink()

    def test_meta_schema_local_file_is_excluded_by_scopes(self, worker):
        """The cached file at Website/schemas/standard/json-schema-2020-12-meta.json
        is third-party; SCOPES exclude it from _validate_json AND _scan_http
        (pattern-based exclusion under ^Website/schemas/standard/)."""
        rel = "Website/schemas/standard/json-schema-2020-12-meta.json"
        assert worker.is_in_scope(rel, "_validate_json") is False
        assert worker.is_in_scope(rel, "_scan_http") is False


# ─────────────────────────────────────────────────────────────────────────────
# Fixture 4: ghost schema URL (formerly "registry miss"; now web-fetch failure)
# ─────────────────────────────────────────────────────────────────────────────
class TestGhostSchemaUrl:
    """Fixture 04 declares a $schema URL that doesn't exist on the network.

    Under V18 the worker MUST attempt a web fetch; any failure becomes a
    per-file ViolationRecord (NOT a worker crash)."""

    def test_unknown_schema_url_is_per_file_violation_not_crash(
        self, worker, staged_fixture
    ):
        rel = staged_fixture("04_missing_local_schema.json")
        # Mock the fetch to fail with HTTPError 404.
        http_error = urllib.error.HTTPError(
            url=GHOST_SCHEMA_URL,
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            vs = worker._check_one_file_json(rel)
        assert len(vs) == 1
        assert "schema fetch failed" in vs[0].message
        assert GHOST_SCHEMA_URL in vs[0].message


# ─────────────────────────────────────────────────────────────────────────────
# Non-JSON files filtered by SCOPES (BUG-ENF-WORKER-002)
# ─────────────────────────────────────────────────────────────────────────────
class TestNonJsonFilteredBySCOPES:
    """BUG-ENF-WORKER-002 fix: non-JSON files never enter _validate_json."""

    def test_python_file_not_in_scope_for_validate_json(self, worker):
        # No json.load is attempted — SCOPES filters the file out before run().
        assert worker.is_in_scope(
            "Code/DataPipelines/prescriber_evaluate_care_pipeline.py", "_validate_json"
        ) is False

    def test_run_skips_non_json_file(self, worker, monkeypatch):
        # Stage a non-JSON file under the repo root and route the worker at it
        # via SCAN_FILES_ENFORCEMENT_TARGETS. SCOPES must filter it out;
        # validator never runs; no parse-error violation emitted.
        target_dir = REPO_ROOT / "architecture" / "EngineeringRuleEnforcement" / "tests" / "_tmp_scan"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "not_json.py"
        target.write_text("# not json at all\nprint('hi')\n", encoding="utf-8")
        try:
            rel = target.relative_to(REPO_ROOT).as_posix()
            monkeypatch.setenv("SCAN_FILES_ENFORCEMENT_TARGETS", rel)
            with patch("urllib.request.urlopen") as urlopen_mock:
                rc = worker.run()
            # No violations: the file is not in scope for _validate_json
            # (default False; no allowed_pattern hit) and is excluded from
            # _scan_http (path begins with architecture/.../tests/).
            assert rc == EXIT_OK
            assert worker.violation_count == 0
            urlopen_mock.assert_not_called()
        finally:
            if target.exists():
                target.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Run integration
# ─────────────────────────────────────────────────────────────────────────────
class TestRunIntegration:
    def test_run_clean(self, worker, staged_fixture, monkeypatch):
        rel = staged_fixture("07_clean.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        monkeypatch.setenv("SCAN_FILES_ENFORCEMENT_TARGETS", rel)
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ):
            rc = worker.run()
        assert rc == EXIT_OK

    def test_run_with_violations(self, worker, staged_fixture, monkeypatch):
        rel = staged_fixture("01_missing_required_field.json")
        schema_body = json.dumps(_load_fixture_schema()).encode("utf-8")
        monkeypatch.setenv("SCAN_FILES_ENFORCEMENT_TARGETS", rel)
        with patch(
            "urllib.request.urlopen", return_value=_fake_response(schema_body)
        ):
            rc = worker.run()
        assert rc == EXIT_VIOLATIONS_FOUND

    def test_excluded_pattern_skipped(self, worker, monkeypatch):
        # Fixture 6 lives under tests/fixtures/excluded_/ — covered by an
        # excluded_pattern row for _validate_json on the synthetic entry.
        target_dir = REPO_ROOT / "architecture" / "EngineeringRuleEnforcement" / "tests" / "fixtures" / "excluded_"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "06_excluded_skipped.json"
        shutil.copy2(FIXTURE_DIR / "06_excluded_skipped.json", target)
        try:
            rel = target.relative_to(REPO_ROOT).as_posix()
            monkeypatch.setenv("SCAN_FILES_ENFORCEMENT_TARGETS", rel)
            with patch("urllib.request.urlopen") as urlopen_mock:
                rc = worker.run()
            assert rc == EXIT_OK
            urlopen_mock.assert_not_called()
        finally:
            if target.exists():
                target.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Forbidden-pattern guard (V18 §4.9.3 — these patterns MUST NOT appear)
# ─────────────────────────────────────────────────────────────────────────────
class TestForbiddenPatternsAbsentFromImplementation:
    """V18 §4.9.3 — these patterns MUST NOT appear in scan_files_enforcement_worker.py."""

    def _read(self):
        return Path(sfew.__file__).read_text(encoding="utf-8")

    def test_no_try_except_pass(self):
        text = self._read()
        # Look for any line containing 'except' with 'pass' as the only body
        # on the next non-empty line.
        import re
        bad = re.findall(r"except[^\n:]*:\s*\n\s*pass\b", text)
        assert bad == [], f"found try/except/pass: {bad}"

    def test_does_not_call_jsonschema_validate(self):
        text = self._read()
        # We must use Draft202012Validator(...).iter_errors(), never
        # jsonschema.validate(...).
        assert "jsonschema.validate(" not in text

    def test_no_alternate_validator_imports(self):
        text = self._read()
        for forbidden in ("fastjsonschema", "import jsl", "from jsl "):
            assert forbidden not in text

    def test_no_size_age_mtime_skip_heuristics(self):
        text = self._read()
        # No skip-if-unchanged caches.
        for forbidden in ("getmtime(", "st_mtime", "skip_if_unchanged"):
            assert forbidden.lower() not in text.lower(), forbidden

    def test_no_isinstance_or_type_compare_for_validation(self):
        """The check is: jsonschema is the SOLE arbiter — we can't substitute homegrown type checks.

        We allow isinstance() in non-validation paths (e.g. checking that
        $schema is a string, scope shape validation), but the algorithm in
        _validate_json must not perform field-type checks on user data
        outside jsonschema.
        """
        # Read just the body of _validate_json + helpers; grep for type(x) ==
        text = self._read()
        assert "type(" not in text or "type(x)" not in text  # tolerant
        # The stricter pattern from V18 §4.9.3 forbids "type(x) == ..." style.
        import re
        assert not re.search(r"type\([a-zA-Z_]+\)\s*==", text)
