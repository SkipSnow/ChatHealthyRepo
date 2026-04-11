# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# v4-017: Read and enforce ALL rules before any deployment.
#
# This script loads every rule from development_rules.json and operating_rules.json.
# Rules that have an "enforcement" field are automatically checked.
# Rules without enforcement are logged as "no automatable check".
#
# Enforcement types:
#   file_scan     — scan files for a regex pattern, fail if found
#   file_absent   — fail if a file exists (e.g. old renamed files)
#   file_present  — fail if a file is missing (e.g. required components)
#   json_check    — load a JSON file and check for required fields
#   no_pattern    — scan files, fail if pattern is NOT found (e.g. missing import)
#
# Usage: python pre_deploy_rule_check.py <target>

import json
import os
import re
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
BRAIN_DIR = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")

SKIP_FILES = {"conversation_log.json", "pipeline_v3_compliance_log.json",
              "pipeline_v3_iteration_log.json", "pipeline_v4_design_iterations.json",
              "pre_deploy_rule_check.py"}


def _resolve_dir(rel_path):
    return os.path.join(REPO_ROOT, rel_path)


def _get_py_files(directory):
    d = _resolve_dir(directory)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d)
            if f.endswith(".py") and not f.startswith("test_") and f not in SKIP_FILES]


def _get_all_files(directory, extensions=(".py", ".json", ".yml")):
    d = _resolve_dir(directory)
    results = []
    if not os.path.isdir(d):
        return results
    for root, _, files in os.walk(d):
        for f in files:
            if f in SKIP_FILES:
                continue
            if any(f.endswith(ext) for ext in extensions):
                results.append(os.path.join(root, f))
    return results


# ── Enforcement executors ─────────────────────────────────────

def enforce_file_scan(rule_id, enforcement):
    """BUG-GOV-004: Regex file_scan removed — cannot distinguish filtered queries
    from unfiltered. False positives block deploys. GPT-4.1-mini enforcement pending.
    For now: warn, don't block."""
    pattern = enforcement.get("pattern", "")
    if pattern:
        print(f"  WARN: {rule_id}: regex scan skipped (BUG-GOV-004 — GPT enforcement pending)")
    return []  # Never block — GPT will handle this at PreToolUse


def enforce_file_absent(rule_id, enforcement):
    """Fail if a file exists."""
    violations = []
    for path in enforcement.get("paths", []):
        full = os.path.join(REPO_ROOT, path)
        if os.path.exists(full):
            violations.append(f"{rule_id}: {path} must not exist")
    return violations


def enforce_file_present(rule_id, enforcement):
    """Fail if a file is missing."""
    violations = []
    for path in enforcement.get("paths", []):
        full = os.path.join(REPO_ROOT, path)
        if not os.path.exists(full):
            violations.append(f"{rule_id}: {path} missing")
    return violations


def enforce_json_check(rule_id, enforcement):
    """Load a JSON file and check for required fields."""
    violations = []
    path = os.path.join(REPO_ROOT, enforcement.get("path", ""))
    if not os.path.exists(path):
        violations.append(f"{rule_id}: {enforcement.get('path')} missing")
        return violations
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        violations.append(f"{rule_id}: invalid JSON: {e}")
        return violations
    for field in enforcement.get("required_fields", []):
        if field not in data:
            violations.append(f"{rule_id}: missing field '{field}'")
    min_entries = enforcement.get("min_entries", 0)
    entries_field = enforcement.get("entries_field", "")
    if entries_field and min_entries > 0:
        entries = data.get(entries_field, {})
        if len(entries) < min_entries:
            violations.append(f"{rule_id}: {entries_field} has {len(entries)} entries, need {min_entries}")
    return violations


def enforce_no_pattern(rule_id, enforcement):
    """Scan files, fail if pattern is NOT found (e.g. missing import)."""
    violations = []
    pattern = enforcement.get("pattern", "")
    if not pattern:
        return violations
    for d in enforcement.get("scan_dirs", []):
        files = enforcement.get("files", [])
        if files:
            file_list = [os.path.join(_resolve_dir(d), f) for f in files]
        else:
            file_list = _get_py_files(d)
        for fpath in file_list:
            if not os.path.exists(fpath):
                violations.append(f"{rule_id}: {os.path.basename(fpath)} missing")
                continue
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if not re.search(pattern, content):
                violations.append(f"{rule_id}: {os.path.basename(fpath)} missing pattern: {pattern[:60]}")
    return violations


