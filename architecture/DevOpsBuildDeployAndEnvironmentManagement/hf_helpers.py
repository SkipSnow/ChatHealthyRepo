"""hf_helpers.py - HF Space helper library imported by the deploy chain.

Library-only module (no main(), no CLI). Used by local_build.py and
local_deploy.py for HF Space target_kind handling. Exposes:

  - HF Space naming + peer URL helpers (_hf_space_name, _hf_peer_url)
  - HF API write helpers (_hf_set_variable, _hf_set_secret)
  - Source-set definitions for each HF Space target (which dirs get
    staged into each Space's docker build context)
  - Filesystem copy helper (_copy_tree) using Builder's exclusion rules
  - React frontend build for FindCare (_build_react_frontend)
  - build_info.json writer for each HF Space (_write_hf_build_info)
"""
from __future__ import annotations

import base64
import functools
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from builder import _EXCLUDE_DIRS, _EXCLUDE_FILE_NAMES, _EXCLUDE_FILE_SUFFIXES
from target_record import TargetRecord
import ch_fonts_inliner


# ── HF Space identifiers from the manifest ─────────────────────────────
# Each HF target's environments[<env>].huggingface_space.space carries the
# fully-qualified 'org/name' Space identifier for that env_binding. The
# manifest is the source of truth (EPIC-008-F-012-S-001-REQ-B-008/B-009);
# no org/base/prefix rule lives in code anymore.

@functools.lru_cache(maxsize=1)
def _load_manifest() -> dict:
    repo_root = Path(__file__).resolve().parents[2]
    manifest = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    return json.loads(manifest.read_text(encoding="utf-8"))


def _hf_space_qualified(target_id: str, env: str) -> str:
    """Return the fully-qualified 'org/name' HF Space identifier for the
    given target_id + env_binding, read from the manifest. Fails loud if
    the target / env_binding / huggingface_space block is missing.
    """
    data = _load_manifest()
    for rec in data["DeploymentTargetRecord"]:
        if rec.get("target_id") != target_id:
            continue
        for env_entry in rec.get("environments", []):
            if env_entry.get("env_binding") != env:
                continue
            hs = env_entry.get("huggingface_space")
            if not hs or "space" not in hs:
                raise RuntimeError(
                    f"manifest target {target_id!r} env_binding {env!r} "
                    f"has no huggingface_space.space — populate it before deploy."
                )
            return hs["space"]
    raise RuntimeError(
        f"manifest has no target {target_id!r} with env_binding {env!r}."
    )


def _hf_org(target_id: str, env: str) -> str:
    return _hf_space_qualified(target_id, env).split("/", 1)[0]


def _hf_space_name(target_id: str, env: str) -> str:
    return _hf_space_qualified(target_id, env).split("/", 1)[1]


@functools.lru_cache(maxsize=1)
def _hf_default_org() -> str:
    """The operator's HF org. Read from any HF target's huggingface_space
    entry — all HF Spaces in this project sit under the same org. Used by
    the HF API helpers below where the caller has the bare space name but
    needs to assemble the API URL.
    """
    data = _load_manifest()
    for rec in data["DeploymentTargetRecord"]:
        if rec.get("target_kind") != "hf_space":
            continue
        for env_entry in rec.get("environments", []):
            hs = env_entry.get("huggingface_space")
            if hs and "space" in hs:
                return hs["space"].split("/", 1)[0]
    raise RuntimeError(
        "no hf_space target with a populated huggingface_space.space "
        "in the manifest — cannot infer operator HF org."
    )


def _hf_peer_url(target_id: str, env: str) -> str:
    """Derived from the manifest's qualified 'org/space' identifier. HF
    publishes Space frontends at https://{org}-{space}.hf.space with
    underscores in the Space name converted to hyphens and the whole
    thing lowercased.
    """
    org, space = _hf_space_qualified(target_id, env).split("/", 1)
    return f"https://{org.lower()}-{space.replace('_', '-').lower()}.hf.space"


# ── Step notice helper ─────────────────────────────────────────────────
def _step(msg: str) -> None:
    print(f"[hf_helpers] {msg}", flush=True)


