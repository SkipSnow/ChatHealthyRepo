import os, json, hashlib

manifest = []
root_dir = "c:/chatHealthy/findCare"
skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "BusinessArtifacts", "test_screenshots", ".claude"}

for root, dirs, files in os.walk(root_dir):
    dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
    for f in sorted(files):
        full = os.path.join(root, f)
        path = os.path.relpath(full, root_dir).replace(os.sep, "/")
        if path.startswith(".") or "__pycache__" in path:
            continue
        try:
            size = os.path.getsize(full)
            with open(full, "rb") as fh:
                content_hash = hashlib.blake2b(fh.read(), digest_size=8).hexdigest()
        except Exception:
            continue
        manifest.append({
            "project_path": path,
            "size": size,
            "content_hash": content_hash,
            "entity_type": "file",
            "description": "",
        })

out = os.path.join(root_dir, "brain", "manifest", "project_manifest.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, ensure_ascii=False)

print(f"Manifest updated: {len(manifest)} files")
