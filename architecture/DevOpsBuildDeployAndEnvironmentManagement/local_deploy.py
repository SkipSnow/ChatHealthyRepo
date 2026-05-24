"""local_deploy.py - operator's deploy manager (all envs).

Single operator-facing deploy entry point per REQ-T-049 (symmetric 4):
ships per-target build packages from local_build/v{N}/<target_id>/ to
their cloud destinations, AND stands up the local host stack for
--env local. Reads each per-target package manifest for the target's
facts; resolves secret VALUES from the bound store via SecretsResolver
(bindings constructed from deployment_architecture.json's per-target
`secrets` map per REQ-T-038 / REQ-T-052). The package itself contains
NO secret values (REQ-T-053).

usage:
    python local_deploy.py --env local
    python local_deploy.py --env dev --target cloudflare
    python local_deploy.py --env qa  --version v42
    python local_deploy.py --env prod --target target_cloudflare_pages_website

If --version is omitted, the highest v{N}/ directory present on disk
under local_build/ is used.

--env local: instantiates LocalDeploy() (absorbed from the legacy
`old_local_deploy.py`) and runs the full host-stack lifecycle per
EPIC-008-F-012-S-001 REQ-T-001..T-008 (port table, atomic teardown,
docker build + run for the 3 backends, host-OS Website wrapper, wait /
verify / smoke / bump). --env dev|qa|prod: ships per-target packages
to their cloud destinations (cloudflare wrangler, HF Space docker push,
Azure FA config-zip).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ch_fonts_inliner
import hf_helpers as rd
from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord


_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/local_build")

# Per-env Cloudflare Pages project map. (The branch flag is read from the
# manifest's environments[].branch field per REQ-T-050 — no hard-coded
# branch map here.)
_CLOUDFLARE_PROJECT: dict[str, str] = {
    "dev":  "chathealthy-website-dev",
    "qa":   "chathealthy-website-qa",
    "prod": "chathealthywebsite",
}

_GHCR_OWNER: str = "skipsnow"
_GHCR_IMAGE_NAME: dict[str, str] = {
    "target_hf_space_findcare_backend":     "findcare-backend",
    "target_hf_space_evaluatecare_backend": "evaluatecare-backend",
    "target_hf_space_shared_services":      "sharedservices-backend",
}
_HF_APP_PORT: dict[str, int] = {
    "target_hf_space_findcare_backend":     7860,
    "target_hf_space_evaluatecare_backend": 7860,
    "target_hf_space_shared_services":      7860,
}
_HF_SIGNING_VAR: dict[str, tuple[str, str]] = {
    # target_id -> (secret_name_in_manifest, secret_name_pushed_to_hf)
    "target_hf_space_findcare_backend":     ("FINDCARE_SIGNING_KEY_B64", "FINDCARE_SIGNING_KEY_PEM"),
    "target_hf_space_evaluatecare_backend": ("EVALCARE_SIGNING_KEY_B64", "EVALCARE_SIGNING_KEY_PEM"),
    "target_hf_space_shared_services":      ("SHARED_SIGNING_KEY_B64",   "SHARED_SIGNING_KEY_PEM"),
}

_PIPELINE_SOURCE_PREFIX = "pipeline/Code/"
_AZURE_REQUIREMENTS_SRC = "pipeline/Code/requirements-pipeline.txt"
_AZURE_REQUIREMENTS_ZIP_PATH = "requirements.txt"


def _step(msg: str) -> None:
    print(f"[local_deploy] {msg}", flush=True)


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _require_local_context() -> None:
    """REQ-T-055 — local_deploy MUST run only on an operator's workstation,
    never on a GitHub Actions runner. For CI deploys use remote_deploy.py."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.exit(
            "ERROR: local_deploy.py MUST NOT run on a GitHub Actions runner. "
            "Use remote_deploy.py instead (REQ-T-055)."
        )


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _resolve_version(repo_root: Path, version_arg: str | None) -> int:
    build_root = repo_root / _BUILD_ROOT_REL
    if not build_root.is_dir():
        sys.exit(
            f"ERROR: build root {build_root} does not exist. "
            f"Run local_build.py first."
        )
    if version_arg is not None:
        m = re.match(r"^v(\d+)$", version_arg)
        if not m:
            sys.exit(f"ERROR: --version must be vN (e.g., v42), got {version_arg!r}")
        n = int(m.group(1))
        if not (build_root / f"v{n}").is_dir():
            sys.exit(f"ERROR: {build_root / f'v{n}'} does not exist.")
        return n
    versions = []
    for child in build_root.iterdir():
        if not child.is_dir():
            continue
        m = re.match(r"^v(\d+)$", child.name)
        if m:
            versions.append(int(m.group(1)))
    if not versions:
        sys.exit(f"ERROR: no v{{N}} build directories under {build_root}.")
    return max(versions)


