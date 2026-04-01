# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# brain_snapshot.py — Produces a manifest-level snapshot of Brain state from local disk.
# GPT receives this as a table of contents. GPT asks for content via GPTReader if needed.
#
# This is the catalog, not the library.

import json
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_BRAIN_DIR = _REPO_ROOT / "brain"
_CONTENT_DIR = _BRAIN_DIR / "machine_artifacts" / "content"
_MANIFEST_PATH = _BRAIN_DIR / "manifest" / "project_manifest.json"


def take_snapshot() -> str:
    """Read the project manifest and Brain collection index from disk.

    Returns a table of contents — file paths, record IDs, descriptions.
    GPT uses GPTReader to fetch actual content when needed.
    """
    parts = []

    # 1. Project manifest — every file in the repo with description
    if _MANIFEST_PATH.exists():
        try:
            with open(_MANIFEST_PATH, encoding="utf-8") as f:
                manifest = json.load(f)
            entries = manifest if isinstance(manifest, list) else manifest.get("files", manifest.get("entries", []))
            if isinstance(entries, list):
                parts.append(f"=== PROJECT MANIFEST ({len(entries)} files) ===")
                parts.append("Use GPTReader ReadArtifact to fetch any file content.")
                parts.append("")
                for e in entries:
                    if isinstance(e, dict):
                        path = e.get("project_path", e.get("path", "?"))
                        desc = e.get("description", e.get("capabilities", ""))
                        if desc:
                            parts.append(f"  {path}: {str(desc)[:150]}")
                        else:
                            parts.append(f"  {path}")
            parts.append("")
        except Exception as e:
            parts.append(f"=== MANIFEST: read error: {e} ===\n")

    # 2. Brain collection index — record IDs and descriptions only
    if _CONTENT_DIR.exists():
        parts.append("=== BRAIN COLLECTIONS ===")
        parts.append("Use GPTReader Query to fetch record content by _record_id.")
        parts.append("")
        for json_file in sorted(_CONTENT_DIR.glob("*.json")):
            try:
                with open(json_file, encoding="utf-8") as f:
                    data = json.load(f)
                records = data.get("records", [])
                if isinstance(records, list):
                    parts.append(f"  {json_file.stem} ({len(records)} records):")
                    for r in records:
                        if isinstance(r, dict):
                            rid = r.get("_record_id", "?")
                            desc = r.get("description", r.get("title", ""))
                            parts.append(f"    {rid}: {str(desc)[:120]}")
                    parts.append("")
            except Exception:
                pass

    return "\n".join(parts)


if __name__ == "__main__":
    print(take_snapshot())
