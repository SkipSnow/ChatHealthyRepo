# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# sync_to_mongo.py — Push all project files from local disk to admin.project_files.
# Text files stored as text, binary files stored as base64.
# Each sync generates a manifest snapshot ID.
#
# Design: brain_v0.1.3_design.json v6.0
# Requirements: R5, R6, R9, R10, R11, R23, R27, R29
#
# Usage:
#   python sync_to_mongo.py                    # sync all project files
#   python sync_to_mongo.py --manifest-only    # rebuild manifest only, don't push file content

import argparse
import base64
import hashlib
import json
import logging
import mimetypes
import os
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_to_mongo")

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

EXCLUDE_DIRS = {
    ".venv", "node_modules", ".git", "dist", "__pycache__",
    ".claude", ".pytest_cache",
}

# R5: Text vs binary classification
TEXT_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".css", ".html", ".json", ".md",
    ".txt", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".sh",
    ".http", ".example", ".local", ".lock",
}

# R29: Artifact type classification
ARTIFACT_TYPE_MAP = {
    "brain/BusinessArtifacts/": "BusinessArtifact",
    "brain/machine_artifacts/document_type_json/": "MachineArtifact",
    "brain/machine_artifacts/document_type_md/": "MachineArtifact",
    "brain/machine_artifacts/document_type_py/": "Tool",
    "brain/machine_artifacts/content/": "Manifest",
    "archive/": "Archive",
    "Code/ConversationalUX/FindCareChat/": "SourceCode",
    "Code/ConversationalUX/ChatHealthyWhoAmIChat/me/": "RuntimeContext",
    "Code/DataPipelines/": "SourceCode",
    "Code/Shared/": "SourceCode",
    "Code/Shared/ops/": "DevOps",
    ".github/workflows/": "DevOps",
    "Website/": "SourceCode",
    "docs/": "Documentation",
    "Legal/": "Legal",
}

DEPLOYMENT_MAP = {
    "Website/": "Website -> Cloudflare",
    "Code/ConversationalUX/FindCareChat/backend/": "Chat App -> HuggingFace",
    "Code/ConversationalUX/FindCareChat/frontend/": "Chat App -> HuggingFace",
    "Code/ConversationalUX/ChatHealthyWhoAmIChat/me/": "Chat App -> HuggingFace",
    "Code/Shared/ChatHealthyMongoUtilities.py": "Chat App -> HuggingFace",
    "Code/DataPipelines/": "Pipelines -> Azure",
    ".github/workflows/": "CI/CD -> GitHub Actions",
}

# R29: Lifecycle state
def _lifecycle_state(project_path):
    if project_path.startswith("archive/"):
        return "archive"
    return "live"


def _artifact_type(project_path):
    """R29: Classify artifact type from path."""
    # Check most specific paths first (longer prefixes)
    for prefix in sorted(ARTIFACT_TYPE_MAP.keys(), key=len, reverse=True):
        if project_path.startswith(prefix):
            return ARTIFACT_TYPE_MAP[prefix]
    return "Other"


