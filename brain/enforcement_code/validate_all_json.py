# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# validate_all_json.py
#
# Validate every non-gitignored JSON file under brain/machine_artifacts/content/
# by reading its declared $schema URL, fetching that schema from the live
# web, and validating the file against it with jsonschema Draft 2020-12.
#
# Each JSON file must:
#   - have a $schema field
#   - that $schema must NOT be the relaxed placeholder
#     (ChatHealthyRelaxedSchema.json) - strict validation only
#   - the fetched schema body must be valid JSON (HTML fallback = fail)
#   - the file must validate with zero errors
#
# Writes a JSON report to test_output/validate_all_json_report.json with
# every error. Also prints a human-readable summary to stdout.
# Exit code 0 iff every non-gitignored JSON under brain content passed.
#
# Usage: python brain/enforcement_code/validate_all_json.py [root_dir]
#   default root_dir is brain/machine_artifacts/content/

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BRAIN_CONTENT = REPO_ROOT / "brain" / "machine_artifacts" / "content"
REPORT_PATH = REPO_ROOT / "test_output" / "validate_all_json_report.json"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

SKIP_DIRS = {
    ".venv", "node_modules", "__pycache__", ".git",
    "test_output", ".iteration_cache", "dist", "build",
}

RELAXED_URL_SUBSTRINGS = (
    "ChatHealthyRelaxedSchema.json",
    "chathealthy-relaxed.schema.json",
)


def is_gitignored(path: Path) -> bool:
    """Return True if git considers path ignored. No subprocess error => tracked."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--", str(path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


_schema_cache: dict[str, Any] = {}


def fetch_schema(url: str) -> tuple[Any | None, str | None]:
    """Fetch a schema from the web. Returns (schema_dict, error_message)."""
    if url in _schema_cache:
        cached = _schema_cache[url]
        if isinstance(cached, tuple):
            return cached
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read()
            content_type = r.headers.get("Content-Type", "")
        if "html" in content_type.lower() or body.lstrip().startswith(b"<"):
            result = (None, f"schema URL returned non-JSON (Content-Type={content_type})")
            _schema_cache[url] = result
            return result
        schema = json.loads(body)
        _schema_cache[url] = (schema, None)
        return schema, None
    except urllib.error.HTTPError as e:
        result = (None, f"HTTP {e.code}: {e.reason}")
        _schema_cache[url] = result
        return result
    except Exception as e:
        result = (None, f"fetch error: {e}")
        _schema_cache[url] = result
        return result


def validate_one(path: Path) -> dict[str, Any]:
    """Return a result dict for a single file."""
    rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    try:
        body = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"file": rel, "status": "UNREADABLE", "errors": [str(e)]}

    try:
        doc = json.loads(body)
    except json.JSONDecodeError as e:
        return {"file": rel, "status": "PARSE_ERROR", "errors": [str(e)]}

    if not isinstance(doc, dict):
        # JSON Lines or array-at-root files don't have a $schema - skip validation
        # but don't flag as error either.
        return {"file": rel, "status": "SKIPPED_NOT_OBJECT", "errors": []}

    schema_url = doc.get("$schema")
    if not isinstance(schema_url, str) or not schema_url:
        return {"file": rel, "status": "MISSING_SCHEMA", "errors": ["$schema field missing or empty"]}

    # Meta-schemas (a schema file declares what draft it is). These are
    # schema files themselves, not content. Report as skipped with reason.
    if "json-schema.org" in schema_url:
        return {"file": rel, "status": "SKIPPED_META_SCHEMA", "errors": [], "schema_url": schema_url}

    # Strict-schema requirement: the relaxed schema is not real validation.
    if any(s in schema_url for s in RELAXED_URL_SUBSTRINGS):
        return {
            "file": rel,
            "status": "NOT_STRICT",
            "errors": [f"file is declared against the relaxed schema ({schema_url}); "
                       "strict validation requires a file-specific schema"],
            "schema_url": schema_url,
        }

    schema, fetch_err = fetch_schema(schema_url)
    if fetch_err:
        return {"file": rel, "status": "FETCH_FAILED",
                "errors": [fetch_err], "schema_url": schema_url}

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return {"file": rel, "status": "VALIDATOR_UNAVAILABLE",
                "errors": ["jsonschema library not installed"],
                "schema_url": schema_url}

    validator = Draft202012Validator(schema)
    errors = []
    for err in validator.iter_errors(doc):
        path_str = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{path_str}: {err.message}")

    # Additional uniqueness pass driven by 'x-unique-by' annotations in the
    # schema. Orphan records (orphan: true) are exempt - they don't have an
    # id field to check.
    errors.extend(check_x_unique_by(schema, doc))

    return {
        "file": rel,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "schema_url": schema_url,
    }


def check_x_unique_by(schema: Any, data: Any) -> list[str]:
    """Walk schema+data together; for every array carrying x-unique-by,
    assert that the named field is unique across items where orphan != true.
    Returns a list of human-readable error strings."""
    errors: list[str] = []
    _walk_unique(schema, data, "", errors)
    return errors


def _walk_unique(schema: Any, data: Any, path: str, errors: list[str]) -> None:
    if not isinstance(schema, dict):
        return
    t = schema.get("type")
    if t == "array" and isinstance(data, list):
        if "x-unique-by" in schema:
            field = schema["x-unique-by"]
            seen: dict[Any, int] = {}
            for idx, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                if item.get("orphan") is True:
                    continue
                if field not in item:
                    continue
                value = item[field]
                if value in seen:
                    errors.append(
                        f"{path}[{idx}]: duplicate {field}={value!r} "
                        f"(first seen at {path}[{seen[value]}])"
                    )
                else:
                    seen[value] = idx
        items_schema = schema.get("items")
        if items_schema:
            for idx, item in enumerate(data):
                _walk_unique(items_schema, item, f"{path}[{idx}]", errors)
    elif t == "object" and isinstance(data, dict):
        for key, subschema in schema.get("properties", {}).items():
            if key in data:
                _walk_unique(subschema, data[key], f"{path}/{key}", errors)


def walk_json_files(root: Path) -> list[Path]:
    """Every .json file under root that isn't in SKIP_DIRS and isn't gitignored."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".json"):
                continue
            fp = Path(dirpath) / name
            if is_gitignored(fp):
                continue
            out.append(fp)
    out.sort()
    return out


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else BRAIN_CONTENT
    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        return 2

    files = walk_json_files(root)
    results = [validate_one(f) for f in files]

    # Summarize
    summary: dict[str, int] = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1

    failures = [r for r in results if r["status"] in (
        "FAIL", "PARSE_ERROR", "UNREADABLE", "MISSING_SCHEMA",
        "NOT_STRICT", "FETCH_FAILED", "VALIDATOR_UNAVAILABLE",
    )]

    # Print per-file report so every error is visible in the console
    print(f"Validating {len(files)} JSON file(s) under {root}")
    print("=" * 90)
    for r in results:
        if r["status"] == "PASS":
            continue  # don't spam PASS lines
        if r["status"] in ("SKIPPED_META_SCHEMA", "SKIPPED_NOT_OBJECT"):
            continue
        print(f"{r['status']:22} {r['file']}")
        for e in r["errors"]:
            print(f"  {e}")
    print("=" * 90)
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    print(f"Failures: {len(failures)}")

    # Write machine-readable report
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "root": str(root),
        "total_files": len(files),
        "summary": summary,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    print(f"Report: {REPORT_PATH}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
