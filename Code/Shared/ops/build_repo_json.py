#!/usr/bin/env python3
# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# build_repo_json.py — Export the full repository as a single JSON file.
#
# Used to communicate the codebase to web-based LLMs (ChatGPT, Perplexity, etc.)
# Excludes: brain/BusinessArtifacts, images, binaries.
# Includes: all source code, configs, docs with file content embedded.
#
# Usage:
#   python build_repo_json.py                              (default output: C:\temp\chatHealthyRepository.json)
#   python build_repo_json.py --output /path/to/out.json
#   python build_repo_json.py --date 2026-04-03

import argparse
import json
import os
from datetime import datetime, timezone

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MANIFEST_PATH = os.path.join(REPO_ROOT, "brain", "manifest", "project_manifest.json")
DEFAULT_OUTPUT = r"C:\temp\chatHealthyRepository.json"

EXCLUDED_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.bmp', '.tiff', '.webp',
    '.pdf', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
    '.zip', '.tar', '.gz', '.7z', '.rar',
    '.exe', '.dll', '.so', '.dylib',
    '.mp3', '.mp4', '.wav', '.avi', '.mov',
    '.woff', '.woff2', '.ttf', '.eot', '.otf',
}

def should_exclude(project_path):
    if project_path.rstrip("/").startswith("brain/BusinessArtifacts"):
        return True
    _, ext = os.path.splitext(project_path.rstrip("/"))
    if ext.lower() in EXCLUDED_EXTENSIONS:
        return True
    return False

def read_file_content(project_path):
    full_path = os.path.join(REPO_ROOT, project_path.replace("/", os.sep))
    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"[Error reading file: {e}]"

def build_tree(manifest_entries):
    dirs = {}
    files = {}

    for entry in manifest_entries:
        pp = entry["project_path"]
        if should_exclude(pp):
            continue
        if entry["entity_type"] in ("directory",):
            dirs[pp.rstrip("/")] = entry.get("description", "")
        else:
            files[pp] = entry

    tree = {}

    # Register directories
    for dir_path, desc in sorted(dirs.items()):
        parts = dir_path.split("/")
        node = tree
        for part in parts:
            if part not in node:
                node[part] = {"directory": part}
            node = node[part]
        if desc:
            node["description"] = desc

    # Add files
    total_files = len(files)
    for idx, (file_path, entry) in enumerate(sorted(files.items())):
        if idx % 20 == 0:
            print(f"  Reading file {idx+1}/{total_files}: {file_path[:80]}", flush=True)

        parts = file_path.split("/")
        filename = parts[-1]
        dir_parts = parts[:-1]

        node = tree
        for part in dir_parts:
            if part not in node:
                node[part] = {"directory": part}
            node = node[part]

        file_entry = {
            "description": entry.get("description", ""),
            "content": read_file_content(file_path)
        }
        node[filename] = file_entry

    return tree

def main():
    parser = argparse.ArgumentParser(description="Export ChatHealthy repo as JSON for web LLMs")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--date", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"), help="Date stamp")
    args = parser.parse_args()

    print("Loading manifest...", flush=True)
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    print(f"Manifest has {len(manifest)} entries", flush=True)
    print("Building tree...", flush=True)

    tree = build_tree(manifest)

    output = {
        "ChatHealthy Code and documentation repository": tree,
        "date": args.date,
        "Copyright": "Skip Snow"
    }

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    print(f"Writing output to {args.output}...", flush=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    file_size = os.path.getsize(args.output)
    print(f"Done! Output: {args.output} ({file_size:,} bytes)", flush=True)

if __name__ == "__main__":
    main()