def _load_target_manifest(repo_root: Path, build_n: int, target_id: str) -> dict:
    path = repo_root / _BUILD_ROOT_REL / f"v{build_n}" / target_id / "manifest.json"
    if not path.is_file():
        sys.exit(f"ERROR: target manifest missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ═════════════════════════════════════════════════════════════════════════
# Cloudflare Pages handler (--env dev|qa|prod, target_kind=cloudflare_pages_project)
# ═════════════════════════════════════════════════════════════════════════

def _deploy_cloudflare(
    build_dir: Path,
    env: str,
    resolver: SecretsResolver,
    target: TargetRecord,
) -> str:
    project = _CLOUDFLARE_PROJECT[env]
    env_binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if env_binding is None or not env_binding.branch:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} has no `branch` "
            f"declared in deployment_architecture.json (REQ-T-050). The deploy "
            f"script reads the branch from the manifest; no hard-coded fallback."
        )
    branch = env_binding.branch
    _step(f"=== cloudflare_pages env={env} project={project} branch={branch} dir={build_dir} ===")
    api_token = resolver.resolve("CLOUDFLARE_API_TOKEN", env)
    account_id = resolver.resolve("CLOUDFLARE_ACCOUNT_ID", env)
    env_for_wrangler = dict(os.environ)
    env_for_wrangler["CLOUDFLARE_API_TOKEN"] = api_token
    env_for_wrangler["CLOUDFLARE_ACCOUNT_ID"] = account_id
    cmd = [
        "npx", "wrangler", "pages", "deploy", str(build_dir),
        f"--project-name={project}",
        f"--branch={branch}",
        "--commit-dirty=true",
    ]
    _step(f"  {' '.join(cmd)}")
    subprocess.run(
        cmd, env=env_for_wrangler, check=True,
        shell=(sys.platform == "win32"),
    )
    return project


# ═════════════════════════════════════════════════════════════════════════
# HF Space handler (--env dev|qa|prod, target_kind=hf_space)
# ═════════════════════════════════════════════════════════════════════════

def _ghcr_image_ref(target_id: str, env: str, build_n: int) -> str:
    return f"ghcr.io/{_GHCR_OWNER}/{_GHCR_IMAGE_NAME[target_id]}:{env}-v{build_n}"


def _docker_build_then_push(build_dir: Path, image_ref: str) -> None:
    _step(f"  docker build -t {image_ref} {build_dir}")
    r = subprocess.run(
        ["docker", "build", "-t", image_ref, str(build_dir)],
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


def _set_hf_config(
    repo_root: Path,
    target_id: str,
    env: str,
    hf_token: str,
    resolver: SecretsResolver,
) -> None:
    """Set the HF Space's variables (computed config) + secrets (from store)."""
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

    manifest_name, hf_secret_name = _HF_SIGNING_VAR[target_id]
    signing_b64 = resolver.resolve(manifest_name, env)
    rd._hf_set_secret(hf_token, space, hf_secret_name, signing_b64)

    if target_id == "target_hf_space_findcare_backend":
        rd._hf_set_secret(hf_token, space, "GEMINI_API_KEY", resolver.resolve("GEMINI_API_KEY", env))
    if target_id == "target_hf_space_shared_services":
        rd._hf_set_secret(hf_token, space, "GOOGLE_OAUTH_CLIENT_ID",     resolver.resolve("GOOGLE_OAUTH_CLIENT_ID", env))
        rd._hf_set_secret(hf_token, space, "GOOGLE_OAUTH_CLIENT_SECRET", resolver.resolve("GOOGLE_OAUTH_CLIENT_SECRET", env))


def _push_thin_dockerfile_to_hf_space(
    target_id: str, env: str, hf_token: str, image_ref: str, port: int,
) -> None:
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
        ["git", "diff", "--staged", "--quiet"], cwd=str(hf_clone),
    )
    if diff.returncode == 0:
        _step("  no changes vs HF Space — skipping push")
    else:
        subprocess.run(
            ["git", "-c", "user.email=skip.snow@gmail.com", "-c", "user.name=SkipSnow",
             "commit", "-m", f"local_deploy {env} -> {image_ref}"],
            cwd=str(hf_clone), check=True,
        )
        _step(f"  push to {space}")
        subprocess.run(["git", "push"], cwd=str(hf_clone), check=True)
    shutil.rmtree(hf_clone, ignore_errors=True)


def _deploy_hf_space(
    repo_root: Path,
    build_dir: Path,
    build_n: int,
    target_id: str,
    env: str,
    resolver: SecretsResolver,
) -> str:
    port = _HF_APP_PORT[target_id]
    image_ref = _ghcr_image_ref(target_id, env, build_n)
    _step(f"=== hf_space {target_id} env={env} -> {image_ref} ===")
    _docker_build_then_push(build_dir, image_ref)
    hf_token = resolver.resolve("HF_TOKEN", env)
    _set_hf_config(repo_root, target_id, env, hf_token, resolver)
    _push_thin_dockerfile_to_hf_space(target_id, env, hf_token, image_ref, port)
    return image_ref


