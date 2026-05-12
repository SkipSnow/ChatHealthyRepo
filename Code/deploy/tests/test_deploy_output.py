# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# test_deploy_output.py — DEVOPS-LOCAL-B011
# Validates that the deploy script produced a correct output file.
#
# Prerequisites: deploy_localhost.sh must have been run at least once.
# Run: python -m pytest Code/deploy/tests/test_deploy_output.py -v

import os
import glob
import pytest

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "_oneshots/test_output", "deploy")


def _get_latest_log():
    """Find the most recent deploy log file."""
    pattern = os.path.join(OUTPUT_DIR, "deploy_localhost_*.log")
    files = sorted(glob.glob(pattern))
    return files[-1] if files else None


@pytest.fixture(scope="module")
def log_content():
    path = _get_latest_log()
    assert path is not None, f"No deploy log found in {OUTPUT_DIR}"
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


@pytest.fixture(scope="module")
def log_path():
    path = _get_latest_log()
    assert path is not None, f"No deploy log found in {OUTPUT_DIR}"
    return path


class TestOutputFileExists:

    def test_output_dir_exists(self):
        assert os.path.isdir(OUTPUT_DIR), f"Output directory missing: {OUTPUT_DIR}"

    def test_log_file_exists(self):
        path = _get_latest_log()
        assert path is not None, f"No deploy log found in {OUTPUT_DIR}"

    def test_filename_has_timestamp(self, log_path):
        basename = os.path.basename(log_path)
        assert basename.startswith("deploy_localhost_"), f"Wrong filename prefix: {basename}"
        assert basename.endswith(".log"), f"Wrong extension: {basename}"
        # Timestamp format: YYYYMMDD_HHMMSS
        ts_part = basename.replace("deploy_localhost_", "").replace(".log", "")
        assert len(ts_part) == 15, f"Timestamp wrong length: {ts_part}"  # YYYYMMDD_HHMMSS


class TestOutputContainsResults:

    def test_contains_pass_fail_results(self, log_content):
        assert "PASS" in log_content, "Output missing PASS results"

    def test_contains_all_9_tests(self, log_content):
        for i in range(1, 10):
            assert f"[{i}/9]" in log_content, f"Missing test [{i}/9]"

    def test_contains_final_summary(self, log_content):
        assert "9/9 TESTS PASSED" in log_content or "TESTS FAILED" in log_content, \
            "Missing final test summary"

    def test_all_tests_passed(self, log_content):
        assert "ALL 9/9 TESTS PASSED" in log_content, \
            "Not all deploy tests passed — check log for failures"


class TestOutputContainsPIDs:

    def test_contains_website_pid(self, log_content):
        assert "PID=" in log_content, "Missing server PIDs"

    def test_contains_all_server_pids(self, log_content):
        assert "Website" in log_content and "PID=" in log_content
        assert "FindCare" in log_content
        assert "EvaluateCare" in log_content
        assert "CHShared" in log_content


class TestOutputContainsPorts:

    def test_contains_port_80(self, log_content):
        assert "Port 80" in log_content or ":80" in log_content

    def test_contains_port_443(self, log_content):
        assert "Port 443" in log_content or ":443" in log_content

    def test_contains_port_7860(self, log_content):
        assert "7860" in log_content

    def test_contains_port_8001(self, log_content):
        assert "8001" in log_content

    def test_contains_port_8002(self, log_content):
        assert "8002" in log_content


class TestOutputContainsSteps:

    def test_contains_kill_step(self, log_content):
        assert "[1/7]" in log_content, "Missing step 1 (kill)"

    def test_contains_wipe_step(self, log_content):
        assert "[2/7]" in log_content, "Missing step 2 (wipe)"

    def test_contains_validate_step(self, log_content):
        assert "[3/7]" in log_content, "Missing step 3 (validate)"

    def test_contains_build_step(self, log_content):
        assert "[4/7]" in log_content, "Missing step 4 (build)"

    def test_contains_start_step(self, log_content):
        assert "[5/7]" in log_content, "Missing step 5 (start)"

    def test_contains_verify_step(self, log_content):
        assert "[6/7]" in log_content, "Missing step 6 (verify)"


class TestOutputContainsTimestamp:

    def test_log_filename_is_timestamped(self, log_path):
        basename = os.path.basename(log_path)
        # deploy_localhost_20260416_134500.log
        assert "2026" in basename, f"No year in filename: {basename}"


class TestOutputContainsMTLS:

    def test_mtls_findcare_evaluatecare(self, log_content):
        assert "mTLS FindCare" in log_content and "EvaluateCare" in log_content

    def test_mtls_findcare_shared(self, log_content):
        assert "mTLS FindCare" in log_content and "CHShared" in log_content


class TestOutputContainsTeardown:

    def test_contains_port_status_before_teardown(self, log_content):
        """Each port must report running or empty before teardown."""
        for port in ["80", "443", "7860", "8001", "8002"]:
            assert f"Port {port}:" in log_content, f"Missing pre-teardown status for port {port}"

    def test_contains_process_counts(self, log_content):
        assert "Python processes:" in log_content, "Missing Python process count"
        assert "Node processes:" in log_content, "Missing Node process count"

    def test_contains_teardown_complete(self, log_content):
        assert "Teardown complete" in log_content, "Missing teardown completion"


class TestOutputContainsErrors:

    def test_no_failures_in_output(self, log_content):
        fail_count = log_content.count("FAIL [")
        assert fail_count == 0, f"Found {fail_count} failures in deploy output"

    def test_no_warnings_after_teardown(self, log_content):
        warning_count = log_content.count("WARNING: Port")
        assert warning_count == 0, f"Found {warning_count} ports still responding after teardown"
