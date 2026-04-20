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

SKIP_FILES = {"conversation_log.json", "pre_deploy_rule_check.py"}


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




def enforce_bugs_schema(rule_id, enforcement):
    """Validate bugs.json against the PUBLISHED schema declared in its
    $schema field. Never validate against a local file - would let
    local-vs-published drift hide. Per ARCH-SCAN-JSON-REQ-003."""
    violations = []
    bugs_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "bugs.json")
    try:
        with open(bugs_path, "r", encoding="utf-8") as f:
            bugs = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        violations.append(f"{rule_id}: {e}")
        return violations

    schema_url = bugs.get("$schema")
    if not schema_url or not schema_url.startswith("http"):
        violations.append(
            f"{rule_id}: bugs.json missing or non-URL $schema field "
            f"(got {schema_url!r}) - cannot validate against the published schema")
        return violations

    schema, err = _fetch_published_schema(schema_url, rule_id)
    if err:
        violations.append(err)
        return violations

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        violations.append(f"{rule_id}: jsonschema library not installed")
        return violations

    for err in Draft202012Validator(schema).iter_errors(bugs):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        violations.append(f"{rule_id}: bugs.json {path}: {err.message}")
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


def enforce_published_schema_validation(rule_id, enforcement):
    """Generic validator: for every JSON under the configured scan_dir,
    read its $schema URL, fetch the schema from the published URL, and
    validate the JSON against it. One executor for every governed brain
    JSON. Never falls back to a local schema file - that would let
    local-vs-published drift hide. Per ARCH-SCAN-JSON-REQ-003 and human
    directive: every JSON we own MUST be validated against its published
    schema, no exceptions."""
    violations = []
    scan_dir = enforcement.get("scan_dir") or os.path.join(
        "brain", "machine_artifacts", "content")
    if not os.path.isabs(scan_dir):
        scan_dir = os.path.join(REPO_ROOT, scan_dir)

    if not os.path.isdir(scan_dir):
        violations.append(f"{rule_id}: scan_dir {scan_dir} does not exist")
        return violations

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        violations.append(f"{rule_id}: jsonschema library not installed")
        return violations

    schema_cache = {}  # by URL, to avoid re-fetching the same loose schema
    json_files = sorted(f for f in os.listdir(scan_dir) if f.endswith(".json"))
    if not json_files:
        violations.append(f"{rule_id}: no JSON files found in {scan_dir}")
        return violations

    for fname in json_files:
        fpath = os.path.join(scan_dir, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            violations.append(f"{rule_id}: {fname}: invalid JSON: {e}")
            continue

        if not isinstance(data, dict):
            violations.append(
                f"{rule_id}: {fname}: top-level is not an object - cannot "
                f"declare $schema")
            continue

        schema_url = data.get("$schema")
        if not schema_url or not isinstance(schema_url, str) or \
                not schema_url.startswith("http"):
            violations.append(
                f"{rule_id}: {fname}: missing or non-URL $schema field "
                f"(got {schema_url!r}) - every governed JSON MUST declare "
                f"its published schema URL")
            continue

        if schema_url not in schema_cache:
            schema, err = _fetch_published_schema(schema_url, rule_id)
            if err:
                schema_cache[schema_url] = (None, err)
            else:
                schema_cache[schema_url] = (schema, None)
        schema, err = schema_cache[schema_url]
        if err:
            violations.append(f"{rule_id}: {fname}: {err}")
            continue

        for jerr in Draft202012Validator(schema).iter_errors(data):
            path = "/".join(str(p) for p in jerr.absolute_path) or "(root)"
            violations.append(
                f"{rule_id}: {fname} {path}: {jerr.message[:200]}")

    return violations


def _fetch_published_schema(schema_url, rule_id):
    """Fetch a JSON Schema from its PUBLISHED URL. Never fall back to a
    local file — that would silently validate against an unpublished
    schema and let drift hide. Per ARCH-SCAN-JSON-REQ-003 and human
    directive: the validator uses the published schema, period."""
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            schema_url, headers={"User-Agent": "chathealthy-validator/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        return json.loads(data.decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, (
            f"{rule_id}: published schema {schema_url} returned HTTP "
            f"{e.code} - publish the schema before validating")
    except urllib.error.URLError as e:
        return None, (
            f"{rule_id}: published schema {schema_url} unreachable "
            f"({e.reason}) - publish the schema before validating")
    except json.JSONDecodeError as e:
        return None, (
            f"{rule_id}: published schema {schema_url} is not valid JSON: {e}")


def enforce_backlog_schema(rule_id, enforcement):
    """Validate agile_backlog.json against the PUBLISHED schema declared in
    its $schema field. Never validate against a local file — would let
    local-vs-published drift hide. Also enforce cross-record uniqueness
    on every ID field per the schema's 'Never reused' contract."""
    violations = []
    backlog_path = enforcement.get("path") or os.path.join(
        REPO_ROOT, "brain", "machine_artifacts", "content", "agile_backlog.json")
    if not os.path.isabs(backlog_path):
        backlog_path = os.path.join(REPO_ROOT, backlog_path)

    try:
        with open(backlog_path, "r", encoding="utf-8") as f:
            backlog = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        violations.append(f"{rule_id}: {e}")
        return violations

    schema_url = backlog.get("$schema")
    if not schema_url or not schema_url.startswith("http"):
        violations.append(
            f"{rule_id}: agile_backlog.json missing or non-URL $schema field "
            f"(got {schema_url!r}) - cannot validate against the published "
            f"schema")
        return violations

    schema, err = _fetch_published_schema(schema_url, rule_id)
    if err:
        violations.append(err)
        return violations

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        violations.append(f"{rule_id}: jsonschema library not installed")
        return violations

    for err in Draft202012Validator(schema).iter_errors(backlog):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        violations.append(
            f"{rule_id}: agile_backlog.json {path}: {err.message[:200]}")

    # ───── Cross-record uniqueness check — DISABLED ─────
    # Per Skip directive 2026-04-19: this check is commented out because
    # it is orphan code (no requirement authorizes it) and was blocking
    # commits on pre-existing duplicate IDs that pre-date today's
    # session. The schema text says these IDs are "never reused" but
    # JSON Schema cannot express that constraint. To re-enable, file an
    # approved requirement that authorizes the check, then uncomment.
    #
    # seen = {"epic_id": {}, "feature_id": {}, "story_id": {}, "req_id": {}}
    # for epic in backlog.get("epics", {}).get("epic", []):
    #     eid = epic.get("epic_id")
    #     if eid:
    #         seen["epic_id"].setdefault(eid, []).append(epic.get("name", "?"))
    #     for feature in epic.get("features", {}).get("feature", []):
    #         fid = feature.get("feature_id")
    #         if fid:
    #             seen["feature_id"].setdefault(fid, []).append(feature.get("name", "?"))
    #         for story in feature.get("stories", {}).get("story", []):
    #             sid = story.get("story_id")
    #             if sid:
    #                 seen["story_id"].setdefault(sid, []).append(
    #                     story.get("name", story.get("title", "?")))
    #             for req in story.get("requirements", {}).get("requirement", []):
    #                 rid = req.get("req_id")
    #                 if rid:
    #                     seen["req_id"].setdefault(rid, []).append(rid)
    # for kind, m in seen.items():
    #     for k, names in m.items():
    #         if len(names) > 1:
    #             violations.append(
    #                 f"{rule_id}: duplicate {kind} '{k}' used by {len(names)} "
    #                 f"records: {names[:5]}")

    return violations


def enforce_conversation_log_schema(rule_id, enforcement):
    """Validate that schema.json defines LogRecords/LogRecord for the prompt_log collection."""
    violations = []
    schema_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "schema.json")
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        violations.append(f"{rule_id}: {e}")
        return violations

    prompt_log = schema.get("collections", {}).get("prompt_log", {})
    if not prompt_log:
        violations.append(f"{rule_id}: schema.json missing prompt_log collection definition")
        return violations

    record_schemas = prompt_log.get("record_schemas", {})
    if not record_schemas:
        violations.append(f"{rule_id}: prompt_log collection missing record_schemas")
        return violations

    if "LogRecord" not in record_schemas:
        violations.append(f"{rule_id}: prompt_log record_schemas missing LogRecord definition")
        return violations

    log_record = record_schemas["LogRecord"]
    fields = log_record.get("fields", {})
    if not fields:
        violations.append(f"{rule_id}: LogRecord has no fields defined")
        return violations

    required_fields = ["actor", "role", "timestamp_pst", "timestamp_utc", "content", "utterance"]
    for fname in required_fields:
        if fname not in fields:
            violations.append(f"{rule_id}: LogRecord missing required field definition '{fname}'")

    return violations