def _deployment(project_path):
    """Determine deployment path from file location."""
    for prefix, deploy in sorted(DEPLOYMENT_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if project_path.startswith(prefix) or project_path == prefix.rstrip("/"):
            return deploy
    return "Not deployed"


def _category(project_path, artifact_type):
    """Determine category from artifact type and path."""
    cat_map = {
        "BusinessArtifact": "Business",
        "MachineArtifact": "Operational",
        "Tool": "Tools",
        "Manifest": "Manifest",
        "Archive": "Archive",
        "RuntimeContext": "Source: Chat App Context",
        "DevOps": "Source: DevOps",
        "Documentation": "Documentation",
        "Legal": "Legal",
    }
    if artifact_type in cat_map:
        return cat_map[artifact_type]
    if project_path.startswith("Website/"):
        return "Source: Website"
    if project_path.startswith("Code/ConversationalUX/FindCareChat/"):
        return "Source: Chat App"
    if project_path.startswith("Code/DataPipelines/"):
        return "Source: Pipelines"
    if project_path.startswith("Code/Shared/"):
        return "Source: Shared"
    if project_path.startswith(".github/"):
        return "Source: DevOps"
    return "Config"


def _is_text(ext):
    """R5: Determine if file is text or binary."""
    return ext.lower() in TEXT_EXTENSIONS


def _content_hash(content_bytes):
    """R6: SHA256 content hash."""
    return hashlib.sha256(content_bytes).hexdigest()


def _mime_type(filepath):
    """R6: Determine MIME type."""
    mt, _ = mimetypes.guess_type(filepath)
    if mt:
        return mt
    ext = os.path.splitext(filepath)[1].lower()
    fallback = {
        ".py": "text/x-python",
        ".tsx": "text/typescript-jsx",
        ".ts": "text/typescript",
        ".md": "text/markdown",
        ".yml": "text/yaml",
        ".yaml": "text/yaml",
        ".http": "text/plain",
        ".example": "text/plain",
        ".local": "text/plain",
        ".lock": "application/json",
        "": "application/octet-stream",
    }
    return fallback.get(ext, "application/octet-stream")


def scan_project():
    """Scan project directory and return list of file entries."""
    entries = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in sorted(files):
            full_path = os.path.join(root, f)
            project_path = os.path.relpath(full_path, PROJECT_ROOT).replace(os.sep, "/")

            # Skip .env files
            if f == ".env" or f == ".env.local":
                continue

            ext = os.path.splitext(f)[1].lower()
            size = os.path.getsize(full_path)
            mime = _mime_type(full_path)
            is_text = _is_text(ext)
            encoding = "text" if is_text else "base64"
            a_type = _artifact_type(project_path)
            l_state = _lifecycle_state(project_path)
            cat = _category(project_path, a_type)
            deploy = _deployment(project_path)

            # Read content and hash
            with open(full_path, "rb") as fh:
                raw = fh.read()

            content_h = _content_hash(raw)

            if is_text:
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    content = base64.b64encode(raw).decode("ascii")
                    encoding = "base64"
            else:
                content = base64.b64encode(raw).decode("ascii")

            entries.append({
                "category": cat,
                "artifact_type": a_type,
                "live_or_archive": l_state,
                "lifecycle_state": l_state,
                "deployment": deploy,
                "filename": f,
                "project_path": project_path,
                "local_path": full_path.replace(os.sep, "/"),
                "mime_type": mime,
                "encoding": encoding,
                "size": size,
                "content_hash": content_h,
                "content": content,
                "description": "",
            })

    return entries


def sync(manifest_only=False):
    """Push project files to admin.project_files in MongoDB."""
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    # R27: Generate snapshot ID
    snapshot_id = f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

    log.info("Scanning project: %s", PROJECT_ROOT)
    entries = scan_project()
    log.info("Found %d files", len(entries))

    # Add snapshot ID to all entries
    for entry in entries:
        entry["manifest_snapshot_id"] = snapshot_id

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)

    try:
        db = client["admin"]

        if not manifest_only:
            # R10: Sync all files to admin.project_files
            log.info("Syncing %d files to admin.project_files (snapshot: %s)", len(entries), snapshot_id)
            coll = db["project_files"]
            coll.drop()
            if entries:
                # Insert copies so insert_many doesn't mutate originals with _id
                import copy
                coll.insert_many([copy.copy(e) for e in entries])
            log.info("Sync complete: %d files pushed", len(entries))

        # Build manifest (without content and _id fields)
        manifest_entries = []
        for entry in entries:
            manifest_entry = {k: v for k, v in entry.items() if k not in ("content", "_id")}
            manifest_entries.append(manifest_entry)

        # Write manifest to admin.manifest
        manifest_doc = {
            "_id": "current",
            "snapshot_id": snapshot_id,
            "generated": datetime.now(timezone.utc).isoformat(),
            "file_count": len(manifest_entries),
            "entries": manifest_entries,
        }
        db["manifest"].replace_one({"_id": "current"}, manifest_doc, upsert=True)
        log.info("Manifest written: %d entries (snapshot: %s)", len(manifest_entries), snapshot_id)

        # Also write manifest JSON to local disk
        manifest_path = os.path.join(PROJECT_ROOT, "brain", "manifest", "project_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_entries, f, indent=2)
        log.info("Local manifest updated: %s", manifest_path)

        return snapshot_id

    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync project files to MongoDB")
    parser.add_argument("--manifest-only", action="store_true",
                        help="Rebuild manifest only, don't push file content")
    args = parser.parse_args()
    snapshot = sync(manifest_only=args.manifest_only)
    log.info("Snapshot ID: %s", snapshot)
