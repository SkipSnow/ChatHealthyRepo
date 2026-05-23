# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""local_publish.py — operator's build manager.

Builds every deploy artifact HERE on this machine and ships it to its
cloud destination. Per operator directive: "the content of what gets
deployed is 100% built here and nowhere else."

Three deploy targets — one entry point:

  hf         — three HF Space backends. For each: docker build locally,
               push to ghcr.io/skipsnow/<svc>:<env>-<sha7>, set HF env
               vars + secrets via API, push 2-line `FROM <ghcr>`
               Dockerfile (built here) to the Space repo.
  cloudflare — Cloudflare Pages site. Stage Website/, inline fonts,
               materialize JSON-owned bytes, `npx wrangler pages
               deploy` from this machine.
  azure      — Azure Functions pipeline. Build deploy.zip from
               pipeline/Code/ on this machine, push via `az
               functionapp deployment source config-zip`, then restart
               the host to drain warm sys.modules cache.

usage:
  python local_publish.py --env dev
  python local_publish.py --env dev --target azure
  python local_publish.py --env qa  --target hf
  python local_publish.py --env prod

prerequisites (one-time on this machine):
  docker login ghcr.io -u <gh-user> -p <PAT-with-write:packages>   # hf
  az login                                                          # azure
  Code/.env carries HF_TOKEN, GEMINI_API_KEY, GOOGLE_OAUTH_*,
  CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, …
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Windows console defaults to cp1252; force UTF-8 so step notices with
# em-dash / arrow / ellipsis don't UnicodeEncodeError out of the run.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from extractor import Extractor
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord

# Reuse the source-set + HF helpers from remote_deploy. They're pure
# functions; the runner-side script still owns its own dispatch.
import ch_fonts_inliner
import remote_deploy as rd


# ── GHCR image-ref convention (HF Spaces) ─────────────────────────────
_GHCR_OWNER: str = "skipsnow"
_GHCR_IMAGE_NAME: dict[str, str] = {
    "target_hf_space_findcare_backend":      "findcare-backend",
    "target_hf_space_evaluatecare_backend":  "evaluatecare-backend",
    "target_hf_space_shared_services":       "sharedservices-backend",
}
_HF_APP_PORT: dict[str, int] = {
    "target_hf_space_findcare_backend":      7860,
    "target_hf_space_evaluatecare_backend":  7860,
    "target_hf_space_shared_services":       7860,
}


def _ghcr_image_ref(target_id: str, env: str, sha7: str) -> str:
    return f"ghcr.io/{_GHCR_OWNER}/{_GHCR_IMAGE_NAME[target_id]}:{env}-{sha7}"


# ── Per-env Azure Functions pipeline map ──────────────────────────────
# The dev Function App + task hub come from the prior deploy-pipelines
# workflow. qa/prod are not yet provisioned; operator fills the map
# before publishing to those envs.
_AZURE_DEPLOY_TARGETS: dict[str, dict[str, str]] = {
    "dev": {
        "resource_group": "FindCareDataPipelines-Dev",
        "function_app":   "DevPipelineManagmentService",
        "task_hub":       "DevPipelineManagmentService",
    },
}


# ── Per-env Cloudflare Pages project map ──────────────────────────────
_CLOUDFLARE_PROJECT: dict[str, str] = {
    "dev":  "chathealthy-website-dev",
    "qa":   "chathealthy-website-qa",
    "prod": "chathealthywebsite",
}
_CLOUDFLARE_BRANCH_FOR_WRANGLER: dict[str, str] = {
    "dev":  "dev",
    "qa":   "qa",
    "prod": "main",
}


def _step(msg: str) -> None:
    print(f"[local_publish] {msg}", flush=True)


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _require_clean_working_tree(repo_root: Path) -> str:
    """HEAD SHA pins the bytes we publish. Reject uncommitted state."""
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    if r.stdout.strip():
        sys.exit(
            "ERROR: working tree has uncommitted changes. Commit them "
            "first so HEAD SHA pins the bytes we publish.\n\n"
            f"{r.stdout}"
        )
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _load_env_kv(env_file: Path) -> dict[str, str]:
    kv: dict[str, str] = {}
    if not env_file.is_file():
        return kv
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        kv[k.strip()] = v.strip().strip('"').strip("'")
    return kv


