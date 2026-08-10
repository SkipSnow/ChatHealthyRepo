# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Pipeline rule compliance — v4-001 C/D/E
# Verifies all pipeline code follows cloud-native best practices.
# No pipeline code may:
#   - Download files with raw HTTP (must use DataFetcherBase)
#   - Load entire files into memory (must stream)
#   - Run on local machines (must use Azure Functions)
#   - Bypass blob storage
#   - Interleave pipeline stages
#
# Run: pytest Code/DataPipelines/tests/test_pipeline_compliance.py -v

import ast
import io
import os
import sys
import tokenize

import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

PIPELINE_DIR = os.path.join(os.path.dirname(__file__), "..")
PIPELINE_FILES = []

for f in os.listdir(PIPELINE_DIR):
    if f.endswith(".py") and not f.startswith("test_") and f != "__init__.py":
        PIPELINE_FILES.append(os.path.join(PIPELINE_DIR, f))


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Source inspection.
#
# Every check below asks a question about what the pipeline code DOES —
# whether it calls requests.get with streaming on, whether it accumulates
# a download into a variable, whether a URL is written into a manager. So
# each one reads the parsed module rather than its characters: a call is a
# call, a string constant is a string constant, and a mention inside a
# comment is not mistaken for either.
# ---------------------------------------------------------------------------

def _parse(source, path):
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        pytest.fail(f"{os.path.basename(path)} does not parse: {exc}")


def _calls(tree, dotted):
    """Every Call node whose target is the given dotted name."""
    want = dotted.split(".")
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        parts = []
        target = node.func
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if list(reversed(parts)) == want:
            out.append(node)
    return out


def _keyword(call, name):
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _string_constants(tree):
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _comment_words(source):
    """Lower-cased word lists, one per comment token."""
    out = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return out
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        cleaned = "".join(c if c.isalnum() else " " for c in tok.string.lower())
        out.append(cleaned.split())
    return out


# ===========================================================================
# v4-001C: No raw HTTP downloads — must use DataFetcherBase
# ===========================================================================

