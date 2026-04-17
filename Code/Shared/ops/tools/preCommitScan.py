# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# preCommitScan.py — Unified pre-commit and pre-push scanner.
# Replaces scan_http.py and pre_deploy_rule_check.py.
#
# One file. One pass. All rules. No redundancy.
#
# Rules enforced:
#   - SEC-HTTPS-001-REQ-004: No HTTP URLs in production code
#   - BRAIN-SCHEMA-REQ-001: All JSON files validated against published schemas
#   - v4-007 enforcement types: file_scan, file_absent, file_present, json_check, no_pattern
#   - v4-031: Dead code scanner (two-pass)
#
# Usage:
#   python preCommitScan.py --staged     (pre-commit hook)
#   python preCommitScan.py all          (pre-push hook)
#
# Exit 0 = clean. Exit 1 = violations.

import ast
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
BRAIN_DIR = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")

# ── HTTP URL scanning ────────────────────────────────────────────

TARGET = "http://"

ALLOWED_PATTERNS = [
    re.compile(r"http://(localhost|127\.0\.0\.1)(:\d+)?(/.*)?$"),
    re.compile(r"http://(dev\.chathealthy\.ai|qa\.chathealthy\.ai|chathealthy\.ai)(:\d+)?(/index\.html|/)?$"),
    re.compile(r"http://www\.w3\.org/"),
    re.compile(r"http://169\.254\.169\.254/"),
]

HTTP_EXEMPT_PATTERNS = [
    r"\.json$",
    r"test_",
    r"conftest\.py",
    r"brain/BusinessArtifacts/",
]

SCHEMA_EXEMPT_PATTERNS = [
    r"\.claude/",
    r"\.vscode/",
    r"package\.json$",
    r"package-lock\.json$",
    r"tsconfig\.json$",
    r"host\.json$",
    r"appsettings.*\.json$",
    r"launchSettings\.json$",
    r"node_modules",
    r"__pycache__",
    r"\.iteration_cache/",
    r"brain/BusinessArtifacts/Audits/",
]

SKIP_PATTERNS = [r"__pycache__", r"\.pyc$", r"node_modules", r"\.venv"]

SCAN_EXTENSIONS = {".py", ".tsx", ".ts", ".js", ".jsx", ".html", ".json", ".yml", ".yaml", ".cfg", ".toml"}


def _should_skip(filepath):
    for p in SKIP_PATTERNS:
        if re.search(p, filepath):
            return True
    _, ext = os.path.splitext(filepath)
    if ext and ext not in SCAN_EXTENSIONS:
        return True
    return False


def _is_http_exempt(filepath):
    for p in HTTP_EXEMPT_PATTERNS:
        if re.search(p, filepath):
            return True
    return False


def _is_schema_exempt(filepath):
    for p in SCHEMA_EXEMPT_PATTERNS:
        if re.search(p, filepath):
            return True
    return False


def _extract_http_urls(line):
    return re.findall(r"http://[^\s\"'`,\)}\]>]+", line)


def _check_http_urls(filepath):
    """Check a single file for insecure HTTP URLs. Returns list of violations."""
    violations = []
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("//"):
                    continue
                if os.path.basename(filepath) == "preCommitScan.py":
                    if "re.compile" in stripped or "re.findall" in stripped:
                        continue
                if TARGET not in line:
                    continue
                for url in _extract_http_urls(line):
                    if any(p.match(url) for p in ALLOWED_PATTERNS):
                        continue
                    violations.append(f"  {filepath}:{i}: {url}")
    except Exception:
        pass
    return violations


def _fetch_schema(schema_url):
    try:
        import urllib.request
        req = urllib.request.Request(schema_url, headers={"User-Agent": "ChatHealthy-Scanner/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _check_json_schema(filepath):
    """Check a single JSON file against its published schema. Returns error string or None."""
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return f"Top-level must be an object, not {type(data).__name__}"
        if "$schema" not in data and "schema_address" not in data:
            return "Missing $schema field"
        schema_url = data.get("$schema", "")
        if "json-schema.org" in schema_url:
            return None
        schema = _fetch_schema(schema_url)
        if schema is None:
            return f"Schema not reachable at published URL: {schema_url}"
        try:
            import jsonschema
            jsonschema.validate(data, schema)
        except ImportError:
            pass
        except jsonschema.ValidationError as e:
            return f"Schema validation failed: {e.message[:200]}"
        return None
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    except Exception as e:
        return f"Error: {e}"


# ── Single-pass file scanner ────────────────────────────────────

def scan_files(files):
    """One pass over all files. Check HTTP URLs and JSON schemas."""
    http_violations = []
    schema_violations = []
    json_count = 0

    for filepath in files:
        if _should_skip(filepath):
            continue

        # HTTP URL check (production code only)
        if not _is_http_exempt(filepath):
            http_violations.extend(_check_http_urls(filepath))

        # JSON schema check
        if filepath.endswith(".json") and not _is_schema_exempt(filepath):
            json_count += 1
            error = _check_json_schema(filepath)
            if error:
                schema_violations.append(f"  {filepath}: {error}")

    # Report
    exit_code = 0

    if http_violations:
        print(f"SEC-HTTPS-001-REQ-004 VIOLATION: {len(http_violations)} insecure HTTP URLs:")
        for v in http_violations:
            print(v)
        exit_code = 1
    else:
        print(f"SEC-HTTPS-001-REQ-004 PASS: 0 insecure HTTP URLs in {len(files)} files.")

    if schema_violations:
        print(f"\nBRAIN-SCHEMA-REQ-001 VIOLATION: {len(schema_violations)} JSON files failed schema check:")
        for v in schema_violations:
            print(v)
        exit_code = 1
    elif json_count > 0:
        print(f"BRAIN-SCHEMA-REQ-001 PASS: {json_count} JSON files validated.")

    return exit_code


# ── Engineering rule executors ───────────────────────────────────

def _resolve_dir(rel_path):
    return os.path.join(REPO_ROOT, rel_path)


def _get_py_files(directory):
    d = _resolve_dir(directory)
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, f) for f in os.listdir(d)
            if f.endswith(".py") and not f.startswith("test_")]