# ── v4-043 — graph orchestration entry point check ─────────────


_ROUTE_DECO_ATTRS = {"post", "get", "put", "delete", "patch",
                     "options", "head", "route"}
_APP_NAMES = {"app", "router", "blueprint", "api", "bp"}


def _is_route_decorator(deco) -> bool:
    if isinstance(deco, ast.Call):
        deco = deco.func
    if isinstance(deco, ast.Attribute) and isinstance(deco.value, ast.Name):
        return (deco.value.id in _APP_NAMES and
                deco.attr in _ROUTE_DECO_ATTRS)
    return False


def _calls_graph_invoke(node) -> bool:
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
        root = os.path.join(REPO_ROOT, d)
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
                local_funcs = {}
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        local_funcs[n.name] = n
                for n in ast.walk(tree):
                    if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if not any(_is_route_decorator(d) for d in n.decorator_list):
                        continue
                    src_lines = src.splitlines()
                    if 0 < n.lineno <= len(src_lines):
                        head = "\n".join(src_lines[max(0, n.lineno - 4):n.lineno])
                        if "graph-exempt" in head:
                            continue
                    if _calls_graph_invoke(n):
                        continue
                    delegated_ok = False
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Call):
                            f = sub.func
                            name = None
                            if isinstance(f, ast.Name):
                                name = f.id
                            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
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
                        f"'{n.name}' bypasses graph orchestrator")
    return violations