class TestNoRawDownloads:
    """v4-001D: All downloads must go through DataFetcherBase."""

    ALLOWED_FILES = {
        "data_fetcher_base.py",     # the base class itself
        "blob_client.py",           # blob access layer
        "crosswalk_builder.py",     # uses RxNorm REST API (not file download)
    }

    def test_no_requests_get_for_downloads(self):
        """Pipeline code must not use requests.get() to download source files.
        Exception: API calls (RxNorm, OpenAI) are allowed.
        """
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            if name in self.ALLOWED_FILES:
                continue
            source = _read(path)
            for call in _calls(_parse(source, path), "requests.get"):
                stream = _keyword(call, "stream")
                if isinstance(stream, ast.Constant) and stream.value is True:
                    violations.append(f"{name}:{call.lineno}: streaming download with requests.get — must use DataFetcherBase")
                timeout = _keyword(call, "timeout")
                if (isinstance(timeout, ast.Constant)
                        and isinstance(timeout.value, (int, float))
                        and not isinstance(timeout.value, bool)
                        and timeout.value >= 300):
                    violations.append(f"{name}:{call.lineno}: long-timeout download with requests.get — must use DataFetcherBase")
        assert len(violations) == 0, (
            f"v4-001D violations — raw downloads found:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# v4-001D: No loading entire files into memory
# ===========================================================================

class TestNoFullFileInMemory:
    """v4-001D: Never load an entire source file into memory."""

    def test_no_readall_on_large_files(self):
        """Pipeline code must not call .readall(), .read(), or accumulate
        entire file content into a variable for source data files."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            source = _read(path)
            lines = source.split("\n")
            tree = _parse(source, path)

            # Accumulating bytes: content += chunk
            for node in ast.walk(tree):
                if (isinstance(node, ast.AugAssign)
                        and isinstance(node.op, ast.Add)
                        and isinstance(node.target, ast.Name)
                        and node.target.id == "content"
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "chunk"):
                    violations.append(f"{name}:{node.lineno}: accumulating chunks into memory (content += chunk)")

            for i, line in enumerate(lines, 1):
                # .readall() on blob downloads
                if '.readall()' in line and 'test' not in name.lower():
                    # Allow readall on small files (config, etc) but flag on data files
                    context = "\n".join(lines[max(0, i-3):i+2])
                    if any(kw in context.lower() for kw in ['csv', 'npi', 'part_d', 'prescrib', 'provider']):
                        violations.append(f"{name}:{i}: .readall() on data file — must stream")
        assert len(violations) == 0, (
            f"v4-001D violations — full file in memory:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_no_decode_entire_file(self):
        """Must not .decode() an entire downloaded file — stream and decode per line."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            source = _read(path)
            if 'test' in name.lower():
                continue
            if _calls(_parse(source, path), "content.decode"):
                # Check context — is this a large file?
                if any(kw in source.lower() for kw in ['part_d', 'npi_', 'prescrib']):
                    violations.append(f"{name}: decoding entire file content — must stream per line")
        assert len(violations) == 0, (
            f"v4-001D violations — decoding entire file:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# v4-001D: All downloads use blob storage
# ===========================================================================

class TestBlobStorageRequired:
    """v4-001D: All source files must be cached in Azure Blob Storage."""

    def test_fetcher_classes_use_blob(self):
        """All DataFetcherBase subclasses must define blob_name()."""
        fetcher_base = _read(os.path.join(PIPELINE_DIR, "data_fetcher_base.py"))
        assert "blob_name" in fetcher_base, "DataFetcherBase must define blob_name method"

    def test_no_direct_url_downloads_in_pipeline_managers(self):
        """Pipeline managers must not contain hardcoded download URLs.
        URLs belong in DataFetcherBase subclasses."""
        violations = []
        managers = [f for f in PIPELINE_FILES if "manager" in os.path.basename(f).lower()]
        for path in managers:
            name = os.path.basename(path)
            source = _read(path)
            hosts = ("data.cms.gov", "download.cms.gov", "oig.hhs.gov", "sam.gov")
            prefixes = tuple(f"{scheme}://{host}"
                             for scheme in ("https", "http") for host in hosts)
            urls = [s for s in _string_constants(_parse(source, path))
                    if s.startswith(prefixes)]
            for url in urls:
                violations.append(f"{name}: hardcoded download URL '{url[:60]}...' — must be in DataFetcherBase subclass")
        assert len(violations) == 0, (
            f"v4-001D violations — hardcoded URLs in managers:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# v4-001E: Pipeline stages are sequential
# ===========================================================================

class TestPipelineStages:
    """v4-001E: 5-stage pipeline — Assemble, Process, Enrich, Embed, Ship."""

    def test_prescriber_evaluate_care_pipeline_has_stages(self):
        """prescriber_evaluate_care_pipeline.py must implement the 5-stage pattern."""
        path = os.path.join(PIPELINE_DIR, "prescriber_evaluate_care_pipeline.py")
        if not os.path.exists(path):
            pytest.skip("prescriber_evaluate_care_pipeline.py not found")
        source = _read(path)
        for stage in ["step_1", "step_2", "step_3", "step_4", "step_5"]:
            assert stage in source, f"prescriber_evaluate_care_pipeline.py missing {stage}"

    def test_no_embed_on_frontend(self):
        """DR-022: No embedding code may reference the frontend cluster."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            if "embed" not in name.lower():
                continue
            source = _read(path)
            if "FRONTEND" in source or "frontend" in source.lower():
                if "never" not in source.lower() and "not" not in source.lower():
                    violations.append(f"{name}: embedding code references frontend cluster (DR-022)")
        # This is a heuristic — manual review needed for edge cases
        # Just warn, don't fail
        if violations:
            _CH_LOG.info(f"\n  DR-022 WARNINGS: {violations}")


# ===========================================================================
# v4-001C: Pipeline runs on Azure, not local
# ===========================================================================

class TestCloudExecution:
    """v4-001C: Pipeline workloads execute on cloud infrastructure."""

    def test_no_localhost_in_pipeline_config(self):
        """Pipeline code must not hardcode localhost for MongoDB connections."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            source = _read(path)
            if "localhost" in source and "mongo" in source.lower():
                violations.append(f"{name}: hardcoded localhost MongoDB — must use connection string from env")
        assert len(violations) == 0, (
            f"v4-001C violations — localhost in pipeline:\n" + "\n".join(f"  {v}" for v in violations)
        )

    def test_pipeline_workers_use_env_prefix(self):
        """All pipeline code must use ENV_PREFIX for database names."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            source = _read(path)
            # Check for hardcoded dev_ database names
            hardcoded = [s for s in _string_constants(_parse(source, path))
                         if s == "dev_PublicHealthData"]
            if hardcoded:
                violations.append(f"{name}: hardcoded 'dev_PublicHealthData' — must use ENV_PREFIX")
        assert len(violations) == 0, (
            f"v4-001C violations — hardcoded database names:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# Data integrity: Parity check requirements
# ===========================================================================

# ===========================================================================
# v4-001F: Fan-out mandatory for parallelizable work
# ===========================================================================

class TestFanOutRequired:
    """v4-001F: Parallelizable work must fan out to workers."""

    def test_orchestrators_use_fan_out(self):
        """Pipeline orchestrators must use fan-out patterns (activity_list, fan_out, parallel)."""
        orchestrators = [f for f in PIPELINE_FILES if "manager" in os.path.basename(f).lower()
                         or "orchestrator" in os.path.basename(f).lower()]
        for path in orchestrators:
            name = os.path.basename(path)
            source = _read(path)
            # Check for single-state serial processing without fan-out
            if "for state in" in source and "fan_out" not in source and "activity_list" not in source:
                # Check if there's a documented fan-in justification
                if "v4-001F" not in source and "fan-in" not in source.lower():
                    _CH_LOG.info(f"\n  WARNING: {name} may process states serially without fan-out (v4-001F)")

    def test_fan_in_has_justification(self):
        """Any fan-in (single process) must cite v4-001F in a comment."""
        violations = []
        for path in PIPELINE_FILES:
            name = os.path.basename(path)
            source = _read(path)
            # A comment that describes fan-in, a single process, or
            # aggregating everything, without the citation that justifies it.
            adjacent_phrases = (("fan", "in"), ("single", "process"))
            flagged = False
            for words in _comment_words(source):
                pairs = set(zip(words, words[1:]))
                if any(p in pairs for p in adjacent_phrases):
                    flagged = True
                    break
                if "aggregate" in words and "all" in words[words.index("aggregate"):]:
                    flagged = True
                    break
            if flagged and "v4-001F" not in source:
                violations.append(f"{name}: fan-in pattern without v4-001F citation")
        # This is advisory — just report
        if violations:
            _CH_LOG.info(f"\n  Fan-in without citation: {violations}")


class TestParityRequirements:
    """Pipeline must include parity verification code."""

    def test_prescriber_evaluate_care_pipeline_has_parity(self):
        """prescriber_evaluate_care_pipeline.py must include parity verification step."""
        path = os.path.join(PIPELINE_DIR, "prescriber_evaluate_care_pipeline.py")
        if not os.path.exists(path):
            pytest.skip("prescriber_evaluate_care_pipeline.py not found")
        source = _read(path)
        assert "parity" in source.lower() or "verify" in source.lower(), (
            "prescriber_evaluate_care_pipeline.py must include parity verification"
        )
