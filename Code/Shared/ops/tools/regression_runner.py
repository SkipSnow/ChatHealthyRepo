# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# TEST-REG-001: Ordered regression test runner.
# Reads agile_backlog.json, runs pytests in hierarchy order.
#
# Usage:
#   python regression_runner.py --all
#   python regression_runner.py --epic EPIC-006
#   python regression_runner.py --feature FC-SEARCH
#   python regression_runner.py --story FC-SEARCH-001

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
BACKLOG_PATH = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "agile_backlog.json")
REPORT_PATH = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "regression_report.json")

# Test directories to search for test files
TEST_DIRS = [
    os.path.join(REPO_ROOT, "Code", "ConversationalUX", "FindCareChat", "backend", "tests"),
    os.path.join(REPO_ROOT, "Code", "evaluate_care", "tests"),
    os.path.join(REPO_ROOT, "Code", "DataPipelines", "tests"),
    os.path.join(REPO_ROOT, "Website", "tests"),
]


def load_backlog():
    with open(BACKLOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def collect_tests(backlog, scope_type, scope_value):
    """Collect pytest_ids grouped by hierarchy, filtered by scope."""
    tests = []

    for epic in backlog.get("epics", []):
        eid = epic.get("epic_id", "")
        if scope_type == "epic" and eid != scope_value:
            continue

        for feat in epic.get("features", []):
            fid = feat.get("feature_id", "")
            if scope_type == "feature" and fid != scope_value:
                continue

            for story in feat.get("stories", []):
                sid = story.get("story_id", "")
                if scope_type == "story" and sid != scope_value:
                    continue

                for req in story.get("requirements", []):
                    rid = req.get("req_id", "")
                    pytest_ids = req.get("pytest_ids", [])
                    status = req.get("status", "")

                    tests.append({
                        "epic_id": eid,
                        "epic_name": epic.get("name", ""),
                        "feature_id": fid,
                        "feature_name": feat.get("name", ""),
                        "story_id": sid,
                        "req_id": rid,
                        "requirement": req.get("requirement", "")[:100],
                        "pytest_ids": pytest_ids,
                        "status": status,
                    })

    return tests


def find_test_file(pytest_ref):
    """Resolve a pytest_id like 'test_foo.py::TestBar::test_baz' to a full path."""
    if not pytest_ref or pytest_ref == "NEEDS_TEST":
        return None
    fname = pytest_ref.split("::")[0]
    for d in TEST_DIRS:
        fpath = os.path.join(d, fname)
        if os.path.isfile(fpath):
            return fpath
    return None


def run_single_test(pytest_ref):
    """Run a single pytest reference. Returns (status, output)."""
    fpath = find_test_file(pytest_ref)
    if not fpath:
        return "not_found", f"Test file not found: {pytest_ref}"

    # Build the full pytest path
    parts = pytest_ref.split("::")
    fname = parts[0]
    test_path = fpath
    if len(parts) > 1:
        test_path = fpath + "::" + "::".join(parts[1:])

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_path, "-x", "--tb=short", "-q"],
            capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
        )
        if result.returncode == 0:
            return "pass", ""
        elif result.returncode == 5:
            return "skip", "No tests collected"
        else:
            # Extract failure info
            output = result.stdout + result.stderr
            return "fail", output[-500:] if len(output) > 500 else output
    except subprocess.TimeoutExpired:
        return "timeout", "Test timed out after 120s"
    except Exception as e:
        return "error", str(e)


def run_regression(scope_type="all", scope_value=None):
    backlog = load_backlog()
    tests = collect_tests(backlog, scope_type, scope_value)

    print(f"Regression Test Runner — scope: {scope_type}" + (f" {scope_value}" if scope_value else ""))
    print(f"Requirements found: {len(tests)}")
    print("=" * 70)

    results = []
    counts = {"pass": 0, "fail": 0, "skip": 0, "not_testable": 0, "not_found": 0, "error": 0, "timeout": 0}
    current_epic = ""
    current_feature = ""

    for t in tests:
        # Print hierarchy headers
        if t["epic_id"] != current_epic:
            current_epic = t["epic_id"]
            print(f"\n{'='*70}")
            print(f"EPIC: {t['epic_id']} — {t['epic_name']}")
            print(f"{'='*70}")
            current_feature = ""

        if t["feature_id"] != current_feature:
            current_feature = t["feature_id"]
            print(f"\n  Feature: {t['feature_id']} — {t['feature_name']}")
            print(f"  {'-'*60}")

        pytest_ids = t["pytest_ids"]
        if not pytest_ids or not any(p.strip() for p in pytest_ids):
            status = "not_testable"
            counts["not_testable"] += 1
            print(f"    {t['req_id']}: NOT TESTABLE (no pytest_ids)")
            results.append({**t, "result": "not_testable", "detail": ""})
            continue

        # Run each pytest_id for this requirement
        req_pass = True
        req_details = []
        for pid in pytest_ids:
            pid = pid.strip()
            if not pid or pid == "NEEDS_TEST":
                continue
            status, detail = run_single_test(pid)
            req_details.append({"pytest_id": pid, "result": status, "detail": detail})
            if status != "pass":
                req_pass = False

        overall = "pass" if req_pass else "fail"
        counts[overall] += 1
        marker = "PASS" if req_pass else "FAIL"
        print(f"    {t['req_id']}: {marker}")
        for d in req_details:
            if d["result"] != "pass":
                print(f"      {d['pytest_id']}: {d['result']}")

        results.append({**t, "result": overall, "test_details": req_details})

    # Summary
    print(f"\n{'='*70}")
    print(f"REGRESSION SUMMARY")
    print(f"{'='*70}")
    print(f"  Pass:         {counts['pass']}")
    print(f"  Fail:         {counts['fail']}")
    print(f"  Skip:         {counts['skip']}")
    print(f"  Not testable: {counts['not_testable']}")
    print(f"  Not found:    {counts['not_found']}")
    print(f"  Error:        {counts['error']}")
    print(f"  Timeout:      {counts['timeout']}")
    total_testable = counts["pass"] + counts["fail"]
    if total_testable > 0:
        print(f"  Pass rate:    {100*counts['pass']//total_testable}%")
    print(f"{'='*70}")

    # Write JSON report
    report = {
        "scope": scope_type,
        "scope_value": scope_value,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "counts": counts,
        "results": results,
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\nReport written to: {REPORT_PATH}")

    return counts["fail"] == 0


def main():
    parser = argparse.ArgumentParser(description="Ordered regression test runner")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="Run all tests")
    group.add_argument("--epic", type=str, help="Run tests for one epic (e.g. EPIC-006)")
    group.add_argument("--feature", type=str, help="Run tests for one feature (e.g. FC-SEARCH)")
    group.add_argument("--story", type=str, help="Run tests for one story (e.g. FC-SEARCH-001)")
    args = parser.parse_args()

    if args.all:
        success = run_regression("all")
    elif args.epic:
        success = run_regression("epic", args.epic)
    elif args.feature:
        success = run_regression("feature", args.feature)
    elif args.story:
        success = run_regression("story", args.story)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