# ── build_info.json baked into each HF Space ──────────────────────────
def _write_hf_build_info(workspace: Path, target_id: str, env: str) -> None:
    """Write build_info.json at the workspace root so the Dockerfile's
    `COPY build_info.json /app/build_info.json` resolves. /health on each
    backend prefers this file over an admin.Versions Mongo read."""
    service_map = {
        "target_hf_space_findcare_backend":     "ch-findcare",
        "target_hf_space_evaluatecare_backend": "ch-evalcare",
        "target_hf_space_shared_services":      "ch-sharedsvc",
    }
    service = service_map.get(target_id, target_id)
    # Build = the canonical global counter in admin.Versions on the
    # front-end cluster (one int, not per-env per build_deploy_promote_plan
    # v3 §3). Same source local_build.py uses for image tagging; baking
    # it here keeps image tag, build_info.json, /health, and banner all
    # in lock-step by construction.
    from dotenv import load_dotenv
    from pymongo import MongoClient
    repo_root = Path(__file__).resolve().parent.parent.parent
    load_dotenv(repo_root / "Code" / ".env")
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        sys.exit("ERROR: MONGO_FRONTEND_connectionString not set in env or Code/.env")
    latest = MongoClient(conn, serverSelectionTimeoutMS=10000)["admin"]["Versions"].find_one(
        sort=[("from", -1)]
    )
    if latest is None:
        sys.exit("ERROR: admin.Versions has no records.")
    build_n = latest.get("build")
    if build_n is None:
        sys.exit("ERROR: admin.Versions latest record has no 'build' field.")
    build_n = int(build_n)
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    info = {
        "build": build_n,
        "commit": commit,
        "env": env,
        "target_id": target_id,
        "service": service,
        "version": "1.4.1",
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    (workspace / "build_info.json").write_text(
        json.dumps(info, indent=2), encoding="utf-8",
    )


# ── HF API: variables + secrets ────────────────────────────────────────
def _hf_curl_delete(token: str, space: str, kind: str, key: str) -> None:
    """Delete a variable or secret on an HF Space (idempotent — 404 fine)."""
    import urllib.error
    import urllib.request
    url = f"https://huggingface.co/api/spaces/{_hf_default_org()}/{space}/{kind}"
    body = b'{"key":"' + key.encode() + b'"}'
    req = urllib.request.Request(
        url, data=body, method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except urllib.error.HTTPError:
        pass
    except urllib.error.URLError:
        pass


def _hf_set_variable(token: str, space: str, key: str, value: str) -> None:
    import json as _json
    import urllib.request
    # Delete any same-named secret first to avoid HF's var/secret collision.
    _hf_curl_delete(token, space, "secrets", key)
    url = f"https://huggingface.co/api/spaces/{_hf_default_org()}/{space}/variables"
    payload = _json.dumps({
        "key": key, "value": value, "description": "Set by local_publish",
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=30).read()


def _hf_set_secret(token: str, space: str, key: str, value: str) -> None:
    import json as _json
    import urllib.request
    _hf_curl_delete(token, space, "variables", key)
    _hf_curl_delete(token, space, "secrets", key)
    url = f"https://huggingface.co/api/spaces/{_hf_default_org()}/{space}/secrets"
    payload = _json.dumps({
        "key": key, "value": value, "description": "Set by local_publish",
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=30).read()


# ── Source-set conventions per target_id ───────────────────────────────
# These are the directories `local_publish.py` ships into each HF
# Space's docker build context.
def _source_set_for(target_id: str) -> list[tuple[str, str | None]]:
    """Return the (src_rel, dst_rel|None) subtree list for an HF target,
    read from its huggingface_source_set field in the manifest. dst=None
    means same as src. Replaces the per-target hardcoded functions
    (_findcare_source_set, _evaluatecare_source_set,
    _sharedservices_source_set).
    """
    data = _load_manifest()
    for rec in data["DeploymentTargetRecord"]:
        if rec.get("target_id") != target_id:
            continue
        raw = rec.get("huggingface_source_set")
        if not raw:
            raise RuntimeError(
                f"manifest target {target_id!r} has no huggingface_source_set "
                f"declared — populate it before deploy."
            )
        out: list[tuple[str, str | None]] = []
        for entry in raw:
            src = entry["src"]
            dst = entry.get("dst")
            out.append((src, dst))
        return out
    raise RuntimeError(f"manifest has no target {target_id!r}.")


def _findcare_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    return _source_set_for("target_hf_space_findcare_backend")


def _evaluatecare_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    return _source_set_for("target_hf_space_evaluatecare_backend")


def _sharedservices_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    return _source_set_for("target_hf_space_shared_services")


def _copy_tree(
    src_root: Path, dst_root: Path,
    src_rel: str, dst_rel: str,
) -> None:
    """Copy a subtree into staging using the SAME exclusion rules
    Builder uses to enumerate source_locations."""
    src = src_root / src_rel
    if not src.is_dir():
        raise FileNotFoundError(f"source dir missing: {src}")
    dst = dst_root if dst_rel == "." else dst_root / dst_rel
    dst.mkdir(parents=True, exist_ok=True)
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        parts = set(path.parts)
        if parts & _EXCLUDE_DIRS:
            continue
        if path.name in _EXCLUDE_FILE_NAMES:
            continue
        if path.suffix.lower() in _EXCLUDE_FILE_SUFFIXES:
            continue
        rel = path.relative_to(src)
        out_path = dst / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out_path)


# ── React frontend build (FindCare only) ──────────────────────────────
def _build_react_frontend(repo_root: Path, env: str) -> None:
    frontend = repo_root / "Code" / "ConversationalUX" / "FindCareChat" / "frontend"
    if not (frontend / "package.json").is_file():
        raise FileNotFoundError(f"frontend package.json missing at {frontend}")
    canonical_vite = (repo_root / "architecture"
                      / "DevOpsBuildDeployAndEnvironmentManagement"
                      / "vite.config.ts")
    if not canonical_vite.is_file():
        raise FileNotFoundError(f"canonical vite config missing at {canonical_vite}")
    vite_copy = frontend / "vite.config.ts"
    shutil.copy2(canonical_vite, vite_copy)
    evalcare_peer = _hf_peer_url("target_hf_space_evaluatecare_backend", env)
    env_for_build = dict(os.environ)
    env_for_build["VITE_API_URL"] = ""
    env_for_build["VITE_EVALCARE_URL"] = evalcare_peer
    try:
        _step(f"npm ci in {frontend}")
        subprocess.run(
            ["npm", "ci"], cwd=str(frontend), env=env_for_build,
            check=True, shell=(sys.platform == "win32"),
        )
        _step(f"npm run build (VITE_EVALCARE_URL={evalcare_peer})")
        subprocess.run(
            ["npm", "run", "build"], cwd=str(frontend), env=env_for_build,
            check=True, shell=(sys.platform == "win32"),
        )
        dist_index = frontend / "dist" / "index.html"
        if not dist_index.is_file():
            raise FileNotFoundError(f"vite produced no {dist_index}")
        if not ch_fonts_inliner.inline_into(dist_index):
            raise RuntimeError(f"CH_FONTS marker not found in {dist_index}")
    finally:
        if vite_copy.is_file():
            vite_copy.unlink()
