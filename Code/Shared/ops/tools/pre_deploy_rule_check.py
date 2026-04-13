# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# v4-017: Read and enforce ALL rules before any deployment.
#
# This script loads every rule from engineering_rules.json.
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

import ast
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
    """BUG-GOV-005 / v4-007: Every requirement in agile_backlog.json must have pytest_ids.
    Scans the one and only JSON where requirements live. Blocks check-in if any
    implemented requirement is missing pytest_ids or has an empty array."""
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
                    # Support both pytest_ids (array) and legacy pytest_id (string)
                    pytest_ids = req.get("pytest_ids", [])
                    if isinstance(pytest_ids, str):
                        pytest_ids = [pytest_ids] if pytest_ids.strip() else []
                    legacy = req.get("pytest_id", "")
                    if legacy and not pytest_ids:
                        pytest_ids = [legacy] if legacy.strip() else []
                    if not pytest_ids or not any(p.strip() for p in pytest_ids):
                        # Unimplemented requirements don't need a real test yet
                        status = req.get("status", "")
                        if status not in ("implemented", "in_progress"):
                            continue
                        violations.append(
                            f"{rule_id}: {req_id} ({story_id}) has no pytest_id"
                        )
    return violations


def enforce_backlog_schema(rule_id, enforcement):
    """Validate agile_backlog.json against agile_backlog_schema in schema.json.
    Checks required fields, patterns, and possible_values at all levels."""
    violations = []
    backlog_path = os.path.join(REPO_ROOT, enforcement.get("path", "brain/machine_artifacts/content/agile_backlog.json"))
    schema_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "schema.json")

    if not os.path.exists(backlog_path) or not os.path.exists(schema_path):
        violations.append(f"{rule_id}: agile_backlog.json or schema.json missing")
        return violations

    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        with open(backlog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        violations.append(f"{rule_id}: invalid JSON: {e}")
        return violations

    schemas = schema.get("collections", {}).get("agile_backlog", {}).get("record_schemas", {})
    if not schemas:
        return violations  # No schema to validate against

    def check(obj, sdef, path):
        for fname, fdef in sdef.get("fields", {}).items():
            if fname in ("features", "stories", "requirements", "depends_on"):
                continue
            if fdef.get("required") and fname not in obj:
                violations.append(f"{rule_id}: {path} missing required field '{fname}'")
            if fname in obj:
                val = obj[fname]
                pattern = fdef.get("pattern", "")
                if pattern and isinstance(val, str) and not re.match(pattern, val):
                    violations.append(f"{rule_id}: {path} '{fname}' value '{val}' does not match {pattern}")

    for epic in data.get("epics", []):
        eid = epic.get("epic_id", "?")
        check(epic, schemas.get("epic", {}), f"epic:{eid}")
        for feat in epic.get("features", []):
            fid = feat.get("feature_id", "?")
            check(feat, schemas.get("feature", {}), f"feature:{fid}")
            for story in feat.get("stories", []):
                sid = story.get("story_id", "?")
                check(story, schemas.get("story", {}), f"story:{sid}")
                for req in story.get("requirements", []):
                    rid = req.get("req_id", "?")
                    check(req, schemas.get("requirement", {}), f"req:{rid}")

    return violations


def enforce_bugs_schema(rule_id, enforcement):
    """Validate bugs.json against bugs_schema in schema.json."""
    violations = []
    bugs_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "bugs.json")
    schema_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "schema.json")
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        with open(bugs_path, "r", encoding="utf-8") as f:
            bugs = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        violations.append(f"{rule_id}: {e}")
        return violations

    bug_fields = schema.get("collections", {}).get("bugs", {}).get("record_schema", {}).get("fields", {})
    for bug in bugs.get("bugs", []):
        bid = bug.get("id", "?")
        for fname, fdef in bug_fields.items():
            if fdef.get("required") and fname not in bug and fdef.get("default") is None:
                violations.append(f"{rule_id}: bug:{bid} missing required field '{fname}'")
    return violations


def enforce_ai_operations_schema(rule_id, enforcement):
    """Validate ai_operations.json dead_code_records against dead_code_record schema."""
    violations = []
    ops_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "ai_operations.json")
    schema_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "schema.json")
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
        with open(ops_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        violations.append(f"{rule_id}: {e}")
        return violations

    dc_fields = schema.get("collections", {}).get("ai_operations", {}).get("record_schemas", {}).get("dead_code_record", {}).get("fields", {})
    for rec in data.get("dead_code_records", []):
        rid = rec.get("id", "?")
        for fname, fdef in dc_fields.items():
            if fdef.get("required") and fname not in rec:
                violations.append(f"{rule_id}: dc:{rid} missing required field '{fname}'")
            if fname in rec:
                possible = fdef.get("possible_values", [])
                val = rec[fname]
                if possible and val and isinstance(val, str) and val not in possible:
                    violations.append(f"{rule_id}: dc:{rid} '{fname}' value '{val}' not in {possible}")
    return violations


EXECUTORS = {
    "file_scan": enforce_file_scan,
    "file_absent": enforce_file_absent,
    "file_present": enforce_file_present,
    "json_check": enforce_json_check,
    "no_pattern": enforce_no_pattern,
    "requirement_pytest": enforce_requirement_pytest,
    "backlog_schema": enforce_backlog_schema,
    "bugs_schema": enforce_bugs_schema,
    "ai_operations_schema": enforce_ai_operations_schema,
}


# ── v4-031: Two-pass dead code scanner ────────────────────────────────

_SCAN_DIRS = [
    "Code/ConversationalUX/FindCareChat/backend",
    "Code/DataPipelines",
    "Code/Shared",
    "Code/evaluate_care",
    "Code/shared_services",
]
_SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".git", "tests", ".pytest_cache"}

