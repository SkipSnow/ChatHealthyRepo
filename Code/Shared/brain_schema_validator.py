# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_schema_validator.py — EPIC-008-F-007-S-020-REQ-T-001.
#
# Validates each brain JSON file against the web schema URL declared in
# its own $schema property. URLs of the form
#   https://{env}.chathealthy.ai/schemas/{env}/<Name>.json
# are resolved offline to Website/schemas/{env}/<Name>.json in this
# repo. Files whose $schema points at chathealthy-relaxed.schema.json
# are skipped — that's the to-do-list placeholder.
#
# schema.json (the brain-level metadata file) is no longer parsed for
# validation rules; it is kept only as the human-readable punch list of
# collections still awaiting a real web schema.

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
BRAIN_CONTENT = REPO_ROOT / "brain" / "machine_artifacts" / "content"
WEBSITE_SCHEMAS = REPO_ROOT / "Website" / "schemas"


def _resolve_schema_url(url: str) -> Path | None:
    """Map a ChatHealthy web-schema URL to a local file.
    Returns None for the relaxed catch-all (skip) or unknown hosts."""
    if not url or not isinstance(url, str):
        return None
    if "chathealthy-relaxed" in url:
        return None
    # https://{env}.chathealthy.ai/schemas/{env}/<Name>.json
    for env in ("dev", "qa", "prod"):
        needle = f"{env}.chathealthy.ai/schemas/{env}/"
        if needle in url:
            name = url.rsplit("/", 1)[-1]
            candidate = WEBSITE_SCHEMAS / env / name
            if candidate.exists():
                return candidate
            return None
    return None


def _validate_one(brain_file: Path) -> list[str]:
    """Validate a single brain JSON. Returns list of error strings."""
    try:
        data = json.loads(brain_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"{brain_file.name}: invalid JSON: {e}"]
    except Exception as e:
        return [f"{brain_file.name}: unreadable: {e}"]

    schema_url = data.get("$schema") if isinstance(data, dict) else None
    schema_path = _resolve_schema_url(schema_url)
    if schema_path is None:
        return []  # no strict schema (relaxed, missing, or external) — skip

    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"{brain_file.name}: cannot load web schema {schema_path.name}: {e}"]

    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for err in validator.iter_errors(data):
        path = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"{brain_file.name}: {path}: {err.message}")
    return errors


def validate_all(brain_dir: str | os.PathLike | None = None) -> dict[str, list[str]]:
    """Validate every brain JSON in brain_dir against its declared web schema.
    Returns {filename: [errors]}."""
    root = Path(brain_dir) if brain_dir else REPO_ROOT / "brain"
    content_dir = root / "machine_artifacts" / "content" if (root / "machine_artifacts").exists() else root
    results: dict[str, list[str]] = {}
    for f in sorted(content_dir.glob("*.json")):
        results[f.name] = _validate_one(f)
    return results


def validate_bugs(brain_dir: str | os.PathLike | None = None) -> list[str]:
    """Backwards-compat shim: validate bugs.json specifically."""
    root = Path(brain_dir) if brain_dir else REPO_ROOT / "brain"
    p = (root / "machine_artifacts" / "content" / "bugs.json") if (root / "machine_artifacts").exists() \
        else (root / "bugs.json")
    return _validate_one(p)


def validate_backlog(brain_dir: str | os.PathLike | None = None) -> list[str]:
    """Backwards-compat shim: validate agile_backlog.json specifically."""
    root = Path(brain_dir) if brain_dir else REPO_ROOT / "brain"
    p = (root / "machine_artifacts" / "content" / "agile_backlog.json") if (root / "machine_artifacts").exists() \
        else (root / "agile_backlog.json")
    return _validate_one(p)


if __name__ == "__main__":
    results = validate_all()
    total = 0
    for name, errs in results.items():
        if errs:
            print(f"\n{name}: {len(errs)} violation(s)")
            for e in errs[:20]:
                print(f"  FAIL: {e}")
            if len(errs) > 20:
                print(f"  ... and {len(errs) - 20} more")
            total += len(errs)
        else:
            print(f"{name}: OK")
    if total:
        print(f"\n{total} total violations.")
        sys.exit(1)
    print("\nAll brain JSON files are schema-compliant.")