# ─────────────────────────────────────────────────────────────────────
# HF Spaces — build image here, push to GHCR, set config, push thin
# Dockerfile to HF Space repo.
# ─────────────────────────────────────────────────────────────────────

def _hf_stage_workspace(repo_root: Path, target: TargetRecord, env: str) -> Path:
    target_id = target.target_id
    workspace = repo_root / "_oneshots" / "local_publish" / env / target_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)

    if target_id == "target_hf_space_findcare_backend":
        source_set = rd._findcare_source_set(repo_root)
        rd._build_react_frontend(repo_root, env)
    elif target_id == "target_hf_space_evaluatecare_backend":
        source_set = rd._evaluatecare_source_set(repo_root)
    elif target_id == "target_hf_space_shared_services":
        source_set = rd._sharedservices_source_set(repo_root)
    else:
        raise RuntimeError(f"unknown hf_space target {target_id!r}")

    for src_rel, dst_rel in source_set:
        _step(f"  stage  {src_rel} -> {dst_rel}")
        rd._copy_tree(repo_root, workspace, src_rel, dst_rel or src_rel)

    rd._write_hf_build_info(workspace, target_id, env)

    if target_id == "target_hf_space_findcare_backend":
        dist = (repo_root / "Code" / "ConversationalUX" / "FindCareChat"
                / "frontend" / "dist")
        static_dst = (workspace / "Code" / "ConversationalUX"
                      / "FindCareChat" / "backend" / "static")
        if static_dst.exists():
            shutil.rmtree(static_dst, ignore_errors=True)
        if not dist.is_dir():
            sys.exit(f"ERROR: React dist missing at {dist}")
        shutil.copytree(dist, static_dst)

    Extractor().materialize(target, workspace)

    if target_id == "target_hf_space_findcare_backend":
        shutil.copy2(
            workspace / "DevOps" / "FindCareBackend" / "Dockerfile",
            workspace / "Dockerfile",
        )
    return workspace


def _docker_build_then_push(workspace: Path, image_ref: str) -> None:
    _step(f"  docker build -t {image_ref} {workspace}")
    r = subprocess.run(
        ["docker", "build", "-t", image_ref, str(workspace)],
        capture_output=True, text=True, creationflags=_cflags(),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker build failed for {image_ref}\n"
            f"{(r.stderr or r.stdout)[-2000:]}"
        )
    _step(f"  docker push {image_ref}")
    r = subprocess.run(
        ["docker", "push", image_ref],
        capture_output=True, text=True, creationflags=_cflags(),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker push failed for {image_ref}\n"
            f"{(r.stderr or r.stdout)[-2000:]}\n"
            f"Hint: docker login ghcr.io -u <gh-user> -p <PAT-with-write:packages>"
        )


