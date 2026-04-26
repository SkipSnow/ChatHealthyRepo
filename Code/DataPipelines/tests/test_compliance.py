# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""v4-001D compliance tests — scan pipeline source for violations."""
import os, pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
PIPELINE_DIR = os.path.join(REPO, "Code", "DataPipelines")

def _read(filename):
    with open(os.path.join(PIPELINE_DIR, filename), "r", encoding="utf-8") as f:
        return f.read()

def test_no_raw_requests_get():
    """EPIC-006-F-015-S-001-REQ-T-001: no requests.get in prescriber_evaluate_care_pipeline.py"""
    content = _read("prescriber_evaluate_care_pipeline.py")
    assert "requests.get(" not in content

def test_no_readall():
    """EPIC-006-F-015-S-001-REQ-T-002: no .readall() on full blob downloads in pipeline files.
    Bounded byte-range reads (offset+length) in partition logic are allowed."""
    EXEMPT_PATTERNS = ["download_blob(offset="]  # bounded reads are OK
    for fname in os.listdir(PIPELINE_DIR):
        if fname.endswith(".py") and not fname.startswith("test_"):
            content = _read(fname)
            for i, line in enumerate(content.split("\n"), 1):
                if ".readall()" in line:
                    # Check if the readall is on a bounded byte-range read
                    # Look at surrounding context (prev 3 lines)
                    lines = content.split("\n")
                    context = "\n".join(lines[max(0,i-4):i])
                    if any(p in context for p in EXEMPT_PATTERNS):
                        continue
                    pytest.fail(f".readall() found in {fname}:{i}: {line.strip()}")

def test_no_hardcoded_urls():
    """EPIC-006-F-015-S-001-REQ-T-003: CMS URL loaded from env/config, not used as bare literal"""
    content = _read("prescriber_evaluate_care_pipeline.py")
    # The URL must appear ONLY inside the CMS_PART_D_URL config assignment.
    # Verify: CMS_PART_D_URL is defined via os.environ.get, and the code uses
    # CMS_PART_D_URL (the variable) not a bare URL string.
    assert "CMS_PART_D_URL = os.environ.get(" in content, \
        "CMS_PART_D_URL must be loaded from os.environ.get"
    # Verify the URL is never used as a bare literal outside the config block
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"'):
            continue
        if "https://data.cms.gov" in stripped:
            # OK if it's part of the os.environ.get default value
            if "os.environ.get" in stripped or "CMS_PART_D_URL" in stripped:
                continue
            pytest.fail(f"Hardcoded CMS URL used outside config: {stripped}")

def test_quality_gate_imported():
    """EPIC-006-F-015-S-001-REQ-T-004"""
    content = _read("prescriber_evaluate_care_pipeline.py")
    assert "from quality_gate import" in content or "import quality_gate" in content

def test_bell_ringer_imported():
    """EPIC-006-F-015-S-001-REQ-T-005: pipeline imports BellRinger from
    architecture/DevOpsBuildDeployAndEnvironmentManagement/bell_ringer.py"""
    content = _read("prescriber_evaluate_care_pipeline.py")
    assert "from bell_ringer import" in content or "import bell_ringer" in content