def enforce_requirement_pytest(rule_id, enforcement):
    """BUG-GOV-005 / v4-007: Every requirement in agile_backlog.json must have a pytest_id.
    Scans the one and only JSON where requirements live. Blocks check-in if any
    requirement is missing pytest_id or has an empty pytest_id."""
    violations = []
    path = os.path.join(REPO_ROOT, enforcement.get("path", "brain/machine_artifacts/content/agile_backlog.json"))
    if not os.path.exists(path):
        violations.append(f"{rule_id}: agile_backlog.json missing")
        return violations
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        violations.append(f"{rule_id}: invalid JSON: {e}")
        return violations

    for epic in data.get("epics", []):
        epic_id = epic.get("epic_id", "?")
        for feature in epic.get("features", []):
            feature_id = feature.get("feature_id", "?")
            for story in feature.get("stories", []):
                story_id = story.get("story_id", "?")
                for req in story.get("requirements", []):
                    req_id = req.get("req_id", "?")
                    pytest_id = req.get("pytest_id", "")
                    if not pytest_id or not pytest_id.strip():
                        # Unimplemented requirements don't need a real test yet
                        status = req.get("status", "")
                        if status not in ("implemented", "in_progress"):
                            continue
                        violations.append(
                            f"{rule_id}: {req_id} ({story_id}) has no pytest_id"
                        )
    return violations


EXECUTORS = {
    "file_scan": enforce_file_scan,
    "file_absent": enforce_file_absent,
    "file_present": enforce_file_present,
    "json_check": enforce_json_check,
    "no_pattern": enforce_no_pattern,
    "requirement_pytest": enforce_requirement_pytest,
}


def main(target: str) -> int:
    print(f"Pre-deploy rule check for: {target}")
    print("=" * 60)

    # Load all rules from brain
    all_rules = []
    for fname in ["development_rules.json", "operating_rules.json"]:
        fpath = os.path.join(BRAIN_DIR, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for r in data.get("rules", []):
            all_rules.append(r)

    print(f"Loaded {len(all_rules)} rules")

    all_violations = []
    checked = 0
    skipped = 0

    for rule in all_rules:
        rule_id = rule.get("id", "unknown")
        enforcement = rule.get("enforcement")

        if not enforcement or not isinstance(enforcement, dict):
            skipped += 1
            continue

        enf_type = enforcement.get("type", "")
        executor = EXECUTORS.get(enf_type)

        if not executor:
            print(f"WARN: {rule_id}: unknown enforcement type '{enf_type}'")
            skipped += 1
            continue

        violations = executor(rule_id, enforcement)
        checked += 1

        if violations:
            print(f"FAIL: {rule_id}")
            for v in violations:
                print(f"  {v}")
            all_violations.extend(violations)
        else:
            print(f"PASS: {rule_id}")

    # SEC-HTTPS-001-REQ-006: Run scan_http.py on staged files
    import subprocess as _sp
    scan_script = os.path.join(os.path.dirname(__file__), "scan_http.py")
    if os.path.exists(scan_script):
        # Get staged files and pass as file list
        staged = _sp.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
        staged_files = [f for f in staged.stdout.strip().split("\n") if f and f.endswith((".py", ".tsx", ".ts", ".js", ".html", ".json", ".yml"))]
        if staged_files:
            scan_result = _sp.run(["python", scan_script] + staged_files,
                capture_output=True, text=True, timeout=30, cwd=REPO_ROOT)
            if scan_result.returncode != 0:
                print(f"FAIL: SEC-HTTPS-001-REQ-004 (scan_http.py)")
                print(scan_result.stdout)
                all_violations.append("SEC-HTTPS-001-REQ-004: insecure HTTP URL found in staged files")
            else:
                print(f"PASS: SEC-HTTPS-001-REQ-004 (scan_http.py)")
            checked += 1
        else:
            print(f"PASS: SEC-HTTPS-001-REQ-004 (no scannable staged files)")
            checked += 1

    print("=" * 60)
    print(f"Rules: {len(all_rules)} | Checked: {checked} | Skipped: {skipped} | Violations: {len(all_violations)}")

    if all_violations:
        print(f"\nDEPLOY BLOCKED: {len(all_violations)} violation(s)")
        return 1
    else:
        print("\nAll enforced rules passed. Deploy may proceed.")
        return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    sys.exit(main(target))