def _set_hf_config(target: TargetRecord, env: str, hf_token: str,
                   env_values: dict[str, str], repo_root: Path) -> None:
    target_id = target.target_id
    space = rd._hf_space_name(target_id, env)
    findcare_peer = rd._hf_peer_url("target_hf_space_findcare_backend",     env)
    evalcare_peer = rd._hf_peer_url("target_hf_space_evaluatecare_backend", env)
    shared_peer   = rd._hf_peer_url("target_hf_space_shared_services",       env)
    rd._hf_set_variable(hf_token, space, "ENV_PREFIX", env)
    if target_id == "target_hf_space_findcare_backend":
        rd._hf_set_variable(hf_token, space, "EVALCARE_URL", evalcare_peer)
        rd._hf_set_variable(hf_token, space, "SHARED_SERVICES_URL", shared_peer)
    elif target_id == "target_hf_space_evaluatecare_backend":
        rd._hf_set_variable(hf_token, space, "FINDCARE_URL", findcare_peer)
        rd._hf_set_variable(hf_token, space, "SHARED_SERVICES_URL", shared_peer)
    elif target_id == "target_hf_space_shared_services":
        rd._hf_set_variable(hf_token, space, "FINDCARE_URL", findcare_peer)
        rd._hf_set_variable(hf_token, space, "EVALCARE_URL", evalcare_peer)
        rd._hf_set_variable(hf_token, space, "FINDCARE_INTERNAL_URL", findcare_peer)

    certs_dir = repo_root / "Code" / "Shared" / "ops" / "certs"

    def _b64(p: Path) -> str:
        return base64.b64encode(p.read_bytes()).decode("ascii")
    rd._hf_set_variable(hf_token, space, "CA_CERT_PEM",       _b64(certs_dir / "ca.crt"))
    rd._hf_set_variable(hf_token, space, "FINDCARE_CERT_PEM", _b64(certs_dir / "findcare.crt"))
    rd._hf_set_variable(hf_token, space, "SHARED_CERT_PEM",   _b64(certs_dir / "shared.crt"))
    rd._hf_set_variable(hf_token, space, "EVALCARE_CERT_PEM", _b64(certs_dir / "evalcare.crt"))

    signing_map = {
        "target_hf_space_findcare_backend":     ("findcare.key", "FINDCARE_SIGNING_KEY_PEM", "FINDCARE_SIGNING_KEY_B64"),
        "target_hf_space_evaluatecare_backend": ("evalcare.key", "EVALCARE_SIGNING_KEY_PEM", "EVALCARE_SIGNING_KEY_B64"),
        "target_hf_space_shared_services":      ("shared.key",   "SHARED_SIGNING_KEY_PEM",   "SHARED_SIGNING_KEY_B64"),
    }
    key_file, secret_name, env_var_b64 = signing_map[target_id]
    signing_b64 = os.environ.get(env_var_b64) or env_values.get(env_var_b64)
    if not signing_b64:
        key_path = certs_dir / key_file
        if not key_path.is_file():
            sys.exit(
                f"ERROR: no {env_var_b64} in env AND no {key_path} on disk; "
                f"cannot ship signing key for {target_id}"
            )
        signing_b64 = _b64(key_path)
    rd._hf_set_secret(hf_token, space, secret_name, signing_b64)

    if target_id == "target_hf_space_findcare_backend":
        gemini = os.environ.get("GEMINI_API_KEY") or env_values.get("GEMINI_API_KEY")
        if not gemini:
            sys.exit("ERROR: GEMINI_API_KEY missing in env and .env; FindCare backend deploy aborted.")
        rd._hf_set_secret(hf_token, space, "GEMINI_API_KEY", gemini)

    if target_id == "target_hf_space_shared_services":
        google_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID") or env_values.get("GOOGLE_OAUTH_CLIENT_ID")
        if not google_id:
            sys.exit("ERROR: GOOGLE_OAUTH_CLIENT_ID missing in env and .env; SharedServices OAuth deploy aborted.")
        rd._hf_set_secret(hf_token, space, "GOOGLE_OAUTH_CLIENT_ID", google_id)
        google_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET") or env_values.get("GOOGLE_OAUTH_CLIENT_SECRET")
        if not google_secret:
            sys.exit("ERROR: GOOGLE_OAUTH_CLIENT_SECRET missing in env and .env; SharedServices OAuth deploy aborted.")
        rd._hf_set_secret(hf_token, space, "GOOGLE_OAUTH_CLIENT_SECRET", google_secret)


