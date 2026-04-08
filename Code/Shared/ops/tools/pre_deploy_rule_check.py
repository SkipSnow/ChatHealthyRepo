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
    """Scan files for a regex pattern. Fail if found."""
    violations = []
    pattern = enforcement.get("pattern", "")
    if not pattern:
        return violations
    dirs = enforcement.get("scan_dirs", [])
    context_pattern = enforcement.get("context_pattern", "")
    context_lines = enforcement.get("context_lines", 0)
    exclude_comments = enforcement.get("exclude_comments", True)
    file_filter = enforcement.get("file_filter", "")

    exempt_files = enforcement.get("exempt_files", [])

    for d in dirs:
        for fpath in _get_py_files(d) if not enforcement.get("all_files") else _get_all_files(d):
            basename = os.path.basename(fpath)
            if basename in exempt_files:
                continue
            if file_filter and not re.search(file_filter, basename):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if exclude_comments and stripped.startswith("#"):
                    continue
                if re.search(pattern, stripped):
                    if context_pattern and context_lines > 0:
                        ctx = "".join(lines[max(0, i - context_lines - 1):min(len(lines), i + context_lines)])
                        if not re.search(context_pattern, ctx):
                            continue
                    # Check exempt patterns
                    exempt = enforcement.get("exempt_patterns", [])
                    if exempt:
                        ctx = "".join(lines[max(0, i - 5):i])
                        if any(p in ctx for p in exempt):
                            continue
                    violations.append(f"{rule_id}: {os.path.basename(fpath)}:{i}")
    return violations


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


EXECUTORS = {
    "file_scan": enforce_file_scan,
    "file_absent": enforce_file_absent,
    "file_present": enforce_file_present,
    "json_check": enforce_json_check,
    "no_pattern": enforce_no_pattern,
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

        if not enforcement:
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
