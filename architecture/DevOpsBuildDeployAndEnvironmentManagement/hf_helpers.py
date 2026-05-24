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


# ── HF Space name convention ───────────────────────────────────────────
# prod                 -> <Base>
# dev/qa/feature       -> <env>_<Base>
_HF_SPACE_BASE: dict[str, str] = {
    "target_hf_space_findcare_backend":      "ChatHealthySpace",
    "target_hf_space_evaluatecare_backend":  "EvaluateCareSpace",
    "target_hf_space_shared_services":       "SharedServicesSpace",
}
_HF_ORG: str = "SkipSnow"


def _hf_space_name(target_id: str, env: str) -> str:
    base = _HF_SPACE_BASE[target_id]
    return base if env == "prod" else f"{env}_{base}"


def _hf_peer_url(target_id: str, env: str) -> str:
    base = {
        "target_hf_space_findcare_backend":     "chathealthyspace",
        "target_hf_space_evaluatecare_backend": "evaluatecarespace",
        "target_hf_space_shared_services":      "sharedservicesspace",
    }[target_id]
    prefix = "" if env == "prod" else f"{env}-"
    return f"https://skipsnow-{prefix}{base}.hf.space"


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
    # Build = commit count on the deployed branch (Rule-063 /
    # admin.Versions convention). Computed against the operator's local
    # repo since the snapshot dir has no .git of its own.
    try:
        cp = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        build_num_str = cp.stdout.strip()
    except Exception:
        build_num_str = ""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        commit = "unknown"
    info = {
        "build": int(build_num_str) if build_num_str.isdigit() else None,
        "commit": commit,
        "env": env,
        "service": service,
        "version": "1.4.1",
        "framework": "0.1.5",
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
    url = f"https://huggingface.co/api/spaces/{_HF_ORG}/{space}/{kind}"
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
    url = f"https://huggingface.co/api/spaces/{_HF_ORG}/{space}/variables"
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
    url = f"https://huggingface.co/api/spaces/{_HF_ORG}/{space}/secrets"
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
def _findcare_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    """Returns [(src_rel, dst_rel|None)]. dst_rel=None means same as src."""
    return [
        ("Code/ConversationalUX/FindCareChat/backend", "Code/ConversationalUX/FindCareChat/backend"),
        ("Code/Shared", "Code/Shared"),
        ("brain/machine_artifacts/content", "brain/machine_artifacts/content"),
        ("Code/ConversationalUX/ChatHealthyWhoAmIChat/me", "Code/ConversationalUX/ChatHealthyWhoAmIChat/me"),
        ("FrontEndApplicationLib", "FrontEndApplicationLib"),
        ("FindCare", "FindCare"),
        ("DevOps/FindCareBackend", "DevOps/FindCareBackend"),
    ]


def _evaluatecare_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    return [
        ("evaluateCare/Code", "."),
        ("FrontEndApplicationLib", "FrontEndApplicationLib"),
    ]


def _sharedservices_source_set(repo_root: Path) -> list[tuple[str, str | None]]:
    return [
        ("sharedServices/Code", "."),
        ("FrontEndApplicationLib", "FrontEndApplicationLib"),
        # EPIC-002-F-003: auth feature lives at architecture/
        # AuthorizationsAndAuthentications/ in the source tree and is
        # staged into the SharedServices build context as `authentication/`.
        ("architecture/AuthorizationsAndAuthentications", "authentication"),
        # EPIC-006-F-002: specialty_filter_tool lives under
        # FindCare/SpecialtyFilter/; SharedServices' build context needs
        # it at the root as `SpecialtyFilter/`.
        ("FindCare/SpecialtyFilter", "SpecialtyFilter"),
    ]


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
