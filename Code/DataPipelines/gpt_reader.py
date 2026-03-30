# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# gpt_reader.py — Read-only broker service for GPT.
# Two actions: ReadArtifact (project files) and Query (MongoDB).
# All access is read-only, capped, authenticated, and audited.
#
# Design: brain_v0.1.3_design.json v6.0
# Requirements: R1-R32

import base64
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from pymongo import MongoClient

log = logging.getLogger("gpt_reader")

# --- Caps (R7, R8) ---
MAX_RESPONSE_BYTES = 500 * 1024  # 500KB
MAX_QUERY_DOCUMENTS = 100
DEFAULT_QUERY_LIMIT = 10

# --- Allowlisted databases and collections (R2) ---
ALLOWED_ENVIRONMENTS = {"dev", "qa", "prod"}
ALLOWED_DB_PREFIXES = {
    "PublicHealthData", "System", "Debug", "AboutUs",
    "Brain", "MachineBrain", "Safety",
}
ALLOWED_GLOBAL_DBS = {"admin"}

# --- Auth (R4) ---
_GPT_READER_TOKEN = os.environ.get("GPT_READER_TOKEN", "")


def _authenticate(token: str) -> bool:
    """R4: Bearer token auth, separate from Router."""
    if not _GPT_READER_TOKEN:
        log.error("GPT_READER_TOKEN not configured")
        return False
    return token == _GPT_READER_TOKEN


def _is_db_allowed(database: str) -> bool:
    """R2: Only allowlisted databases."""
    if database in ALLOWED_GLOBAL_DBS:
        return True
    for prefix in ALLOWED_DB_PREFIXES:
        for env in ALLOWED_ENVIRONMENTS:
            if database == f"{env}_{prefix}":
                return True
    return False


def _audit_log(client: MongoClient, entry: dict):
    """R26: Audit every read request and response."""
    try:
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        client["admin"]["gpt_reader_audit"].insert_one(entry)
    except Exception as exc:
        log.error("Audit log failed: %s", exc)


def _check_response_size(data: str, action: str) -> dict:
    """R7: Check response doesn't exceed cap. Returns 507 error dict or None."""
    size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if size > MAX_RESPONSE_BYTES:
        return {
            "error": "response_too_large",
            "message": f"{action} response is {size:,} bytes. Max is {MAX_RESPONSE_BYTES:,}.",
            "max_bytes": MAX_RESPONSE_BYTES,
            "actual_bytes": size,
            "suggestion": "Request a smaller artifact or narrow your query.",
        }
    return None


def _read_artifact(client: MongoClient, config: dict) -> tuple:
    """ReadArtifact action — R1, R5, R6, R7, R13, R27.

    Returns (status_code, response_dict).
    """
    project_path = config.get("project_path")
    snapshot_id = config.get("manifest_snapshot_id")

    if not project_path:
        return 400, {"error": "project_path is required"}

    # R13: No access outside project boundary
    if ".." in project_path or project_path.startswith("/"):
        return 403, {"error": "Access denied: path traversal not allowed"}

    # Look up in admin.project_files
    coll = client["admin"]["project_files"]

    # R27: Snapshot consistency check
    if snapshot_id:
        doc = coll.find_one({"project_path": project_path, "manifest_snapshot_id": snapshot_id})
        if not doc:
            # Check if file exists with different snapshot
            exists = coll.find_one({"project_path": project_path})
            if exists:
                return 409, {
                    "error": "snapshot_mismatch",
                    "message": f"Artifact exists but snapshot_id does not match. Requested: {snapshot_id}, current: {exists.get('manifest_snapshot_id')}",
                    "suggestion": "Request a fresh manifest snapshot from Claude.",
                }
            return 404, {"error": f"Artifact not found: {project_path}"}
    else:
        doc = coll.find_one({"project_path": project_path})
        if not doc:
            return 404, {"error": f"Artifact not found: {project_path}"}

    content = doc.get("content", "")
    encoding = doc.get("encoding", "text")
    mime_type = doc.get("mime_type", "application/octet-stream")
    size = doc.get("size", 0)

    # R7: Response size check
    overflow = _check_response_size(content, "ReadArtifact")
    if overflow:
        return 507, overflow

    response = {
        "project_path": project_path,
        "mime_type": mime_type,
        "encoding": encoding,
        "size": size,
        "content": content,
    }
    return 200, response