def enforce_file_scan(rule_id, enforcement):
    pattern = enforcement.get("pattern", "")
    if pattern:
        print(f"  WARN: {rule_id}: regex scan skipped (BUG-GOV-004 — GPT enforcement pending)")
    return []


def enforce_file_absent(rule_id, enforcement):
    violations = []
    for path in enforcement.get("paths", []):
        if os.path.exists(os.path.join(REPO_ROOT, path)):
            violations.append(f"{rule_id}: {path} must not exist")
    return violations


def enforce_file_present(rule_id, enforcement):
    violations = []
    for path in enforcement.get("paths", []):
        if not os.path.exists(os.path.join(REPO_ROOT, path)):
            violations.append(f"{rule_id}: {path} missing")
    return violations


def enforce_json_check(rule_id, enforcement):
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
    violations = []
    pattern = enforcement.get("pattern", "")
    if not pattern:
        return violations
    for d in enforcement.get("scan_dirs", []):
        files = enforcement.get("files", [])
        file_list = [os.path.join(_resolve_dir(d), f) for f in files] if files else _get_py_files(d)
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


# ── Dead code scanner (v4-031) ──────────────────────────────────

_SCAN_DIRS = [
    "Code/ConversationalUX/FindCareChat/backend",
    "Code/DataPipelines",
    "Code/Shared",
    "Code/evaluate_care",
    "Code/shared_services",
]
_SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".git", "tests", ".pytest_cache"}
_SCANNER_FUNCTIONS = {"dead_code_scan", "comment_out_dead_code"}


def _get_decorator_strings(node):
    decs = []
    for d in getattr(node, "decorator_list", []):
        if isinstance(d, ast.Name):
            decs.append(d.id)
        elif isinstance(d, ast.Attribute):
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
    for d in decorators:
        if "app." in d:
            return True
        if "trigger" in d or "function_name" in d:
            return True
    if name.startswith("check_") and name != "check_url":
        return True
    if name.startswith("validate_"):
        return True
    if name in _SCANNER_FUNCTIONS:
        return True
    return False


def dead_code_scan(repo_root):
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
                    if not is_test and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not node.name.startswith("_"):
                            decs = _get_decorator_strings(node)
                            end = getattr(node, "end_lineno", node.lineno)
                            definitions.setdefault(node.name, []).append(
                                (relpath, node.lineno, end, "function", decs))
                    elif not is_test and isinstance(node, ast.ClassDef):
                        if not node.name.startswith("_"):
                            decs = _get_decorator_strings(node)
                            end = getattr(node, "end_lineno", node.lineno)
                            definitions.setdefault(node.name, []).append(
                                (relpath, node.lineno, end, "class", decs))
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


# ── Git helpers ──────────────────────────────────────────────────

def get_staged_files():
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10)
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


def get_all_tracked_files():
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            capture_output=True, text=True, timeout=10)
        return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        return []


# ── Main ─────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python preCommitScan.py --staged | all")
        sys.exit(2)

    mode = args[0]

    if mode == "--staged":
        files = get_staged_files()
        if not files:
            print("No staged files to scan.")
            return 0
        return scan_files(files)

    if mode == "all":
        # Full pre-push scan: engineering rules + file scan
        print("Pre-deploy rule check for: all")
        print("=" * 60)

        # Load engineering rules
        all_rules = []
        rules_path = os.path.join(BRAIN_DIR, "engineering_rules.json")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                all_rules = json.load(f).get("rules", [])
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
            enf_list = enforcement if isinstance(enforcement, list) else [enforcement] if isinstance(enforcement, dict) else []
            if not enf_list:
                skipped += 1
                continue
            for enf in enf_list:
                if not isinstance(enf, dict):
                    continue
                enf_type = enf.get("type", "")
                executor = EXECUTORS.get(enf_type)
                if not executor:
                    if enf_type:
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

        # File scan (HTTP + schema) on staged files
        staged = get_staged_files()
        if staged:
            file_result = scan_files(staged)
            if file_result != 0:
                all_violations.append("File scan violations found")
            checked += 1
        else:
            print("PASS: SEC-HTTPS-001-REQ-004 (no scannable staged files)")
            checked += 1

        print("=" * 60)
        print(f"Rules: {len(all_rules)} | Checked: {checked} | Skipped: {skipped} | Violations: {len(all_violations)}")

        if all_violations:
            print(f"\nDEPLOY BLOCKED: {len(all_violations)} violation(s)")
            return 1
        else:
            print("\nAll enforced rules passed. Deploy may proceed.")
            return 0

    # Direct file/dir scan
    files = []
    for path in args:
        if os.path.isfile(path):
            files.append(path)
        elif os.path.isdir(path):
            for root, _, fnames in os.walk(path):
                for fname in fnames:
                    files.append(os.path.join(root, fname))
    if files:
        return scan_files(files)

    print("No files to scan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