def _push_thin_dockerfile_to_hf_space(target: TargetRecord, env: str,
                                       hf_token: str, image_ref: str,
                                       port: int) -> None:
    target_id = target.target_id
    space = rd._hf_space_name(target_id, env)
    hf_clone = Path(tempfile.mkdtemp(prefix=f"hf_{space}_"))
    repo_url = f"https://{rd._HF_ORG}:{hf_token}@huggingface.co/spaces/{rd._HF_ORG}/{space}"
    _step(f"  clone https://huggingface.co/spaces/{rd._HF_ORG}/{space}")
    subprocess.run(
        ["git", "clone", repo_url, str(hf_clone)],
        check=True, capture_output=True,
    )
    readme = hf_clone / "README.md"
    readme_bytes: bytes | None = readme.read_bytes() if readme.is_file() else None
    for path in list(hf_clone.iterdir()):
        if path.name == ".git":
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    if readme_bytes is not None:
        readme.write_bytes(readme_bytes)
    thin = f"FROM {image_ref}\nEXPOSE {port}\n"
    (hf_clone / "Dockerfile").write_text(thin, encoding="utf-8")

    subprocess.run(
        ["git", "-c", "user.email=skip.snow@gmail.com", "-c", "user.name=SkipSnow",
         "add", "."],
        cwd=str(hf_clone), check=True,
    )
    diff = subprocess.run(
        ["git", "diff", "--staged", "--quiet"],
        cwd=str(hf_clone),
    )
    if diff.returncode == 0:
        _step(f"  no changes vs HF Space — skipping push")
    else:
        commit_msg = f"local_publish {env} -> {image_ref}"
        subprocess.run(
            ["git", "-c", "user.email=skip.snow@gmail.com", "-c", "user.name=SkipSnow",
             "commit", "-m", commit_msg],
            cwd=str(hf_clone), check=True,
        )
        _step(f"  push to {space}")
        subprocess.run(
            ["git", "push"], cwd=str(hf_clone), check=True,
        )
    shutil.rmtree(hf_clone, ignore_errors=True)


def _publish_hf_spaces(repo_root: Path, coll: DeploymentCollection, env: str,
                       sha7: str, hf_token: str, env_kv: dict[str, str]) -> list[str]:
    published: list[str] = []
    for target in coll:
        if env not in target.env_binding_set():
            continue
        if target.target_kind != "hf_space":
            continue
        port = _HF_APP_PORT[target.target_id]
        image_ref = _ghcr_image_ref(target.target_id, env, sha7)
        _step(f"=== hf_space {target.target_id} -> {image_ref} ===")
        workspace = _hf_stage_workspace(repo_root, target, env)
        _docker_build_then_push(workspace, image_ref)
        _set_hf_config(target, env, hf_token, env_kv, repo_root)
        _push_thin_dockerfile_to_hf_space(target, env, hf_token, image_ref, port)
        published.append(image_ref)
    return published


# ─────────────────────────────────────────────────────────────────────
# Cloudflare Pages — stage Website here, inline fonts, materialize
# JSON-owned bytes, push via npx wrangler from this machine.
# ─────────────────────────────────────────────────────────────────────

def _publish_cloudflare_pages(repo_root: Path, target: TargetRecord, env: str,
                              env_values: dict[str, str]) -> str:
    project = _CLOUDFLARE_PROJECT[env]
    branch_for_wrangler = _CLOUDFLARE_BRANCH_FOR_WRANGLER[env]
    _step(f"=== cloudflare_pages target={target.target_id} project={project} ===")

    api_token = (
        os.environ.get("CLOUDFLARE_API_TOKEN")
        or env_values.get("CLOUDFLARE_API_TOKEN")
        or env_values.get("CLOUDFLARE_ACCOUN_TOKEN", "")  # legacy typo in old .env
    )
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or env_values.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not api_token or not account_id:
        sys.exit("ERROR: CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID missing in env and Code/.env.")

    env_for_wrangler = dict(os.environ)
    env_for_wrangler["CLOUDFLARE_API_TOKEN"] = api_token
    env_for_wrangler["CLOUDFLARE_ACCOUNT_ID"] = account_id

    workspace = repo_root / "_oneshots" / "local_publish" / env / target.target_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    rd._copy_tree(repo_root, workspace, "Website", ".")
    snippet = ch_fonts_inliner.read_snippet()
    inlined = 0
    for html in workspace.rglob("*.html"):
        if ch_fonts_inliner.inline_into(html, snippet):
            inlined += 1
    _step(f"  CH_FONTS inlined in {inlined} pages in {workspace}")
    Extractor().materialize(target, workspace)

    cmd = [
        "npx", "wrangler", "pages", "deploy", str(workspace),
        f"--project-name={project}",
        f"--branch={branch_for_wrangler}",
        "--commit-dirty=true",
    ]
    _step(f"  {' '.join(cmd)}")
    subprocess.run(
        cmd, env=env_for_wrangler, check=True,
        shell=(sys.platform == "win32"),
    )
    return project