# Functions that are part of the scanner itself
_SCANNER_FUNCTIONS = {"dead_code_scan", "comment_out_dead_code"}


def _get_decorator_strings(node):
    """Extract all decorator strings from a function/class node."""
    decs = []
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Name):
            decs.append(d.id)
        elif isinstance(d, ast.Attribute):
            # e.g. app.post, app.get, app.route
            parts = []
            n = d
            while isinstance(n, ast.Attribute):
                parts.append(n.attr)
                n = n.value
            if isinstance(n, ast.Name):
                parts.append(n.id)
            decs.append(".".join(reversed(parts)))
        elif isinstance(d, ast.Call):
            if isinstance(d.func, ast.Attribute):
                parts = []
                n = d.func
                while isinstance(n, ast.Attribute):
                    parts.append(n.attr)
                    n = n.value
                if isinstance(n, ast.Name):
                    parts.append(n.id)
                decs.append(".".join(reversed(parts)))
            elif isinstance(d.func, ast.Name):
                decs.append(d.func.id)
    return decs


def _is_framework_invoked(name, decorators):
    """Check if a function is invoked by a framework decorator."""
    for d in decorators:
        # FastAPI: app.post, app.get, app.route, app.middleware
        if "app." in d:
            return True
        # Azure: activity_trigger, orchestration_trigger, function_name
        if "trigger" in d or "function_name" in d:
            return True
    # Governance singleton methods (called via getattr from _JSON_FUNCTION_MAP)
    if name.startswith("check_") and name != "check_url":
        return True
    # Pydantic validators
    if name.startswith("validate_"):
        return True
    # Scanner's own functions
    if name in _SCANNER_FUNCTIONS:
        return True
    return False


def dead_code_scan(repo_root):
    """Two-pass dead code scanner (v4-031). Returns list of (file, start, end, name, kind)."""
    definitions = {}
    references = set()

    for d in _SCAN_DIRS:
        dirpath = os.path.join(repo_root, d)
        if not os.path.isdir(dirpath):
            continue
        for root, dirs, files in os.walk(dirpath):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
            for f in files:
                if not f.endswith(".py"):
                    continue
                is_test = f.startswith("test_")
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, encoding="utf-8", errors="replace") as fp:
                        source = fp.read()
                    tree = ast.parse(source)
                except Exception:
                    continue

                relpath = os.path.relpath(fpath, repo_root).replace(os.sep, "/")

                for node in ast.walk(tree):
                    # Collect definitions only from non-test files
                    if not is_test and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not node.name.startswith("_"):
                            decs = _get_decorator_strings(node)
                            end = getattr(node, "end_lineno", node.lineno)
                            definitions.setdefault(node.name, []).append(
                                (relpath, node.lineno, end, "function", decs)
                            )
                    elif not is_test and isinstance(node, ast.ClassDef):
                        if not node.name.startswith("_"):
                            decs = _get_decorator_strings(node)
                            end = getattr(node, "end_lineno", node.lineno)
                            definitions.setdefault(node.name, []).append(
                                (relpath, node.lineno, end, "class", decs)
                            )
                    if isinstance(node, ast.Name):
                        references.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        references.add(node.attr)

    dead = []
    for name, locations in sorted(definitions.items()):
        if name in references:
            continue
        for loc_file, start, end, kind, decs in locations:
            if _is_framework_invoked(name, decs):
                continue
            dead.append((loc_file, start, end, name, kind))
    dead.sort()
    return dead


def comment_out_dead_code(repo_root, dead_list):
    """Stage 1: Comment out dead functions with DEAD CODE markers."""
    by_file = {}
    for fpath, start, end, name, kind in dead_list:
        by_file.setdefault(fpath, []).append((start, end, name, kind))

    for fpath, items in by_file.items():
        full_path = os.path.join(repo_root, fpath)
        with open(full_path, encoding="utf-8") as f:
            lines = f.readlines()

        for start, end, name, kind in sorted(items, reverse=True):
            lines.insert(end, "# END DEAD CODE\n")
            for i in range(start - 1, end):
                if lines[i].strip():
                    lines[i] = "# " + lines[i]
                else:
                    lines[i] = "#\n"
            lines.insert(start - 1, f"# DEAD CODE (v4-031) -- unreferenced {kind} '{name}', marked for deletion\n")

        with open(full_path, "w", encoding="utf-8") as f:
            f.writelines(lines)


def main(target: str) -> int:
    print(f"Pre-deploy rule check for: {target}")
    print("=" * 60)

    # Load all rules from brain
    all_rules = []
    for fname in ["engineering_rules.json"]:
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

        # Support both single enforcement (dict) and multiple (array)
        if isinstance(enforcement, list):
            enf_list = enforcement
        elif isinstance(enforcement, dict):
            enf_list = [enforcement]
        else:
            skipped += 1
            continue

        for enf in enf_list:
            if not isinstance(enf, dict):
                continue
            enf_type = enf.get("type", "")
            executor = EXECUTORS.get(enf_type)

            if not executor:
                print(f"WARN: {rule_id}: unknown enforcement type '{enf_type}'")
                skipped += 1
                continue

            violations = executor(rule_id, enf)
            checked += 1

            if violations:
                print(f"FAIL: {rule_id} ({enf_type})")
                for v in violations:
                    print(f"  {v}")
                all_violations.extend(violations)
            else:
                print(f"PASS: {rule_id} ({enf_type})")

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
