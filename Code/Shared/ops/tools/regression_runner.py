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
import asyncio
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
                        "test_method": req.get("test_method", ""),
                        "test_timeout_seconds": req.get("test_timeout_seconds", 0),
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


def _clean_pytest_ref(pytest_ref):
    """Clean up pytest_id references that have extra info like '(7 tests)'."""
    # Remove parenthetical counts: "TestHTTPRejection (7 tests)" -> "TestHTTPRejection"
    import re
    return re.sub(r'\s*\(\d+\s+tests?\)', '', pytest_ref).strip()


def _is_playwright_test(fpath):
    """Check if a test file uses Playwright (needs browser)."""
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            head = f.read(2000)
        return "playwright" in head.lower() or "from playwright" in head
    except Exception:
        return False


async def run_single_test_async(pytest_ref, test_method="", test_timeout=0, semaphore=None):
    """Run a single pytest reference asynchronously. Returns (pytest_ref, status, output)."""
    pytest_ref = _clean_pytest_ref(pytest_ref)
    fpath = find_test_file(pytest_ref)
    if not fpath:
        return pytest_ref, "not_found", f"Test file not found: {pytest_ref}"

    parts = pytest_ref.split("::")
    test_path = fpath
    if len(parts) > 1:
        test_path = fpath + "::" + "::".join(parts[1:])

    cmd = [sys.executable, "-m", "pytest", test_path, "-x", "--tb=short", "-q"]
    is_pw = test_method in ("playwright", "playwright+pytest") or _is_playwright_test(fpath)
    timeout = test_timeout if test_timeout > 0 else (180 if is_pw else 120)

    if is_pw:
        cmd.extend(["--browser", "chromium"])

    async with semaphore:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=REPO_ROOT,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = (stdout or b"").decode(errors="replace") + (stderr or b"").decode(errors="replace")
            if proc.returncode == 0:
                return pytest_ref, "pass", ""
            elif proc.returncode == 5:
                return pytest_ref, "skip", "No tests collected"
            else:
                return pytest_ref, "fail", output[-500:]
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return pytest_ref, "timeout", f"Test timed out after {timeout}s"
        except Exception as e:
            return pytest_ref, "error", str(e)


def run_regression(scope_type="all", scope_value=None, concurrency=4):
    backlog = load_backlog()
    tests = collect_tests(backlog, scope_type, scope_value)

    # Filter to only testable, implemented requirements
    testable = []
    not_testable = []
    for t in tests:
        status = t.get("status", "").lower()
        if status not in ("implemented", "in_progress"):
            not_testable.append({**t, "result": "not_implemented", "detail": ""})
            continue
        pytest_ids = t["pytest_ids"]
        if not pytest_ids or not any(p.strip() for p in pytest_ids):
            not_testable.append({**t, "result": "not_testable", "detail": ""})
            continue
        testable.append(t)

    print(f"Regression Test Runner -- scope: {scope_type}" + (f" {scope_value}" if scope_value else ""))
    print(f"Requirements: {len(tests)} total, {len(testable)} testable, {len(not_testable)} skipped")
    print(f"Concurrency: {concurrency}")
    print("=" * 70)

    # Run all testable requirements concurrently
    results = list(not_testable)  # Start with skipped ones
    counts = {"pass": 0, "fail": 0, "skip": 0, "not_testable": len([r for r in not_testable if r["result"] == "not_testable"]),
              "not_implemented": len([r for r in not_testable if r["result"] == "not_implemented"]),
              "not_found": 0, "error": 0, "timeout": 0}

    # Stream results as they complete
    req_results = {}  # req_id -> {req, details[]}
    completed_count = [0]
    total_tasks = sum(len([p for p in t["pytest_ids"] if p.strip() and p != "NEEDS_TEST"]) for t in testable)

    async def run_and_stream(t, pid, sem):
        pid = pid.strip()
        result = await run_single_test_async(pid, t.get("test_method", ""), t.get("test_timeout_seconds", 0), sem)
        _, status, detail = result
        completed_count[0] += 1
        # Stream: print immediately as each test completes
        marker = "PASS" if status == "pass" else status.upper()
        print(f"  [{completed_count[0]}/{total_tasks}] {t['req_id']} / {pid}: {marker}", flush=True)
        return t["req_id"], {"pytest_id": pid, "result": status, "detail": detail}

    async def run_all():
        sem = asyncio.Semaphore(concurrency)
        tasks = []
        for t in testable:
            for pid in t["pytest_ids"]:
                pid = pid.strip()
                if not pid or pid == "NEEDS_TEST":
                    continue
                tasks.append(run_and_stream(t, pid, sem))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                continue
            rid, detail = result
            req_results.setdefault(rid, []).append(detail)

    asyncio.run(run_all())

    # Build final results in hierarchy order
    current_epic = ""
    current_feature = ""
    print(f"\n{'='*70}")
    print("RESULTS BY HIERARCHY")
    print(f"{'='*70}")
    for t in testable:
        rid = t["req_id"]
        if t["epic_id"] != current_epic:
            current_epic = t["epic_id"]
            print(f"\n  EPIC: {t['epic_id']} -- {t['epic_name']}")
            current_feature = ""
        if t["feature_id"] != current_feature:
            current_feature = t["feature_id"]
            print(f"    Feature: {t['feature_id']} -- {t['feature_name']}")

        if rid in req_results:
            details = req_results[rid]
            req_pass = all(d["result"] == "pass" for d in details)
            overall = "pass" if req_pass else "fail"
            counts[overall] += 1
            marker = "PASS" if req_pass else "FAIL"
            print(f"      {rid}: {marker}")
            results.append({**t, "result": overall, "test_details": details})
        else:
            counts["not_found"] += 1
            print(f"      {rid}: NOT FOUND")
            results.append({**t, "result": "not_found", "detail": ""})

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
    parser.add_argument("--concurrency", type=int, default=16, help="Max concurrent tests (default 16, Windows pipe limit)")
    args = parser.parse_args()

    if args.all:
        success = run_regression("all", concurrency=args.concurrency)
    elif args.epic:
        success = run_regression("epic", args.epic, concurrency=args.concurrency)
    elif args.feature:
        success = run_regression("feature", args.feature, concurrency=args.concurrency)
    elif args.story:
        success = run_regression("story", args.story, concurrency=args.concurrency)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