def _publish_cloudflare(repo_root: Path, coll: DeploymentCollection, env: str,
                        env_kv: dict[str, str]) -> list[str]:
    published: list[str] = []
    for target in coll:
        if env not in target.env_binding_set():
            continue
        if target.target_kind != "cloudflare_pages_project":
            continue
        project = _publish_cloudflare_pages(repo_root, target, env, env_kv)
        published.append(project)
    return published


# ─────────────────────────────────────────────────────────────────────
# Azure Functions pipeline — build zip here, push via az config-zip.
# ─────────────────────────────────────────────────────────────────────

_AZURE_EXCLUDE_DIR_NAMES = {"__pycache__", "venv", "venv_func",
                            ".pytest_cache", ".mypy_cache"}
_AZURE_EXCLUDE_FILE_NAMES = {"local.settings.json", ".env"}
_AZURE_EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _az_query(args: list[str], err_label: str) -> str:
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: {err_label} failed (exit {r.returncode})\n"
            f"  args: {' '.join(args)}\n"
            f"  stderr: {(r.stderr or '').strip()[:1000]}"
        )
    return r.stdout.strip()


def _block_if_active_orchestrations(rg: str, app: str, task_hub: str) -> None:
    _step("checking for active orchestrations …")
    master_key = _az_query(
        ["az", "functionapp", "keys", "list",
         "--resource-group", rg, "--name", app,
         "--query", "masterKey", "-o", "tsv"],
        err_label="az functionapp keys list",
    )
    if not master_key:
        sys.exit("ERROR: empty masterKey from Azure — cannot verify pipeline state.")
    default_host = _az_query(
        ["az", "functionapp", "show",
         "--resource-group", rg, "--name", app,
         "--query", "properties.defaultHostName", "-o", "tsv"],
        err_label="az functionapp show",
    )
    if not default_host:
        sys.exit("ERROR: empty defaultHostName from Azure — cannot verify pipeline state.")
    base = (
        f"https://{default_host}/runtime/webhooks/durabletask/instances"
        f"?taskHub={task_hub}&code={master_key}"
    )
    try:
        urllib.request.urlopen(f"{base}&runtimeStatus=Running", timeout=5).read()
    except Exception:
        pass

    def _query(status: str) -> list:
        deadline = time.time() + 360
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{base}&runtimeStatus={status}", timeout=20,
                ) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(body)
                    if isinstance(data, list):
                        return data
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
                pass
            _step(f"  waiting for warm-up to query {status} …")
            time.sleep(20)
        sys.exit(
            f"ERROR: could not query {status} orchestrations within 6 min; "
            "cannot verify pipeline is idle."
        )

    running = _query("Running")
    pending = _query("Pending")
    total = len(running) + len(pending)
    if total > 0:
        sys.exit(
            f"DEPLOY BLOCKED: {len(running)} Running + {len(pending)} "
            f"Pending orchestration(s) active on {app}. Terminate the "
            "running orchestrations before deploying."
        )
    _step(f"  no active orchestrations on {app} — safe to deploy")


def _build_pipeline_zip(repo_root: Path, sha7: str) -> Path:
    src = repo_root / "pipeline" / "Code"
    if not src.is_dir():
        sys.exit(f"ERROR: pipeline source dir missing: {src}")
    src_req = src / "requirements-pipeline.txt"
    if not src_req.is_file():
        sys.exit(f"ERROR: requirements-pipeline.txt missing at {src_req}")

    out_dir = repo_root / "_oneshots" / "local_publish" / "azure"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"deploy_{sha7}.zip"
    if zip_path.exists():
        zip_path.unlink()

    requirements_bytes = src_req.read_bytes()

    _step(f"building zip from {src} → {zip_path}")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("requirements.txt", requirements_bytes)
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            parts = set(path.parts)
            if parts & _AZURE_EXCLUDE_DIR_NAMES:
                continue
            if path.name in _AZURE_EXCLUDE_FILE_NAMES:
                continue
            if path.suffix.lower() in _AZURE_EXCLUDE_SUFFIXES:
                continue
            if path.name in ("requirements.txt", "requirements-pipeline.txt"):
                continue
            rel = path.relative_to(src)
            zf.write(path, arcname=str(rel).replace("\\", "/"))

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    _step(f"  zip built: {zip_path.name} ({size_mb:.1f} MB)")
    return zip_path