def _query(client: MongoClient, config: dict) -> tuple:
    """Query action — R2, R3, R7, R8, R14.

    Returns (status_code, response_dict).
    """
    database = config.get("database")
    collection = config.get("collection")
    query = config.get("query", {})
    projection = config.get("projection")
    sort_spec = config.get("sort")
    limit = config.get("limit", DEFAULT_QUERY_LIMIT)
    environment = config.get("environment")

    if not database or not collection:
        return 400, {"error": "database and collection are required"}

    # R2: Allowlist check
    if not _is_db_allowed(database):
        return 403, {"error": f"Database not authorized: {database}"}

    # R8: Cap limit
    if not isinstance(limit, int) or limit < 1:
        limit = DEFAULT_QUERY_LIMIT
    if limit > MAX_QUERY_DOCUMENTS:
        limit = MAX_QUERY_DOCUMENTS

    # Validate query is a dict (prevent injection)
    if not isinstance(query, dict):
        return 400, {"error": "query must be a JSON object"}

    # Validate collection exists
    try:
        coll = client[database][collection]
    except Exception as exc:
        return 404, {"error": f"Collection not found: {exc}"}

    # R14: Pre-flight estimate — only block if no limit would help
    try:
        total_matching = coll.count_documents(query, limit=MAX_QUERY_DOCUMENTS + 1)
    except Exception as exc:
        return 404, {"error": f"Query error: {exc}"}

    # If caller didn't filter and total exceeds cap, warn them
    if total_matching > MAX_QUERY_DOCUMENTS and limit >= MAX_QUERY_DOCUMENTS:
        return 507, {
            "error": "response_too_large",
            "message": f"Query matches {total_matching:,}+ documents. Max is {MAX_QUERY_DOCUMENTS}. Your limit ({limit}) doesn't help — add a filter.",
            "max_documents": MAX_QUERY_DOCUMENTS,
            "estimated_result": total_matching,
            "suggestion": "Add a filter or reduce limit.",
        }
    estimated = min(total_matching, limit)

    # Execute query
    start = time.time()
    cursor = coll.find(query, projection)
    if sort_spec and isinstance(sort_spec, dict):
        cursor = cursor.sort(list(sort_spec.items()))
    cursor = cursor.limit(limit)

    documents = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])  # ObjectId to string
        documents.append(doc)

    elapsed_ms = int((time.time() - start) * 1000)

    # R7: Check serialized response size
    response_json = json.dumps(documents, default=str)
    overflow = _check_response_size(response_json, "Query")
    if overflow:
        return 507, overflow

    response = {
        "environment": environment or "",
        "database": database,
        "collection": collection,
        "count": len(documents),
        "documents": documents,
        "metrics": {
            "matched_estimate": estimated,
            "returned_count": len(documents),
            "response_bytes": len(response_json.encode("utf-8")),
            "elapsed_ms": elapsed_ms,
        },
    }
    return 200, response


def handle_gpt_reader(config: dict, token: str) -> tuple:
    """Main entry point. Returns (status_code, response_dict).

    Called by function_app.py route handler.
    """
    # R4: Auth
    if not _authenticate(token):
        return 401, {"error": "Unauthorized"}

    action = config.get("action")
    if not action:
        return 400, {"error": "action is required (ReadArtifact or Query)"}

    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not conn:
        return 500, {"error": "MONGO_FRONTEND_connectionString not configured"}

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)

    try:
        # Execute action
        if action == "ReadArtifact":
            status, response = _read_artifact(client, config)
        elif action == "Query":
            status, response = _query(client, config)
        else:
            status, response = 400, {"error": f"Unknown action: {action}. Valid: ReadArtifact, Query"}

        # R26: Audit
        _audit_log(client, {
            "actor": "GPT",
            "assignment_id": config.get("assignment_id", "unknown"),
            "action": action,
            "target": config.get("project_path") or f"{config.get('database', '')}.{config.get('collection', '')}",
            "status_code": status,
            "response_bytes": len(json.dumps(response, default=str).encode("utf-8")) if status == 200 else 0,
        })

        return status, response

    finally:
        client.close()