# ═════════════════════════════════════════════════════════════════════════
# Azure Function App handler (--env dev|qa|prod, target_kind=azure_function_app)
# ═════════════════════════════════════════════════════════════════════════

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
    """Reject the deploy if any orchestration is Running or Pending on
    this FA. The pipeline cannot be reloaded mid-run safely."""
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
        sys.exit("ERROR: empty defaultHostName from Azure.")
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
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, json.JSONDecodeError):
                pass
            _step(f"  waiting for warm-up to query {status} …")
            time.sleep(20)
        sys.exit(
            f"ERROR: could not query {status} orchestrations within 6 min."
        )

    running = _query("Running")
    pending = _query("Pending")
    total = len(running) + len(pending)
    if total > 0:
        sys.exit(
            f"DEPLOY BLOCKED: {len(running)} Running + {len(pending)} "
            f"Pending orchestration(s) active on {app}. Terminate them "
            "before deploying."
        )
    _step(f"  no active orchestrations on {app} — safe to deploy")


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


def _deploy_azure_function_app(
    build_dir: Path,
    target: TargetRecord,
    env: str,
) -> str:
    env_binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if env_binding is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} has no env_binding "
            f"matching {env!r}; cannot deploy."
        )
    if env_binding.azure is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} is missing "
            f"the `azure` sub-object (resource_group / function_app / "
            f"task_hub) in deployment_architecture.json."
        )
    rg = env_binding.azure["resource_group"]
    app = env_binding.azure["function_app"]
    task_hub = env_binding.azure["task_hub"]
    _step(f"=== azure_function_app {target.target_id} env={env} -> {app} (rg={rg}) ===")
    zip_path = build_dir / "deploy.zip"
    if not zip_path.is_file():
        sys.exit(f"ERROR: deploy.zip missing at {zip_path}")
    _block_if_active_orchestrations(rg, app, task_hub)
    _az_push_zip(rg, app, zip_path)
    return f"{app}"


# ═════════════════════════════════════════════════════════════════════════
# Cloud dispatch (--env dev|qa|prod)
# ═════════════════════════════════════════════════════════════════════════

def _deploy_one(
    repo_root: Path,
    build_n: int,
    target_id: str,
    target_kind: str,
    env: str,
    resolver: SecretsResolver,
    coll: DeploymentCollection,
) -> str:
    build_dir = repo_root / _BUILD_ROOT_REL / f"v{build_n}" / target_id
    if not build_dir.is_dir():
        sys.exit(f"ERROR: build dir missing: {build_dir}")
    if target_kind == "cloudflare_pages_project":
        target = coll.by_target_id(target_id)
        return _deploy_cloudflare(build_dir, env, resolver, target)
    if target_kind == "hf_space":
        return _deploy_hf_space(repo_root, build_dir, build_n, target_id, env, resolver)
    if target_kind == "azure_function_app":
        target = coll.by_target_id(target_id)
        return _deploy_azure_function_app(build_dir, target, env)
    raise RuntimeError(
        f"target_kind {target_kind!r} not supported in local_deploy."
    )


_DEPLOYABLE_KINDS = ("cloudflare_pages_project", "hf_space", "azure_function_app")


def _select_target_ids(coll: DeploymentCollection, target_arg: str) -> list[tuple[str, str]]:
    """Return [(target_id, target_kind), ...] matching the filter."""
    if target_arg == "all":
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind in _DEPLOYABLE_KINDS
        ]
    if target_arg in ("cloudflare", "cloudflare_pages_project"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "cloudflare_pages_project"
        ]
    if target_arg in ("hf", "hf_space"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "hf_space"
        ]
    if target_arg in ("azure", "azure_function_app"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "azure_function_app"
        ]
    for t in coll:
        if t.target_id == target_arg:
            return [(t.target_id, t.target_kind)]
    return []


def _run_cloud_deploy(env: str, version: str | None, target_arg: str) -> int:
    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} env={env} target={target_arg}")
    build_n = _resolve_version(repo_root, version)
    _step(f"version=v{build_n}")
    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    env_file = repo_root / "Code" / ".env"
    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    resolver = SecretsResolver.from_collection(coll, env_file=env_file)
    selected = _select_target_ids(coll, target_arg)
    if not selected:
        sys.exit(f"ERROR: no targets matched --target={target_arg!r}")
    by_id = {t.target_id: t for t in coll}
    deployed: list[str] = []
    for target_id, target_kind in selected:
        target = by_id[target_id]
        if env not in target.env_binding_set():
            _step(f"  skip {target_id}: no env_binding for {env!r}")
            continue
        deployed.append(_deploy_one(
            repo_root, build_n, target_id, target_kind, env, resolver, coll,
        ))
    if not deployed:
        sys.exit(
            f"ERROR: nothing deployed for env={env!r} target={target_arg!r}. "
            f"Check env_binding in deployment_architecture.json."
        )
    _step(f"deployed {len(deployed)} target(s):")
    for d in deployed:
        _step(f"  {d}")
    return 0


# ═════════════════════════════════════════════════════════════════════════
# LocalDeploy class (--env local; subsumes legacy old_local_deploy.LocalDeploy)
# Per EPIC-008-F-012-S-001 REQ-T-001..T-008 + REQ-B-001..B-007.
# ═════════════════════════════════════════════════════════════════════════

_REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"


