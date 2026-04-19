# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_schema_validator.py — BRAIN-SCHEMA-REQ-001
# Strict enforcement of brain JSON files against schema.json.
# Every brain JSON write MUST pass this validator.

import json
import os
from pathlib import Path


def _load_schema(brain_dir: str) -> dict:
    schema_path = os.path.join(brain_dir, "machine_artifacts", "content", "schema.json")
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


def _find_collection_schema(schema: dict, collection_name: str) -> dict | None:
    collections = schema.get("collections", {})
    if isinstance(collections, dict):
        return collections.get(collection_name)
    # Legacy list format
    for col in collections:
        if isinstance(col, dict) and col.get("collection") == collection_name:
            return col
    return None


def _extract_constraints(record_schemas: dict) -> dict:
    """Extract all field constraints from record_schemas recursively.
    Returns {entity_type: {field_name: [allowed_values]}}"""
    constraints = {}
    for entity_name, entity_def in record_schemas.items():
        if not isinstance(entity_def, dict):
            continue
        fields = entity_def.get("fields", {})
        field_constraints = {}
        for field_name, field_def in fields.items():
            if isinstance(field_def, dict) and field_def.get("possible_values"):
                field_constraints[field_name] = field_def["possible_values"]
        if field_constraints:
            constraints[entity_name] = field_constraints
    return constraints


def _validate_record(record: dict, entity_type: str, constraints: dict, path: str) -> list[str]:
    """Validate a single record against constraints. Returns list of errors."""
    errors = []
    entity_constraints = constraints.get(entity_type, {})

    for field_name, allowed_values in entity_constraints.items():
        if field_name in record:
            value = record[field_name]
            if value not in allowed_values:
                errors.append(
                    f"{path}.{field_name} = '{value}' — "
                    f"not in allowed values: {allowed_values}"
                )

    return errors


def _validate_hierarchy(data: dict, constraints: dict) -> list[str]:
    """Validate the epic→feature→story→requirement hierarchy."""
    errors = []

    for i, epic in enumerate(data.get("epics", [])):
        epic_path = f"epics[{i}]({epic.get('epic_id', '?')})"
        errors.extend(_validate_record(epic, "epic", constraints, epic_path))

        for j, feature in enumerate(epic.get("features", [])):
            feat_path = f"{epic_path}.features[{j}]({feature.get('feature_id', '?')})"
            errors.extend(_validate_record(feature, "feature", constraints, feat_path))

            for k, story in enumerate(feature.get("stories", [])):
                story_path = f"{feat_path}.stories[{k}]({story.get('story_id', '?')})"
                errors.extend(_validate_record(story, "story", constraints, story_path))

                for m, req in enumerate(story.get("requirements", [])):
                    req_path = f"{story_path}.requirements[{m}]({req.get('req_id', '?')})"
                    errors.extend(_validate_record(req, "requirement", constraints, req_path))

    return errors


def validate_backlog(brain_dir: str) -> list[str]:
    """Validate agile_backlog.json against schema.json constraints.
    Returns list of error strings. Empty list = valid."""
    schema = _load_schema(brain_dir)
    col_schema = _find_collection_schema(schema, "agile_backlog")
    if not col_schema:
        return ["Schema collection 'agile_backlog' not found in schema.json"]

    record_schemas = col_schema.get("record_schemas", {})
    constraints = _extract_constraints(record_schemas)

    if not constraints:
        return ["No field constraints found in agile_backlog schema"]

    backlog_path = os.path.join(brain_dir, "machine_artifacts", "content", "agile_backlog.json")
    with open(backlog_path, encoding="utf-8") as f:
        data = json.load(f)

    return _validate_hierarchy(data, constraints)


def validate_bugs(brain_dir: str) -> list[str]:
    """Validate bugs.json against schema.json constraints.
    Returns list of error strings. Empty list = valid."""
    schema = _load_schema(brain_dir)
    col_schema = _find_collection_schema(schema, "bugs")
    if not col_schema:
        return ["Schema collection 'bugs' not found in schema.json"]

    # New shape: collections.bugs.bug is an array of field-definition dicts
    # keyed by "name". Each entry may carry possible_values.
    bug_defs = col_schema.get("bug", [])
    field_specs = {}
    if isinstance(bug_defs, list):
        for entry in bug_defs:
            if isinstance(entry, dict) and "name" in entry:
                field_specs[entry["name"]] = entry

    constraints = {}
    for fname, spec in field_specs.items():
        pv = spec.get("possible_values")
        if pv:
            constraints.setdefault("bug", {})[fname] = pv

    if not constraints:
        return []

    bugs_path = os.path.join(brain_dir, "machine_artifacts", "content", "bugs.json")
    with open(bugs_path, encoding="utf-8") as f:
        data = json.load(f)

    errors = []
    for i, bug in enumerate(data.get("bug", [])):
        bug_path = f"bug[{i}]({bug.get('bugid', '?')})"
        errors.extend(_validate_record(bug, "bug", constraints, bug_path))

    return errors


def validate_all(brain_dir: str) -> dict[str, list[str]]:
    """Validate all brain JSON files against schema.json.
    Returns {collection_name: [errors]}."""
    results = {}
    results["agile_backlog"] = validate_backlog(brain_dir)
    results["bugs"] = validate_bugs(brain_dir)
    return results


if __name__ == "__main__":
    brain = os.path.join(os.path.dirname(__file__), "..", "..", "brain")
    results = validate_all(brain)
    total_errors = 0
    for collection, errors in results.items():
        if errors:
            print(f"\n{collection}: {len(errors)} violations")
            for err in errors:
                print(f"  FAIL: {err}")
            total_errors += len(errors)
        else:
            print(f"{collection}: OK")

    if total_errors:
        print(f"\n{total_errors} total violations found.")
        exit(1)
    else:
        print("\nAll brain JSON files are schema-compliant.")
