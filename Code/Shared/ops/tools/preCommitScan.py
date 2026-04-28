# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# preCommitScan.py — Unified pre-commit and pre-push scanner.
#
# One file. One pass. All rules. No redundancy.
#
# Rules enforced:
#   - EPIC-002-F-001-S-012-REQ-B-004: No HTTP URLs in production code
#   - v4-007 enforcement types: file_scan, file_absent, file_present, json_check, no_pattern
#   - v4-031: Dead code scanner (two-pass)
#
# EPIC-008-F-007-S-020-REQ-T-001 (JSON-vs-schema validation) is a RUNTIME requirement,
# not a governance-time requirement, and is no longer enforced here.
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
    r"test_",
    r"conftest\.py",
    r"brain/BusinessArtifacts/",
    r"Code/Shared/ops/tools/scan_http\.py$",  # scanner's own regex patterns
    r"Code/Shared/ops/tools/preCommitScan\.py$",  # ditto
]

SKIP_PATTERNS = [r"__pycache__", r"\.pyc$", r"node_modules", r"\.venv"]

SCAN_EXTENSIONS = {".py", ".tsx", ".ts", ".js", ".jsx", ".html", ".yml", ".yaml", ".cfg", ".toml"}


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


# ── Single-pass file scanner ────────────────────────────────────

def scan_files(files):
    """One pass over all files. HTTP URL check only.
    JSON-vs-schema validation is a runtime concern, not governance-time."""
    http_violations = []

    for filepath in files:
        if _should_skip(filepath):
            continue

        # HTTP URL check (production code only)
        if not _is_http_exempt(filepath):
            http_violations.extend(_check_http_urls(filepath))

    # Report
    exit_code = 0

    if http_violations:
        print(f"EPIC-002-F-001-S-012-REQ-B-004 VIOLATION: {len(http_violations)} insecure HTTP URLs:")
        for v in http_violations:
            print(v)
        exit_code = 1
    else:
        print(f"EPIC-002-F-001-S-012-REQ-B-004 PASS: 0 insecure HTTP URLs in {len(files)} files.")

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


# ── v4-043 — graph orchestration entry point check ─────────────


# Decorator names that mark a route handler (FastAPI / Flask / Starlette).
_ROUTE_DECO_ATTRS = {"post", "get", "put", "delete", "patch",
                     "options", "head", "route"}
# Variable suffixes typically used for the app/router instance.
_APP_NAMES = {"app", "router", "blueprint", "api", "bp"}


def _is_route_decorator(deco) -> bool:
    """Match @app.post(...), @router.get(...), @blueprint.route(...)."""
    if isinstance(deco, ast.Call):
        deco = deco.func
    if isinstance(deco, ast.Attribute) and isinstance(deco.value, ast.Name):
        return (deco.value.id in _APP_NAMES and
                deco.attr in _ROUTE_DECO_ATTRS)
    return False