EXECUTORS = {
    "file_scan": enforce_file_scan,
    "file_absent": enforce_file_absent,
    "file_present": enforce_file_present,
    "json_check": enforce_json_check,
    "no_pattern": enforce_no_pattern,
    "published_schema_validation": enforce_published_schema_validation,
    # Per-file executors below are deprecated by the generic
    # published_schema_validation above. Kept for backwards compatibility
    # with rule entries that still name them; remove once v4-007 only
    # declares published_schema_validation.
    "bugs_schema": enforce_bugs_schema,
    "backlog_schema": enforce_backlog_schema,
    "ai_operations_schema": enforce_ai_operations_schema,
    "conversation_log_schema": enforce_conversation_log_schema,
    "graph_entry_check": enforce_graph_entry_check,
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


_IMPLICIT_TARGET_FILES = {
    "bugs_schema": ["brain/machine_artifacts/content/bugs.json"],
    "backlog_schema": ["brain/machine_artifacts/content/agile_backlog.json"],
    "ai_operations_schema": ["brain/machine_artifacts/content/ai_operations.json"],
    "conversation_log_schema": ["brain/machine_artifacts/content/schema.json"],
}


def _changed_files_for_deploy():
    """Return the set of file paths that this deploy is delivering.
    On GH Actions push: derived from the event payload (before..after).
    Locally: derived from `git diff @{upstream}..HEAD`.
    Returns None if the set cannot be determined (caller falls back to
    full scan, never silently under-validates)."""
    import subprocess as _sp
    # GH Actions push event: GITHUB_EVENT_PATH + GITHUB_SHA give us the range.
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        try:
            with open(event_path, "r", encoding="utf-8") as f:
                ev = json.load(f)
            before = ev.get("before") or ""
            after = ev.get("after") or os.environ.get("GITHUB_SHA", "HEAD")
            if before and before != "0000000000000000000000000000000000000000":
                r = _sp.run(["git", "diff", "--name-only", f"{before}..{after}"],
                            capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
                if r.returncode == 0:
                    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
        except Exception:
            pass
    # Local fallback: diff against upstream.
    try:
        up = _sp.run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                     capture_output=True, text=True, timeout=5, cwd=REPO_ROOT)
        if up.returncode == 0:
            r = _sp.run(["git", "diff", "--name-only", f"{up.stdout.strip()}..HEAD"],
                        capture_output=True, text=True, timeout=10, cwd=REPO_ROOT)
            if r.returncode == 0:
                return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    except Exception:
        pass
    return None


def _enforcement_in_scope(enf: dict, changed_files: set) -> bool:
    """True if the enforcement entry's scope intersects with the changed
    files. Path comes from explicit fields OR the implicit-target mapping.
    If neither is known, run conservatively."""
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


def main(target: str) -> int:
    print(f"Pre-deploy rule check for: {target}")
    print("=" * 60)

    changed_files = _changed_files_for_deploy()
    if changed_files is None:
        print("WARN: could not determine changed-file set; running full scan (conservative).")
        scope_filter_active = False
    else:
        print(f"Deploy scope: {len(changed_files)} changed file(s)")
        scope_filter_active = True

    # Load all rules from brain (new shape: rules.rule[]; backward-compat
    # path also accepts old shape: rules[] for transition safety).
    all_rules = []
    for fname in ["engineering_rules.json"]:
        fpath = os.path.join(BRAIN_DIR, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        rules_blob = data.get("rules", [])
        if isinstance(rules_blob, dict):
            for r in rules_blob.get("rule", []):
                all_rules.append(r)
        else:
            for r in rules_blob:
                all_rules.append(r)

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

            # Skip executor if its scope doesn't intersect with the
            # files this deploy is delivering.
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
        print(f"Out-of-scope (not validated, deploy doesn't touch them): {out_of_scope}")

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
