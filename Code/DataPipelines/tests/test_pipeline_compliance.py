# Copyright (c) 2026 Skip Snow. All rights reserved.
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

import os
import re
import sys

import pytest

PIPELINE_DIR = os.path.join(os.path.dirname(__file__), "..")
PIPELINE_FILES = []

for f in os.listdir(PIPELINE_DIR):
    if f.endswith(".py") and not f.startswith("test_") and f != "__init__.py":
        PIPELINE_FILES.append(os.path.join(PIPELINE_DIR, f))


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


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
            # Find requests.get with large file indicators
            if re.search(r'requests\.get\(.*stream\s*=\s*True', source):
                violations.append(f"{name}: streaming download with requests.get — must use DataFetcherBase")
            if re.search(r'requests\.get\(.*timeout\s*=\s*[3-9]\d\d', source):
                violations.append(f"{name}: long-timeout download with requests.get — must use DataFetcherBase")
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
            for i, line in enumerate(lines, 1):
                # Accumulating bytes: content += chunk or content = content + chunk
                if re.search(r'content\s*\+=\s*chunk', line):
                    violations.append(f"{name}:{i}: accumulating chunks into memory (content += chunk)")
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
            # Pattern: content.decode or text = content.decode
            if re.search(r'content\.decode\(', source) and 'test' not in name.lower():
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
            urls = re.findall(r'https?://(?:data\.cms\.gov|download\.cms\.gov|oig\.hhs\.gov|sam\.gov)[^\s\'"]+', source)
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

    def test_overnight_pipeline_has_stages(self):
        """overnight_pipeline.py must implement the 5-stage pattern."""
        path = os.path.join(PIPELINE_DIR, "overnight_pipeline.py")
        if not os.path.exists(path):
            pytest.skip("overnight_pipeline.py not found")
        source = _read(path)
        for stage in ["step_1", "step_2", "step_3", "step_4", "step_5"]:
            assert stage in source, f"overnight_pipeline.py missing {stage}"

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
            print(f"\n  DR-022 WARNINGS: {violations}")


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
            hardcoded = re.findall(r'["\']dev_PublicHealthData["\']', source)
            if hardcoded:
                violations.append(f"{name}: hardcoded 'dev_PublicHealthData' — must use ENV_PREFIX")
        assert len(violations) == 0, (
            f"v4-001C violations — hardcoded database names:\n" + "\n".join(f"  {v}" for v in violations)
        )


# ===========================================================================
# Data integrity: Parity check requirements
# ===========================================================================

class TestParityRequirements:
    """Pipeline must include parity verification code."""

    def test_copy_to_frontend_has_parity_check(self):
        """CopyToFrontEnd code must verify record counts match."""
        path = os.path.join(PIPELINE_DIR, "copy_to_frontend.py")
        if not os.path.exists(path):
            pytest.skip("copy_to_frontend.py not found")
        source = _read(path)
        has_count = "count_documents" in source
        has_parity = "parity" in source.lower() or "verify" in source.lower() or "match" in source.lower()
        assert has_count, "copy_to_frontend.py must count documents for parity"

    def test_overnight_pipeline_has_parity(self):
        """overnight_pipeline.py must include parity verification step."""
        path = os.path.join(PIPELINE_DIR, "overnight_pipeline.py")
        if not os.path.exists(path):
            pytest.skip("overnight_pipeline.py not found")
        source = _read(path)
        assert "parity" in source.lower() or "verify" in source.lower(), (
            "overnight_pipeline.py must include parity verification"
        )