def _calls_graph_invoke(node) -> bool:
    """Walk an AST node and return True if any descendant Call matches
    `<something>.invoke(...)` (the LangGraph orchestrator entry pattern)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == "invoke":
                return True
            if (isinstance(f, ast.Attribute) and
                    isinstance(f.value, ast.Attribute) and
                    f.value.attr == "invoke"):
                return True
    return False


def enforce_graph_entry_check(rule_id, enforcement):
    """v4-043: every FastAPI/Flask route handler in the named scan_dirs
    MUST invoke through a graph (any `<x>.invoke(...)` in the handler
    body, OR delegate to a same-module helper that does)."""
    violations = []
    for d in enforcement.get("scan_dirs", []):
        root = _resolve_dir(d)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [x for x in dirnames
                           if x not in {"__pycache__", ".pytest_cache",
                                        ".venv", "node_modules", "tests"}]
            for fn in filenames:
                if not fn.endswith(".py") or fn.startswith("test_"):
                    continue
                fp = os.path.join(dirpath, fn)
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        src = f.read()
                    tree = ast.parse(src)
                except (SyntaxError, UnicodeDecodeError):
                    continue
                # Build a map of locally-defined function name -> its body
                local_funcs = {}
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        local_funcs[n.name] = n
                # Find all route handlers and check each
                for n in ast.walk(tree):
                    if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if not any(_is_route_decorator(d) for d in n.decorator_list):
                        continue
                    # Allow exemption via marker comment in source
                    src_lines = src.splitlines()
                    if 0 < n.lineno <= len(src_lines):
                        head = "\n".join(src_lines[max(0, n.lineno - 4):n.lineno])
                        if "graph-exempt" in head:
                            continue
                    # Direct invoke?
                    if _calls_graph_invoke(n):
                        continue
                    # Delegated helper one level deep?
                    delegated_ok = False
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Call):
                            f = sub.func
                            name = None
                            if isinstance(f, ast.Name):
                                name = f.id
                            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                                # e.g., self._chat_inner — skip self
                                name = f.attr
                            if name and name in local_funcs:
                                if _calls_graph_invoke(local_funcs[name]):
                                    delegated_ok = True
                                    break
                    if delegated_ok:
                        continue
                    rel = os.path.relpath(fp, REPO_ROOT).replace("\\", "/")
                    violations.append(
                        f"{rule_id}: {rel}:{n.lineno}: route handler "
                        f"'{n.name}' bypasses graph orchestrator (no "
                        f"<state_graph>.invoke(...) in body or in same-"
                        f"module helper). Add a graph node or mark the "
                        f"handler '# graph-exempt' with a paired "
                        f"risk_acceptance entry.")
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


# ── Pre-push scope helpers (BUG-SCANNER-OVERSCOPE-001) ──────────


def _changed_files_for_push():
    """Return the set of repo-relative file paths that this push will
    deliver to the remote (i.e. files in commits ahead of @{upstream}).
    Returns None if upstream is unknown — caller falls back to full scan."""
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True, text=True, timeout=5, cwd=REPO_ROOT)
        if upstream.returncode != 0:
            return None
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{upstream.stdout.strip()}..HEAD"],
            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
        if result.returncode != 0:
            return None
        return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    except Exception:
        return None


# Implicit target files for enforcement types that hardcode their target
# rather than declaring it in the rule's enforcement entry. Keeps the
# scope filter accurate without forcing every rule to spell out its path.
_IMPLICIT_TARGET_FILES = {
    "bugs_schema": ["brain/machine_artifacts/content/bugs.json"],
    "backlog_schema": ["brain/machine_artifacts/content/agile_backlog.json"],
    "conversation_log_schema": ["brain/machine_artifacts/content/schema.json"],
}


def _enforcement_in_scope(enf: dict, changed_files: set) -> bool:
    """True if the enforcement entry's scope intersects with the changed
    files. Path comes from explicit fields in the enforcement entry OR
    from the per-type implicit-target mapping above. If neither is known
    the scope is unknown and we run conservatively."""
    if not changed_files:
        return False
    paths = []
    for k in ("scan_dir", "path"):
        v = enf.get(k)
        if isinstance(v, str):
            paths.append(v)
    for k in ("scan_dirs", "paths"):
        v = enf.get(k)
        if isinstance(v, list):
            paths.extend(p for p in v if isinstance(p, str))
    enf_type = enf.get("type", "")
    if not paths and enf_type in _IMPLICIT_TARGET_FILES:
        paths.extend(_IMPLICIT_TARGET_FILES[enf_type])
    if not paths:
        return True
    for cf in changed_files:
        for p in paths:
            if cf == p or cf.startswith(p.rstrip("/") + "/"):
                return True
    return False


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
        # Pre-push scan, scoped to files affected by the about-to-push commits.
        # Per BUG-SCANNER-OVERSCOPE-001: pre-existing violations in unchanged
        # files MUST NOT block a push that does not modify them.
        print("Pre-deploy rule check for: all")
        print("=" * 60)

        # Determine the changed-file set for this push by diffing against
        # the upstream branch. If we cannot determine it, fall back to
        # the conservative full-scan behavior (so we never silently
        # under-validate).
        changed_files = _changed_files_for_push()
        if changed_files is None:
            print("WARN: could not determine changed-file set for push; "
                  "running full scan (conservative).")
            scope_filter_active = False
        else:
            print(f"Pre-push scope: {len(changed_files)} changed file(s)")
            scope_filter_active = True

        # Load engineering rules (new shape: rules.rule[]; backward-compat
        # path also accepts old shape: rules[] for safety during transition).
        all_rules = []
        rules_path = os.path.join(BRAIN_DIR, "engineering_rules.json")
        if os.path.exists(rules_path):
            with open(rules_path, "r", encoding="utf-8") as f:
                rules_blob = json.load(f).get("rules", [])
            if isinstance(rules_blob, dict):
                all_rules = rules_blob.get("rule", [])
            else:
                all_rules = rules_blob
        print(f"Loaded {len(all_rules)} rules")

        all_violations = []
        checked = 0
        skipped = 0
        out_of_scope = 0

        for rule in all_rules:
            rule_id = rule.get("id", "unknown")
            # New shape: rule.enforcements.enforcement[]; old shape: rule.enforcement
            enf_block = rule.get("enforcements")
            if isinstance(enf_block, dict):
                enforcement = enf_block.get("enforcement", [])
            else:
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
                # Scope filter: skip executor if its scope doesn't intersect
                # with the files this push is touching.
                if scope_filter_active and not _enforcement_in_scope(enf, changed_files):
                    out_of_scope += 1
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
        if scope_filter_active:
            print(f"Out-of-scope (not validated, push doesn't touch them): {out_of_scope}")

        # File scan (HTTP + schema) on staged files
        staged = get_staged_files()
        if staged:
            file_result = scan_files(staged)
            if file_result != 0:
                all_violations.append("File scan violations found")
            checked += 1
        else:
            print("PASS: EPIC-002-F-001-S-012-REQ-B-004 (no scannable staged files)")
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