class LocalDeploy:
    """Local deploy per V11 EPIC-008-F-004 S-001 + S-002 + EPIC-008-F-012-S-001.

    Backends run as Docker containers per V11 S-002-REQ-T-001. The Website
    wrapper runs as a host-OS process per V11 S-002-REQ-T-002 (intentional
    Docker exception). Smoke test invoked after build/start/verify per
    REQ-B-004.
    """

    # REQ-T-001 — canonical port assignments. DO NOT restate.
    PORTS = {
        "http":     80,
        "https":    443,
        "findcare": 7860,
        "evalcare": 8001,
        "shared":   8002,
    }

    # container_name -> (port label, src_dir relative to repo root, build_context relative)
    BACKEND_CONTAINERS = {
        "ch-findcare":  ("findcare", "DevOps/FindCareBackend",   "."),
        "ch-evalcare":  ("evalcare", "evaluateCare/Code",        "evaluateCare/Code"),
        "ch-sharedsvc": ("shared",   "sharedServices/Code",      "sharedServices/Code"),
    }

    def __init__(self) -> None:
        if _REPO_ROOT_ENV not in os.environ:
            sys.exit(f"ERROR: {_REPO_ROOT_ENV} env var not set. Cannot resolve paths.")
        self.env = "local"
        self.repo_root = Path(os.environ[_REPO_ROOT_ENV]).resolve()
        self.deploy_dir = Path(__file__).resolve().parent
        self.frontend_dir = (self.repo_root / "Code" / "ConversationalUX"
                             / "FindCareChat" / "frontend")
        self.backend_dir = (self.repo_root / "Code" / "ConversationalUX"
                            / "FindCareChat" / "backend")
        self.website_dir = self.repo_root / "Website"
        self.certs_dir = self.repo_root / "Code" / "Shared" / "ops" / "certs"
        self.output_dir = self.repo_root / "_oneshots/test_output" / "deploy"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        self.website_staging_dir = self.output_dir / f"website_{ts}"
        self.output_path = (self.output_dir / f"deploy_local_{ts}.json")
        self.results: dict = {
            "env": self.env,
            "started_at": ts,
            "ports": dict(self.PORTS),
            "steps": [],
            "verification": [],
            "smoke_rc": None,
            "smoke_passed": None,
            "structured_output_path": str(self.output_path),
        }
        self.backend_procs: list[subprocess.Popen] = []

    # REQ-B-005 — step notices
    def _step_notice(self, msg: str) -> None:
        line = f"[STEP {self.env}] {msg}"
        print(line, flush=True)
        self.results["steps"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
        })

    def _deployment_architecture_gate(self) -> None:
        backlog_path = self.repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
        backlog_schema = self.repo_root / "Website" / "schemas" / "ChatHealthyAgileBacklogSchema.json"
        deployment_path = self.repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
        env_path = self.repo_root / "Code" / ".env"
        coll = RecordLoader().load_collection(deployment_path)
        backlog = AgileBacklogLoader(schema_uri=backlog_schema).load(backlog_path)
        env_values: set[str] = set()
        if env_path.is_file():
            env_values = SecretsResolver().env_values_for_leak_check(env_path)
        report = Crosswalk().check(
            coll=coll, backlog=backlog,
            repo_root=self.repo_root, env_values=env_values,
        )
        if not report.is_pass:
            sys.stderr.write(report.format() + "\n")
            sys.exit(report.exit_code())
        self._step_notice(
            f"deployment-architecture gate passed "
            f"(targets={len(coll)}, violations=0)"
        )

    def _lint_workflow_yamls(self) -> None:
        import yaml as _yaml
        workflow_dir = self.repo_root / ".github" / "workflows"
        if not workflow_dir.is_dir():
            sys.exit(
                f"Deploy aborted — .github/workflows directory missing at "
                f"{workflow_dir} (CHATHEALTHY_PROJECT_ROOT may be wrong)"
            )
        yml_paths = sorted(
            list(workflow_dir.glob("*.yml")) + list(workflow_dir.glob("*.yaml"))
        )
        failures: list[str] = []
        for path in yml_paths:
            try:
                with path.open("r", encoding="utf-8") as f:
                    _yaml.safe_load(f)
            except _yaml.YAMLError as exc:
                failures.append(f"{path}: {exc}")
            except OSError as exc:
                failures.append(f"{path}: cannot read ({type(exc).__name__}: {exc})")
        if failures:
            sys.exit(
                "Deploy aborted — workflow yaml lint failed:\n  "
                + "\n  ".join(failures)
            )
        self._step_notice(f"workflow yaml lint passed ({len(yml_paths)} files)")

    # REQ-T-005 — atomic teardown precondition
    def _teardown_precondition(self) -> None:
        cflags = _cflags()
        for container_name in self.BACKEND_CONTAINERS:
            result = subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True, text=True, creationflags=cflags,
            )
            if result.returncode == 0:
                self._step_notice(f"docker stopped {container_name}")
        killed_pids = set()
        for port in self.PORTS.values():
            for pid in self._pids_listening_on(port):
                if pid == os.getpid():
                    continue
                try:
                    psutil.Process(pid).kill()
                    killed_pids.add(pid)
                except psutil.NoSuchProcess:
                    pass
        for stale in (self.frontend_dir / "dist",
                      self.frontend_dir / "node_modules" / ".vite"):
            if stale.exists():
                shutil.rmtree(stale)
        time.sleep(2)
        not_clear = [p for p in self.PORTS.values() if self._port_in_use(p)]
        if not_clear:
            sys.exit(f"ERROR: ports still in use after teardown: {not_clear}")
        self.results["steps"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": f"teardown killed pids: {sorted(killed_pids) or 'none'}",
        })

    def _pids_listening_on(self, port: int) -> list[int]:
        return [c.pid for c in psutil.net_connections(kind="inet")
                if c.status == psutil.CONN_LISTEN and c.laddr.port == port and c.pid]

    def _port_in_use(self, port: int) -> bool:
        return len(self._pids_listening_on(port)) > 0

    def _validate_prerequisites(self) -> None:
        required_certs = [
            "localhost.crt", "localhost.key",
            "findcare.crt", "findcare.key",
            "evalcare.crt", "evalcare.key",
            "shared.crt", "shared.key",
            "ca.crt",
        ]
        missing = [c for c in required_certs if not (self.certs_dir / c).is_file()]
        if missing:
            sys.exit(f"ERROR: missing certs in {self.certs_dir}: {missing}")
        if not shutil.which("node"):
            sys.exit("ERROR: node not on PATH")
        if not shutil.which("python"):
            sys.exit("ERROR: python not on PATH")

    def _ensure_docker_available(self) -> None:
        """V11 S-002-REQ-T-001 — Docker mandatory; no Python-subprocess fallback."""
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True, text=True, timeout=15, creationflags=_cflags(),
        )
        if result.returncode != 0:
            sys.exit(
                "ERROR: Docker daemon not available. "
                "FIX: open Docker Desktop and re-run this script. "
                f"`docker version` exit={result.returncode}, "
                f"stderr={result.stderr.strip()[:300]}"
            )

    def _write_build_info(self, build_ctx_abs: Path, container_name: str) -> Path:
        cflags = _cflags()
        try:
            build_num = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"],
                cwd=str(self.repo_root), capture_output=True, text=True,
                creationflags=cflags, check=True,
            ).stdout.strip()
        except Exception:
            build_num = "0"
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(self.repo_root), capture_output=True, text=True,
                creationflags=cflags, check=True,
            ).stdout.strip()
        except Exception:
            commit = "unknown"
        info = {
            "build": int(build_num) if build_num.isdigit() else None,
            "commit": commit,
            "env": self.env,
            "service": container_name,
            "version": "1.4.1",
            "framework": "0.1.5",
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        out = build_ctx_abs / "build_info.json"
        out.write_text(json.dumps(info, indent=2), encoding="utf-8")
        return out

    def _build_backend_containers(self) -> None:
        """V11 S-002-REQ-T-001 docker build for the 3 backends."""
        import shutil as _shutil
        frontend_lib_src = self.repo_root / "FrontEndApplicationLib"
        auth_src = self.repo_root / "architecture" / "AuthorizationsAndAuthentications"
        specialty_filter_src = self.repo_root / "FindCare" / "SpecialtyFilter"
        for container_name, entry in self.BACKEND_CONTAINERS.items():
            _label, src_dir, build_ctx_rel = entry
            image_tag = container_name
            dockerfile_abs = self.repo_root / src_dir / "Dockerfile"
            if not dockerfile_abs.is_file():
                sys.exit(
                    f"ERROR: Dockerfile missing at {dockerfile_abs}. "
                    "V11 S-002-REQ-T-001 requires Dockerfile per backend."
                )
            build_ctx_abs = self.repo_root if build_ctx_rel == "." else (self.repo_root / build_ctx_rel)
            staged_lib = None
            if build_ctx_rel != ".":
                staged_lib = build_ctx_abs / "FrontEndApplicationLib"
                if staged_lib.exists():
                    _shutil.rmtree(staged_lib)
                _shutil.copytree(frontend_lib_src, staged_lib,
                                 ignore=_shutil.ignore_patterns("__pycache__", "*.pyc"))
            staged_auth = None
            if container_name == "ch-sharedsvc" and auth_src.is_dir():
                staged_auth = build_ctx_abs / "authentication"
                if staged_auth.exists():
                    _shutil.rmtree(staged_auth)
                _shutil.copytree(
                    auth_src, staged_auth,
                    ignore=_shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "ArchitectureDesignAndAuditDocs",
                    ),
                )
            staged_specialty_filter = None
            if container_name == "ch-sharedsvc" and specialty_filter_src.is_dir():
                staged_specialty_filter = build_ctx_abs / "SpecialtyFilter"
                if staged_specialty_filter.exists():
                    _shutil.rmtree(staged_specialty_filter)
                _shutil.copytree(
                    specialty_filter_src, staged_specialty_filter,
                    ignore=_shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "*.docx", "*.tsx", "*.ts",
                    ),
                )
            build_info_path = self._write_build_info(build_ctx_abs, container_name)
            self._step_notice(
                f"building image {image_tag} (-f {src_dir}/Dockerfile, "
                f"context={build_ctx_rel})"
            )
            try:
                result = subprocess.run(
                    ["docker", "build", "-t", image_tag,
                     "-f", str(dockerfile_abs), str(build_ctx_abs)],
                    cwd=str(self.repo_root),
                    capture_output=True, text=True, creationflags=_cflags(),
                )
            finally:
                if staged_lib is not None and staged_lib.exists():
                    _shutil.rmtree(staged_lib)
                if staged_auth is not None and staged_auth.exists():
                    _shutil.rmtree(staged_auth)
                if staged_specialty_filter is not None and staged_specialty_filter.exists():
                    _shutil.rmtree(staged_specialty_filter)
                if build_info_path.is_file():
                    build_info_path.unlink()
            if result.returncode != 0:
                sys.exit(
                    f"ERROR: docker build failed for {image_tag}: "
                    f"{result.stderr.strip()[:500]}"
                )

    def _stage_wrapper_website(self) -> None:
        n = ch_fonts_inliner.stage_and_inline(
            self.website_dir, self.website_staging_dir
        )
        self._step_notice(
            f"website staged -> {self.website_staging_dir} (CH_FONTS inlined in {n} pages)"
        )

    # REQ-T-008 — React frontend build (high-miss step)
    def _build_react_frontend(self) -> None:
        self._step_notice("building React frontend (high-miss step)")
        api_url = ""
        evalcare_url = f"https://localhost:{self.PORTS['evalcare']}"
        env = os.environ.copy()
        env["VITE_API_URL"] = api_url
        env["VITE_EVALCARE_URL"] = evalcare_url
        canonical_vite = self.deploy_dir / "vite.config.ts"
        vite_copy = self.frontend_dir / "vite.config.ts"
        if not canonical_vite.is_file():
            sys.exit(f"ERROR: canonical vite config missing at {canonical_vite}")
        shutil.copy2(canonical_vite, vite_copy)
        try:
            subprocess.run(
                ["npm", "ci", "--silent"],
                cwd=self.frontend_dir, env=env, check=True,
                shell=(sys.platform == "win32"),
            )
            subprocess.run(
                ["npm", "run", "build"],
                cwd=self.frontend_dir, env=env, check=True,
                shell=(sys.platform == "win32"),
            )
        except subprocess.CalledProcessError as e:
            sys.exit(f"ERROR: React build failed: {e}")
        finally:
            if vite_copy.is_file():
                vite_copy.unlink()
        dist_index = self.frontend_dir / "dist" / "index.html"
        if not dist_index.is_file():
            sys.exit(f"ERROR: React build produced no {dist_index}")
        if not ch_fonts_inliner.inline_into(dist_index):
            sys.exit(f"ERROR: CH_FONTS marker not found in {dist_index}")
        backend_static = self.backend_dir / "static"
        for old in ("assets", "index.html"):
            old_path = backend_static / old
            if old_path.is_dir():
                shutil.rmtree(old_path)
            elif old_path.is_file():
                old_path.unlink()
        backend_static.mkdir(parents=True, exist_ok=True)
        for item in (self.frontend_dir / "dist").iterdir():
            if item.is_dir():
                shutil.copytree(item, backend_static / item.name)
            else:
                shutil.copy2(item, backend_static / item.name)

    def _start_backend_processes(self) -> None:
        """V11 S-002-REQ-T-001 docker run + S-002-REQ-T-002 host-OS Website."""
        certs_host = str(self.certs_dir).replace("\\", "/")
        env_file = self.repo_root / "Code" / ".env"
        if not env_file.is_file():
            sys.exit(
                f"ERROR: env file missing at {env_file}; backend containers "
                "depend on it for MongoDB / API credentials."
            )
        from dotenv import dotenv_values
        env_dict = dotenv_values(env_file)
        env_args: list[str] = []
        for k, v in env_dict.items():
            if v is None:
                continue
            env_args.extend(["-e", f"{k}={v}"])
        cflags = _cflags()
        for container_name, (label, _src_dir, _build_ctx) in self.BACKEND_CONTAINERS.items():
            host_port = self.PORTS[label]
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True, creationflags=cflags,
            )
            extra_env: list[str] = []
            if container_name == "ch-sharedsvc":
                extra_env.extend([
                    "-e", f"FINDCARE_INTERNAL_URL=https://host.docker.internal:{self.PORTS['findcare']}",
                ])
            run_cmd = (
                ["docker", "run", "-d",
                 "--name", container_name,
                 "--add-host", "host.docker.internal:host-gateway",
                 "-p", f"{host_port}:7860",
                 "-v", f"{certs_host}:/certs:ro"]
                + env_args + extra_env + [container_name]
            )
            run_result = subprocess.run(
                run_cmd, capture_output=True, text=True, creationflags=cflags,
            )
            if run_result.returncode != 0:
                sys.exit(
                    f"ERROR: docker run {container_name} failed: "
                    f"{run_result.stderr.strip()[:500]}"
                )
            self._step_notice(
                f"docker run {container_name} -> host port {host_port}"
            )
        certs_arg = str(self.certs_dir)
        website_arg = str(self.website_staging_dir)
        log_dir = self.output_dir / "process_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = (log_dir / f"website-80-443_{self.results['started_at']}.log")
        log_fh = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(self.deploy_dir / "_start_website.py"),
             certs_arg, website_arg],
            cwd=str(self.repo_root),
            stdout=log_fh, stderr=subprocess.STDOUT,
            creationflags=(
                (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
                if sys.platform == "win32" else 0
            ),
        )
        self.backend_procs.append(proc)
        self._step_notice(
            f"started website pid={proc.pid} log={log_file.name} "
            "(host-OS per V11 S-002-REQ-T-002)"
        )

    # REQ-B-003 — wait for components
    def _wait_for_all_components(self, timeout_s: int = 180) -> None:
        checks = [
            ("findcare",
             f"https://localhost:{self.PORTS['findcare']}/health",
             lambda t: '"status":"ok"' in t),
            ("evalcare",
             f"https://localhost:{self.PORTS['evalcare']}/health",
             lambda t: '"service":"evaluate_care"' in t),
            ("shared",
             f"https://localhost:{self.PORTS['shared']}/health",
             lambda t: '"service":"shared_services"' in t),
            ("website",
             "https://localhost/",
             lambda t: True),
        ]
        deadline = time.time() + timeout_s
        last_state = {}
        with httpx.Client(verify=False, timeout=5) as c:
            while time.time() < deadline:
                ready, missing = [], []
                for label, url, ok_pred in checks:
                    try:
                        r = (c.get(url) if label == "website" else c.post(url))
                        if r.status_code == 200 and ok_pred(r.text):
                            ready.append(label)
                            last_state[label] = "ready"
                        else:
                            missing.append(label)
                            last_state[label] = f"status={r.status_code} text={r.text[:80]!r}"
                    except Exception as e:
                        missing.append(label)
                        last_state[label] = f"err={type(e).__name__}"
                if not missing:
                    self._step_notice(f"all components ready: {ready}")
                    return
                time.sleep(3)
        sys.exit(
            f"ERROR: components did not all come up in {timeout_s}s. "
            f"State: {last_state}"
        )

    # REQ-B-003 — verify components
    def _verify_components(self) -> None:
        passed, failed = [], []
        v = self.results["verification"]

        def record(name: str, ok: bool, detail: str = "") -> None:
            v.append({"name": name, "ok": ok, "detail": detail[:300]})
            (passed if ok else failed).append(name)

        with httpx.Client(verify=False, timeout=10) as c:
            r = c.get("http://localhost/", follow_redirects=False)
            record("http_to_https_301", r.status_code == 301, f"got {r.status_code}")
            r = c.get("https://localhost/")
            record("website_200", r.status_code == 200, f"got {r.status_code}")
            record("website_has_banner", "envBanner" in r.text, "")
            for svc, port in (("findcare", self.PORTS["findcare"]),
                              ("evalcare", self.PORTS["evalcare"]),
                              ("shared",   self.PORTS["shared"])):
                r = c.post(f"https://localhost:{port}/health")
                record(f"{svc}_health", r.status_code == 200, r.text)
        ca = str(self.certs_dir / "ca.crt")
        fc_cert = (str(self.certs_dir / "findcare.crt"),
                   str(self.certs_dir / "findcare.key"))
        for tgt_svc, tgt_port, expected_substr in (
            ("evalcare", self.PORTS["evalcare"], "evaluate_care"),
            ("shared",   self.PORTS["shared"],   "shared_services"),
        ):
            with httpx.Client(cert=fc_cert, verify=ca, timeout=10) as cc:
                r = cc.post(f"https://localhost:{tgt_port}/health")
                ok = r.status_code == 200 and expected_substr in r.text
                record(f"mtls_findcare_to_{tgt_svc}", ok,
                       r.text if ok else f"{r.status_code}: {r.text}")
        self._step_notice(
            f"verification: {len(passed)} passed, {len(failed)} failed"
            + (f"; failed={failed}" if failed else "")
        )
        if failed:
            sys.exit(
                f"ERROR: verification failed for {failed}. Aborting deploy "
                "per V11 S-001-REQ-B-001 (atomic / no half-deployed state)."
            )

    # REQ-B-004 — invoke smoke test
    def _invoke_smoke_test(self) -> int:
        cmd = [
            sys.executable, "-m", "pytest", "-v",
            str(self.deploy_dir / "find_care_smoke_test.py"),
            f"--smoke-env={self.env}",
        ]
        result = subprocess.run(
            cmd, cwd=str(self.repo_root),
            capture_output=True, text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    def _display_smoke_failure_banner(self) -> None:
        banner = (
            "\n=========================================="
            "\n  SMOKE TEST FAILED"
            "\n  Environment left up for inspection."
            "\n=========================================="
        )
        print(banner, flush=True)
        sys.stderr.write(banner + "\n")

    def _human_verify_before_teardown(self) -> None:
        self._step_notice(
            "smoke failed — environment left up; operator intervention "
            "required."
        )
        try:
            ans = input("Smoke failed. Tear down anyway? (y/n): ").strip().lower()
        except (EOFError, OSError):
            return
        if ans != "y":
            sys.exit("Teardown aborted at human verify gate.")

    # Rule-063 per-env bump (local catches up to dev)
    def _bump_local_build_counter(self) -> None:
        cflags = _cflags()
        subprocess.run(
            ["git", "fetch", "origin", "dev"],
            cwd=str(self.repo_root), capture_output=True, creationflags=cflags,
        )
        diff = subprocess.run(
            ["git", "diff", "origin/dev", "--quiet"],
            cwd=str(self.repo_root), creationflags=cflags,
        )
        if diff.returncode != 0:
            self._step_notice(
                "build counter NOT bumped: working tree differs from "
                "origin/dev. Local keeps its prior build number."
            )
            self.results["build_after_bump"] = "skipped_tree_divergent"
            return
        ops_dir = self.repo_root / "Code" / "Shared" / "ops"
        added = False
        if str(ops_dir) not in sys.path:
            sys.path.insert(0, str(ops_dir))
            added = True
        try:
            from bump_build import bump as _bump
            record = _bump(env="local")
        finally:
            if added and str(ops_dir) in sys.path:
                sys.path.remove(str(ops_dir))
        builds = {b["env"]: b["build"] for b in record["builds"]}
        self.results["build_after_bump"] = builds
        self._step_notice(
            f"build counter bumped: local={builds.get('local')} "
            f"(dev={builds.get('dev')}, qa={builds.get('qa')}, "
            f"prod={builds.get('prod')})"
        )

    # REQ-T-006 — structured deploy output
    def _write_structured_output(self) -> None:
        self.results["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.results["backend_pids"] = [p.pid for p in self.backend_procs]
        self.output_path.write_text(
            json.dumps(self.results, indent=2), encoding="utf-8",
        )
        self._step_notice(f"structured output -> {self.output_path}")

    def _ensure_local_ca_trusted(self) -> None:
        if sys.platform != "win32":
            self._step_notice(
                f"skipping CA trust step (platform={sys.platform!r}, Windows-only)"
            )
            return
        ca_path = self.certs_dir / "ca.crt"
        if not ca_path.is_file():
            sys.exit(f"ERROR: ChatHealthy CA cert missing at {ca_path}")
        probe = subprocess.run(
            ["certutil", "-store", "Root"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if "ChatHealthy" in (probe.stdout or ""):
            self._step_notice("ChatHealthy Local CA already in Windows Root store")
            return
        self._step_notice(
            "installing ChatHealthy Local CA into Windows Root store "
            "(UAC prompt will appear; approve it)"
        )
        ps_cmd = (
            "Start-Process certutil "
            f"-ArgumentList '-addstore','-f','Root','{ca_path}' "
            "-Verb RunAs -Wait"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            sys.exit(
                f"ERROR: certutil install failed (rc={result.returncode}). "
                f"stderr={result.stderr or '(empty)'} "
                f"stdout={result.stdout or '(empty)'}"
            )
        verify = subprocess.run(
            ["certutil", "-store", "Root"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if "ChatHealthy" not in (verify.stdout or ""):
            sys.exit(
                "ERROR: ChatHealthy Local CA install completed but the "
                "Windows Root store still doesn't show it (UAC cancelled?)."
            )
        self._step_notice("ChatHealthy Local CA verified in Windows Root store")

    # ── Orchestration ─────────────────────────────────────────────────
    def run(self) -> int:
        self._step_notice(f"deploy started for {self.env}")
        self._ensure_local_ca_trusted()
        self._lint_workflow_yamls()
        self._deployment_architecture_gate()
        self._ensure_docker_available()
        self._teardown_precondition()
        self._step_notice("old environment torn down and ready")
        self._validate_prerequisites()
        self._stage_wrapper_website()
        # React build MUST run before backend container build.
        self._build_react_frontend()
        self._build_backend_containers()
        self._start_backend_processes()
        self._wait_for_all_components()
        self._verify_components()
        self._step_notice("new environment built and verified")
        self._bump_local_build_counter()
        self._step_notice("smoke test started")
        smoke_rc = self._invoke_smoke_test()
        self.results["smoke_rc"] = smoke_rc
        self.results["smoke_passed"] = (smoke_rc == 0)
        self._step_notice(f"smoke test ended: rc={smoke_rc}")
        if smoke_rc != 0:
            self._display_smoke_failure_banner()
            self._human_verify_before_teardown()
        self._write_structured_output()
        return smoke_rc


# ═════════════════════════════════════════════════════════════════════════
# main() dispatcher
# ═════════════════════════════════════════════════════════════════════════

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy per-target build packages: --env local stands up "
                    "the local host stack; --env dev|qa|prod ships cloud targets."
    )
    parser.add_argument("--env", required=True, choices=["local", "dev", "qa", "prod"])
    parser.add_argument(
        "--version", default=None,
        help="vN build to deploy (cloud envs). Default: highest v{N}/ on disk. Ignored for --env local.",
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | a specific target_id. Ignored for --env local.",
    )
    args = parser.parse_args(argv)
    _require_local_context()
    if args.env == "local":
        return LocalDeploy().run()
    return _run_cloud_deploy(args.env, args.version, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