def _az_push_zip(rg: str, app: str, zip_path: Path) -> None:
    _step(f"pushing zip to Azure: {app}")
    r = subprocess.run(
        ["az", "functionapp", "deployment", "source", "config-zip",
         "--resource-group", rg, "--name", app,
         "--src", str(zip_path)],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az config-zip failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}\n"
            f"  stdout: {(r.stdout or '').strip()[:500]}"
        )
    _step(f"  config-zip pushed to {app}")


def _az_restart(rg: str, app: str) -> None:
    _step(f"restarting {app} (drain sys.modules cache)")
    r = subprocess.run(
        ["az", "functionapp", "restart",
         "--resource-group", rg, "--name", app],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az functionapp restart failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1000]}"
        )
    _step("  restart requested")


def _publish_azure_pipeline(repo_root: Path, env: str, sha7: str) -> str:
    if env not in _AZURE_DEPLOY_TARGETS:
        sys.exit(
            f"ERROR: no Azure deploy target configured for env={env!r}. "
            f"Operator must add resource_group / function_app / task_hub "
            f"to _AZURE_DEPLOY_TARGETS in local_publish.py before "
            f"publishing to {env}."
        )
    target = _AZURE_DEPLOY_TARGETS[env]
    rg = target["resource_group"]
    app = target["function_app"]
    task_hub = target["task_hub"]
    _step(f"=== azure_function_app {app} (rg={rg}) ===")
    _block_if_active_orchestrations(rg, app, task_hub)
    zip_path = _build_pipeline_zip(repo_root, sha7)
    _az_push_zip(rg, app, zip_path)
    _az_restart(rg, app)
    return f"{app}@{sha7}"


# ─────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Operator build manager — build deploy artifacts here, "
                    "ship to cloud destinations."
    )
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument(
        "--target", default="all",
        choices=["all", "hf", "cloudflare", "azure"],
        help="Which deploy targets to publish. Default: all.",
    )
    args = parser.parse_args(argv)
    env = args.env
    target_filter = args.target

    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} env={env} target={target_filter}")

    sha7 = _require_clean_working_tree(repo_root)
    _step(f"HEAD={sha7} (clean tree)")

    # Gate: Crosswalk against the working tree (= HEAD; clean).
    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    backlog_schema = repo_root / "Website" / "schemas" / "ChatHealthyAgileBacklogSchema.json"
    backlog_path = repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
    env_file = repo_root / "Code" / ".env"

    backlog = AgileBacklogLoader(schema_uri=backlog_schema).load(backlog_path)
    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    env_values_for_leak: set[str] = (
        SecretsResolver().env_values_for_leak_check(env_file)
        if env_file.is_file() else set()
    )
    report = Crosswalk().check(
        coll=coll, backlog=backlog, repo_root=repo_root,
        env_values=env_values_for_leak,
    )
    if not report.is_pass:
        sys.stderr.write(report.format() + "\n")
        return report.exit_code()
    _step(f"crosswalk gate passed (targets={len(coll)}, violations=0)")

    env_kv = _load_env_kv(env_file)

    published: list[str] = []

    if target_filter in ("all", "azure"):
        published.append(_publish_azure_pipeline(repo_root, env, sha7))

    if target_filter in ("all", "hf"):
        hf_token = os.environ.get("HF_TOKEN") or env_kv.get("HF_TOKEN", "")
        if not hf_token:
            sys.exit("ERROR: HF_TOKEN missing in env and Code/.env; cannot publish HF Spaces.")
        published.extend(_publish_hf_spaces(repo_root, coll, env, sha7, hf_token, env_kv))

    if target_filter in ("all", "cloudflare"):
        published.extend(_publish_cloudflare(repo_root, coll, env, env_kv))

    if not published:
        sys.exit(
            f"ERROR: nothing published for env={env!r} target={target_filter!r}. "
            f"Check env_binding in deployment_architecture.json + Azure target map."
        )
    _step(f"published {len(published)} artifact(s):")
    for ref in published:
        _step(f"  {ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
