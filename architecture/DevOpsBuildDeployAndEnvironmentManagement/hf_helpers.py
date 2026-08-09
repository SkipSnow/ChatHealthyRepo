"""hf_helpers.py - HF Space helper library imported by the deploy chain.

Library-only module (no main(), no CLI). Used by build_chathealthy.py and
deploy_chathealthy.py for HF Space target_kind handling. Exposes:

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
    """Stable-base-name URL. Retained for callers that still want the
    manifest-stored Space (no build_n suffix). New per-build code should
    use _hf_peer_url_for_build."""
    org, space = _hf_space_qualified(target_id, env).split("/", 1)
    return f"https://{org.lower()}-{space.replace('_', '-').lower()}.hf.space"


def _hf_peer_url_for_build(target_id: str, env: str, build_n: int) -> str:
    """Reverted 2026-06-22: returns the stable-base-name URL. The
    per-build (`_<n>`) Space-naming scheme is retired — HF rate-limits
    Space CREATE calls, and re-deploying to the same Space is the
    standard pattern. `build_n` is accepted for caller compatibility
    but unused."""
    return _hf_peer_url(target_id, env)


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
    # v3 §3). Same source build_chathealthy.py uses for image tagging;
    # baking it here keeps image tag, build_info.json, /health, and banner
    # all in lock-step by construction.
    from version_counter import VERSIONS_COLLECTION, VERSIONS_DB, latest_record
    latest = latest_record()
    build_n = latest.get("build")
    if build_n is None:
        sys.exit(
            f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record has "
            f"no 'build' field."
        )
    build_n = int(build_n)
    version_str = latest.get("version")
    framework_str = latest.get("framework")
    if not version_str:
        sys.exit(
            f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record has "
            f"no 'version' field."
        )
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Version + framework now come from admin.Versions (single-source). Skip
    # edits the latest record directly when a release ships; build chain
    # reads from there and bakes into the image.
    info = {
        "build": build_n,
        "commit": commit,
        "env": env,
        "target_id": target_id,
        "service": service,
        "version": version_str,
        "framework": framework_str,
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


# ── HF API: Space lifecycle (create / delete / list / wait) ─────────────
def _hf_space_per_build_qualified(target_id: str, env: str, build_n: int) -> str:
    """Reverted 2026-06-22: returns the STABLE BASE qualified name. The
    per-build (`_<n>`) Space-naming scheme is retired — HF rate-limits
    Space CREATE calls and orphan Spaces accumulate. `build_n` is
    accepted for caller compatibility but unused."""
    return _hf_space_qualified(target_id, env)


def _hf_space_per_build_name(target_id: str, env: str, build_n: int) -> str:
    return _hf_space_per_build_qualified(target_id, env, build_n).split("/", 1)[1]


def _hf_space_exists(token: str, qualified: str) -> bool:
    import urllib.error
    import urllib.request
    url = f"https://huggingface.co/api/spaces/{qualified}"
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise


def _hf_create_space(token: str, qualified: str, sdk: str = "docker") -> None:
    """Create a new HF Space. Idempotent — if it already exists, no-op."""
    if _hf_space_exists(token, qualified):
        _step(f"  hf-create skip {qualified} (already exists)")
        return
    import json as _json
    import urllib.request
    org, name = qualified.split("/", 1)
    url = "https://huggingface.co/api/repos/create"
    payload = _json.dumps({
        "type": "space",
        "name": name,
        "organization": org,
        "private": False,
        "sdk": sdk,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    urllib.request.urlopen(req, timeout=30).read()
    _step(f"  hf-create OK {qualified} (sdk={sdk})")


def _hf_delete_space(token: str, qualified: str) -> None:
    """Delete an HF Space. Idempotent — 404 fine."""
    import json as _json
    import urllib.error
    import urllib.request
    org, name = qualified.split("/", 1)
    url = "https://huggingface.co/api/repos/delete"
    payload = _json.dumps({
        "type": "space",
        "name": name,
        "organization": org,
    }).encode()
    req = urllib.request.Request(
        url, data=payload, method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=30).read()
        _step(f"  hf-delete OK {qualified}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _step(f"  hf-delete skip {qualified} (already gone)")
            return
        raise


def _hf_list_per_build_spaces(token: str, target_id: str, env: str) -> list[tuple[str, int]]:
    """Return [(qualified_name, build_n)] for every existing HF Space that
    matches the per-build naming pattern <base>_<int> for this target/env.
    Used to find the previous build's Space so we can delete it after the
    new one converges."""
    import urllib.error
    import urllib.request
    qualified_base = _hf_space_qualified(target_id, env)
    org, base = qualified_base.split("/", 1)
    out: list[tuple[str, int]] = []
    # HF /api/spaces?author=<org> lists all Spaces under the org; filter client-side.
    url = f"https://huggingface.co/api/spaces?author={org}&limit=1000"
    req = urllib.request.Request(
        url, method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        raw = urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError:
        return out
    import json as _json
    spaces = _json.loads(raw)
    prefix = f"{base}_"
    for s in spaces:
        sid = s.get("id") or ""
        # sid is "org/name". strip org/.
        if "/" not in sid:
            continue
        name = sid.split("/", 1)[1]
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        if not suffix.isdigit():
            continue
        out.append((sid, int(suffix)))
    return out


def _hf_wait_for_build_convergence(qualified: str, build_n: int,
                                    timeout_s: int = 600,
                                    poll_interval_s: int = 10) -> bool:
    """Poll the Space's public /health endpoint until it reports the expected
    build_n, or until timeout. Returns True on convergence, False on timeout."""
    import time
    import urllib.error
    import urllib.request
    org, name = qualified.split("/", 1)
    health_url = (
        f"https://{org.lower()}-{name.replace('_', '-').lower()}.hf.space/health"
    )
    deadline = time.monotonic() + timeout_s
    last_build = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(health_url, method="POST")
            raw = urllib.request.urlopen(req, timeout=15).read()
            import json as _json
            data = _json.loads(raw)
            last_build = data.get("build")
            if last_build == build_n:
                _step(f"  hf-wait OK {qualified}: build={build_n}")
                return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
            pass
        time.sleep(poll_interval_s)
    _step(f"  hf-wait TIMEOUT {qualified}: last build={last_build} expected={build_n}")
    return False


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
    sharedservices_peer = _hf_peer_url("target_hf_space_shared_services", env)
    env_for_build = dict(os.environ)
    env_for_build["VITE_API_URL"] = ""
    env_for_build["VITE_EVALCARE_URL"] = evalcare_peer
    env_for_build["VITE_SHAREDSERVICES_URL"] = sharedservices_peer
    try:
        _step(f"npm ci in {frontend}")
        subprocess.run(
            ["npm", "ci"], cwd=str(frontend), env=env_for_build,
            check=True, shell=(sys.platform == "win32"),
        )
        _step(f"npm run build (VITE_EVALCARE_URL={evalcare_peer} VITE_SHAREDSERVICES_URL={sharedservices_peer})")
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
