"""local_deploy.py - operator's deploy manager (all envs).

Single operator-facing deploy entry point per REQ-T-049: ships per-target
build packages from localBuild/<target_id>/ to their cloud destinations,
AND stands up the local host stack for --env local. Reads each
per-target package manifest for the target's facts (build_number
included); resolves secret VALUES from the bound store via
SecretsResolver (bindings constructed from deployment_architecture.json's
per-target `secrets` map per REQ-T-038 / REQ-T-052). The package itself
contains NO secret values (REQ-T-053).

usage:
    python local_deploy.py --env local
    python local_deploy.py --env dev --target cloudflare
    python local_deploy.py --env qa
    python local_deploy.py --env prod --target target_cloudflare_pages_website

One current build at a time lives under localBuild/; the checkout is
the version. No --version flag.

--env local: instantiates LocalDeploy() and runs the full host-stack
lifecycle per EPIC-008-F-012-S-001 REQ-T-001..T-008. --env dev|qa|prod:
ships per-target packages to their cloud destinations (cloudflare
wrangler, HF Space docker push, Azure FA config-zip).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
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

import aca_helpers
import ch_fonts_inliner
import chdm_helpers
import hf_helpers as rd
from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord


BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/localBuild")


def firm_git_identity() -> dict:
    """Read firm.git_identity{name, email} from
    deployment_architecture.json. Replaces the previously hardcoded
    `user.name=SkipSnow` / `user.email=skip.snow@gmail.com` in git
    invocations on the HF Space push path."""
    repo_root = Path(__file__).resolve().parents[2]
    manifest_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    git_id = (data.get("firm") or {}).get("git_identity")
    if not git_id or "name" not in git_id or "email" not in git_id:
        sys.exit(
            "ERROR: firm.git_identity{name, email} missing from "
            "deployment_architecture.json — populate it before deploy."
        )
    return git_id


GHCR_OWNER: str = "skipsnow"
GHCR_IMAGE_NAME: dict[str, str] = {
    "target_hf_space_findcare_backend":     "findcare-backend",
    "target_hf_space_evaluatecare_backend": "evaluatecare-backend",
    "target_hf_space_shared_services":      "sharedservices-backend",
}
HF_APP_PORT: dict[str, int] = {
    "target_hf_space_findcare_backend":     7860,
    "target_hf_space_evaluatecare_backend": 7860,
    "target_hf_space_shared_services":      7860,
}
# NOTE: signing-key renames, peer URLs, cert PEMs, and ENV_PREFIX stamping
# used to live in this module as hardcoded per-target maps. They have been
# moved entirely into the per-target `variables` block in
# deployment_architecture.json. The deploy script is now data-driven; no
# target-specific knowledge lives here.

PIPELINE_SOURCE_PREFIX = "pipeline/Code/"
AZURE_REQUIREMENTS_SRC = "pipeline/Code/requirements-pipeline.txt"
AZURE_REQUIREMENTS_ZIP_PATH = "requirements.txt"


def step(msg: str) -> None:
    print(f"[local_deploy] {msg}", flush=True)


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def require_local_context() -> None:
    """REQ-T-055 — local_deploy MUST run only on an operator's workstation,
    never on a GitHub Actions runner. For CI deploys use remote_deploy.py."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.exit(
            "ERROR: local_deploy.py MUST NOT run on a GitHub Actions runner. "
            "Use remote_deploy.py instead (REQ-T-055)."
        )


# Firm-level promote-chain rule. local + dev deploy from the dev branch;
# qa deploys from the qa branch; prod deploys from main. Every cloud
# target in deployment_architecture.json carries the same env_binding.branch
# values, so this map is the redundant firm-level statement of the rule —
# enforced regardless of any per-target env_binding wiring at the cloud
# dispatch path. local stand-up also enforces it here, before any other
# work begins.
ENV_BRANCH: dict[str, str] = {
    "local": "dev",
    "dev":   "dev",
    "qa":    "qa",
    "prod":  "main",
}


def require_branch_matches_env(env: str) -> None:
    """Hard-fail unless the local working-tree branch is the one the firm
    promote chain says deploys to `env`. Runs at main() entry so neither
    local nor cloud deploys can ever ship a wrong-branch source set."""
    expected = ENV_BRANCH.get(env)
    if not expected:
        sys.exit(f"ERROR: unknown env {env!r}; promote-chain guard refuses to proceed.")
    cp = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        sys.exit(
            "ERROR: deploy refuses to run from a detached HEAD or non-branch state.\n"
            f"  stderr: {(cp.stderr or '').strip()[:300]}"
        )
    actual = cp.stdout.strip()
    if actual != expected:
        sys.exit(
            f"ERROR: promote-chain guard — refusing to deploy.\n"
            f"  env                   : {env}\n"
            f"  expected branch       : {expected}\n"
            f"  current branch (HEAD) : {actual}\n"
            f"To deploy {env} you MUST be on branch '{expected}'. Use the "
            "promote workflow to land the change there first."
        )


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def load_target_manifest(repo_root: Path, target_id: str, build_root_rel: Path | None = None) -> dict:
    root = build_root_rel if build_root_rel is not None else BUILD_ROOT_REL
    path = repo_root / root / target_id / "manifest.json"
    if not path.is_file():
        sys.exit(f"ERROR: target manifest missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ═════════════════════════════════════════════════════════════════════════
# Cloudflare Pages handler (--env dev|qa|prod, target_kind=cloudflare_pages_project)
# ═════════════════════════════════════════════════════════════════════════

def deploy_cloudflare(
    build_dir: Path,
    env: str,
    resolver: SecretsResolver,
    target: TargetRecord,
) -> str:
    env_binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if env_binding is None or not env_binding.branch:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} has no `branch` "
            f"declared in deployment_architecture.json (REQ-T-050). The deploy "
            f"script reads the branch from the manifest; no hard-coded fallback."
        )
    cf_block = getattr(env_binding, "cloudflare_pages", None) or {}
    if isinstance(cf_block, dict):
        project = cf_block.get("project_name")
    else:
        project = getattr(cf_block, "project_name", None)
    if not project:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} has no "
            f"cloudflare_pages.project_name declared in "
            f"deployment_architecture.json. Populate it before deploy."
        )
    branch = env_binding.branch
    step(f"=== cloudflare_pages env={env} project={project} branch={branch} dir={build_dir} ===")
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
    step(f"  {' '.join(cmd)}")
    subprocess.run(
        cmd, env=env_for_wrangler, check=True,
        shell=(sys.platform == "win32"),
    )
    return project


# ═════════════════════════════════════════════════════════════════════════
# HF Space handler (--env dev|qa|prod, target_kind=hf_space)
# ═════════════════════════════════════════════════════════════════════════

def ghcr_image_ref(target_id: str, env: str, build_n: int) -> str:
    return f"ghcr.io/{_GHCR_OWNER}/{_GHCR_IMAGE_NAME[target_id]}:{env}-v{build_n}"


def docker_build_then_push(build_dir: Path, image_ref: str) -> None:
    step(f"  docker build -t {image_ref} {build_dir}")
    r = subprocess.run(
        ["docker", "build", "-t", image_ref, str(build_dir)],
        capture_output=True, text=True, creationflags=creation_flags(),
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker build failed for {image_ref}\n"
            f"{(r.stderr or r.stdout)[-2000:]}"
        )
    step(f"  docker push {image_ref}")
    r = subprocess.run(
        ["docker", "push", image_ref],
        capture_output=True, text=True, creationflags=creation_flags(),
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker push failed for {image_ref}\n"
            f"{(r.stderr or r.stdout)[-2000:]}\n"
            f"Hint: docker login ghcr.io -u <gh-user> -p <PAT-with-write:packages>"
        )


def set_hf_config(
    repo_root: Path,
    target_id: str,
    env: str,
    hf_token: str,
    resolver: SecretsResolver,
    target: TargetRecord,
) -> None:
    """Push the HF Space's variables and secrets, entirely data-driven from
    the target record. Zero target-specific knowledge lives in this function.

    The `variables` block holds entries pushed as HF Space VARIABLES (visible
    config). Each value is a source-qualifier string; see the docstring on
    TargetRecord.variables for the supported qualifier syntax.

    The `secrets` block holds entries pushed as HF Space SECRETS (hidden
    credentials). Same qualifier dispatch — but most entries are simply
    `local_env`-resolved.
    """
    space = rd._hf_space_name(target_id, env)

    def _resolve_qualifier(name: str, qualifier: str) -> str:
        if qualifier == "local_env":
            return resolver.resolve(name, env)
        if qualifier == "env_name":
            return env
        if qualifier.startswith("local_cert_file:"):
            rel = qualifier.split(":", 1)[1]
            return base64.b64encode((repo_root / rel).read_bytes()).decode("ascii")
        if qualifier.startswith("peer_url:"):
            peer_target_id = qualifier.split(":", 1)[1]
            return rd._hf_peer_url(peer_target_id, env)
        if qualifier.startswith("rename_from:"):
            other_name = qualifier.split(":", 1)[1]
            other_qual = (target.secrets or {}).get(other_name)\
                or (target.variables or {}).get(other_name)
            if other_qual is None:
                raise KeyError(
                    f"target {target_id!r}: variable/secret {name!r} declared "
                    f"as rename_from:{other_name} but {other_name!r} does not "
                    "exist in the target's secrets or variables blocks"
                )
            return _resolve_qualifier(other_name, other_qual)
        raise ValueError(
            f"target {target_id!r}: unknown source qualifier "
            f"{qualifier!r} on entry {name!r}"
        )

    for name, qualifier in (target.variables or {}).items():
        value = _resolve_qualifier(name, qualifier)
        rd._hf_set_variable(hf_token, space, name, value)

    for name, qualifier in (target.secrets or {}).items():
        value = _resolve_qualifier(name, qualifier)
        rd._hf_set_secret(hf_token, space, name, value)


def push_thin_dockerfile_to_hf_space(
    target_id: str, env: str, hf_token: str, image_ref: str, port: int,
) -> None:
    org = rd._hf_org(target_id, env)
    space = rd._hf_space_name(target_id, env)
    hf_clone = Path(tempfile.mkdtemp(prefix=f"hf_{space}_"))
    repo_url = f"https://{org}:{hf_token}@huggingface.co/spaces/{org}/{space}"
    step(f"  clone https://huggingface.co/spaces/{org}/{space}")
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
        ["git", "-c", f"user.email={_firm_git_identity()['email']}", "-c", f"user.name={_firm_git_identity()['name']}",
         "add", "."],
        cwd=str(hf_clone), check=True,
    )
    diff = subprocess.run(
        ["git", "diff", "--staged", "--quiet"], cwd=str(hf_clone),
    )
    if diff.returncode == 0:
        step("  no changes vs HF Space — skipping push")
    else:
        subprocess.run(
            ["git", "-c", f"user.email={_firm_git_identity()['email']}", "-c", f"user.name={_firm_git_identity()['name']}",
             "commit", "-m", f"local_deploy {env} -> {image_ref}"],
            cwd=str(hf_clone), check=True,
        )
        step(f"  push to {space}")
        subprocess.run(["git", "push"], cwd=str(hf_clone), check=True)
    shutil.rmtree(hf_clone, ignore_errors=True)


def deploy_hf_space(
    repo_root: Path,
    build_dir: Path,
    build_n: int,
    target_id: str,
    env: str,
    resolver: SecretsResolver,
    target: TargetRecord,
) -> str:
    port = HF_APP_PORT[target_id]
    image_ref = ghcr_image_ref(target_id, env, build_n)
    step(f"=== hf_space {target_id} env={env} -> {image_ref} ===")
    docker_build_then_push(build_dir, image_ref)
    hf_token = resolver.resolve("HF_TOKEN", env)
    set_hf_config(repo_root, target_id, env, hf_token, resolver, target)
    push_thin_dockerfile_to_hf_space(target_id, env, hf_token, image_ref, port)
    return image_ref


# ═════════════════════════════════════════════════════════════════════════
# Azure Function App handler (--env dev|qa|prod, target_kind=azure_function_app)
# ═════════════════════════════════════════════════════════════════════════

def az_query(args: list[str], err_label: str) -> str:
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: {err_label} failed (exit {r.returncode})\n"
            f"  args: {' '.join(args)}\n"
            f"  stderr: {(r.stderr or '').strip()[:1000]}"
        )
    return r.stdout.strip()


def block_if_active_orchestrations(rg: str, app: str, task_hub: str) -> None:
    """Reject the deploy if any orchestration is Running or Pending on
    this FA. The pipeline cannot be reloaded mid-run safely."""
    step("checking for active orchestrations …")
    master_key = az_query(
        ["az", "functionapp", "keys", "list",
         "--resource-group", rg, "--name", app,
         "--query", "masterKey", "-o", "tsv"],
        err_label="az functionapp keys list",
    )
    if not master_key:
        sys.exit("ERROR: empty masterKey from Azure — cannot verify pipeline state.")
    default_host = az_query(
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
    # Initial probe: long timeout so a Netherite cold start (~40s observed
    # on dev Flex Consumption + scale-to-zero) doesn't waste the first
    # iteration of the query loop below.
    try:
        urllib.request.urlopen(f"{base}&runtimeStatus=Running", timeout=90).read()
    except Exception:
        pass

    def _query(status: str) -> list:
        # Per-request timeout sized for a cold Durable management endpoint
        # (Netherite + Flex Consumption + scale-to-zero); the prior 20s
        # value timed out every attempt for the same reason.
        deadline = time.time() + 360
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{base}&runtimeStatus={status}", timeout=90,
                ) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    data = json.loads(body)
                    if isinstance(data, list):
                        return data
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, json.JSONDecodeError):
                pass
            step(f"  waiting for warm-up to query {status} …")
            time.sleep(10)
        sys.exit(
            f"ERROR: could not query {status} orchestrations within 6 min."
        )

    running = _query("Running")
    pending = _query("Pending")
    # Durable Entities (instance IDs prefixed with '@') are KV state actors,
    # not orchestrations — they don't dispatch user code during a deploy and
    # surviving across a deploy is the intended semantic (configure() is
    # idempotent; each pipeline run resets state). Counting them as "active"
    # was wedging every deploy that followed a run with entities.
    def _is_user_orch(inst: dict) -> bool:
        iid = inst.get("instanceId", "") or ""
        return not iid.startswith("@")
    running_user = [i for i in running if _is_user_orch(i)]
    pending_user = [i for i in pending if _is_user_orch(i)]
    total = len(running_user) + len(pending_user)
    if total > 0:
        sys.exit(
            f"DEPLOY BLOCKED: {len(running_user)} Running + {len(pending_user)} "
            f"Pending orchestration(s) active on {app}. Terminate them "
            "before deploying."
        )
    step(f"  no active orchestrations on {app} — safe to deploy")


def az_push_zip(rg: str, app: str, zip_path: Path) -> None:
    step(f"pushing zip to Azure: {app}")
    r = subprocess.run(
        ["az", "functionapp", "deployment", "source", "config-zip",
         "--resource-group", rg, "--name", app,
         "--src", str(zip_path)],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    # az config-zip emits Bad Request when the host is currently in an
    # error state — but the zip upload + WEBSITE_RUN_FROM_PACKAGE update
    # have already happened by then. Treat as a warning and force the
    # follow-up restart; that's what lets the host re-mount the new
    # package and clear any prior error.
    if r.returncode != 0:
        stderr_text = (r.stderr or "").strip()
        if "Bad Request" not in stderr_text:
            sys.exit(
                f"ERROR: az config-zip failed (exit {r.returncode})\n"
                f"  stderr: {stderr_text[:1500]}\n"
                f"  stdout: {(r.stdout or '').strip()[:500]}"
            )
        step(f"  config-zip Bad Request (package uploaded; will force restart)")
    else:
        step(f"  config-zip pushed to {app}")
    step(f"  restarting {app} so the host re-mounts the new package")
    r2 = subprocess.run(
        ["az", "functionapp", "restart",
         "--resource-group", rg, "--name", app],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r2.returncode != 0:
        sys.exit(
            f"ERROR: az functionapp restart failed (exit {r2.returncode})\n"
            f"  stderr: {(r2.stderr or '').strip()[:500]}"
        )
    step(f"  restart issued to {app}")


GATEWAY_STORAGE_ACCOUNT = "findcarestorage"
GATEWAY_PLAN_LOCATION = "eastus2"
GATEWAY_PYTHON_VERSION = "3.11"
GATEWAY_FUNCTIONS_VERSION = "4"


def functionapp_exists(rg: str, app: str) -> bool:
    r = subprocess.run(
        ["az", "functionapp", "show",
         "--name", app, "--resource-group", rg, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    return r.returncode == 0


def functionapp_create(rg: str, app: str) -> None:
    step(f"  creating Function App {app} (rg={rg}) on Consumption Linux Python {_GATEWAY_PYTHON_VERSION}")
    # Storage account lives in a different RG (FindCareAzureInfrastructure),
    # so pass its full resource ID — az functionapp create only accepts a
    # bare name when the storage account is in the same RG as the FA.
    sid = subprocess.run(
        ["az", "storage", "account", "show",
         "--name", GATEWAY_STORAGE_ACCOUNT, "--query", "id", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if sid.returncode != 0 or not sid.stdout.strip():
        sys.exit(
            f"ERROR: storage account {_GATEWAY_STORAGE_ACCOUNT} not found\n"
            f"  stderr: {(sid.stderr or '').strip()[:500]}"
        )
    storage_id = sid.stdout.strip()
    r = subprocess.run(
        ["az", "functionapp", "create",
         "--name", app,
         "--resource-group", rg,
         "--storage-account", storage_id,
         "--consumption-plan-location", GATEWAY_PLAN_LOCATION,
         "--runtime", "python",
         "--runtime-version", GATEWAY_PYTHON_VERSION,
         "--functions-version", GATEWAY_FUNCTIONS_VERSION,
         "--os-type", "Linux",
         "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az functionapp create failed for {app} (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def functionapp_set_appsettings(rg: str, app: str, settings: dict[str, str]) -> None:
    if not settings:
        return
    pairs = [f"{k}={v}" for k, v in settings.items()]
    step(f"  setting {len(pairs)} app settings on {app}")
    r = subprocess.run(
        ["az", "functionapp", "config", "appsettings", "set",
         "--name", app, "--resource-group", rg,
         "--settings", *pairs, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az functionapp config appsettings set failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )


GATEWAY_UPSTREAM_TARGET_ID = "target_azure_container_app_pipeline"


def resolve_durable_router_url(coll: "DeploymentCollection", env: str) -> str:
    """Look up the durable Container App's current FQDN from Azure and
    build the upstream Router URL the gateway forwards to. Fails hard if
    the target is missing, the container app does not exist, or it has
    no FQDN — no fallback, no guessing."""
    upstream = coll.by_target_id(GATEWAY_UPSTREAM_TARGET_ID)
    env_binding = next(
        (e for e in upstream.environments if e.env_binding == env), None,
    )
    if env_binding is None or env_binding.azure_container_app is None:
        sys.exit(
            f"ERROR: gateway needs upstream target "
            f"{_GATEWAY_UPSTREAM_TARGET_ID!r} env={env!r} azure_container_app "
            f"to resolve DURABLE_ROUTER_URL — not found in manifest."
        )
    aca = env_binding.azure_container_app
    rg = aca["resource_group"]
    container_app = aca["container_app"]
    r = subprocess.run(
        ["az", "containerapp", "show",
         "--name", container_app, "--resource-group", rg,
         "--query", "properties.configuration.ingress.fqdn", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 or not r.stdout.strip():
        sys.exit(
            f"ERROR: cannot read FQDN for upstream container app "
            f"{container_app!r} in {rg!r}: {(r.stderr or '').strip()[:500]}"
        )
    return f"https://{r.stdout.strip()}/api/Router"


def deploy_azure_function_app(
    build_dir: Path,
    target: TargetRecord,
    env: str,
    resolver: SecretsResolver | None = None,
    coll: "DeploymentCollection | None" = None,
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
    step(f"=== azure_function_app {target.target_id} env={env} -> {app} (rg={rg}) ===")
    zip_path = build_dir / "deploy.zip"
    if not zip_path.is_file():
        sys.exit(f"ERROR: deploy.zip missing at {zip_path}")

    # Provision the FA resource if it doesn't exist (idempotent).
    if not functionapp_exists(rg, app):
        functionapp_create(rg, app)
    else:
        step(f"  Function App {app} already exists — no-op create")

    # Resolve secrets and set as Function App app settings. The Gateway
    # reads MONGO_FRONTEND_connectionString (build_id lookup), API_TOKEN_MAP
    # (bearer auth), AzureWebJobsStorage, ENV_PREFIX. The script-filename
    # override (PythonScriptFileName) tells the v2 worker which file holds
    # the FunctionApp object; EnableWorkerIndexing turns the v2 model on.
    app_settings: dict[str, str] = {
        "AzureWebJobsFeatureFlags":   "EnableWorkerIndexing",
        "PythonScriptFileName":       "ChatHealthyDataPipelinesGatewayFunctionApp.py",
    }
    # Resolve the upstream durable container app's FQDN from Azure and push
    # it as DURABLE_ROUTER_URL so the gateway code does not have to carry a
    # hardcoded URL fallback. No coll = test-only invocation. When the
    # upstream ACA target has been retired from the manifest we skip the
    # call site entirely — _resolve_durable_router_url stays strict (its
    # body documents the biz rule for when a durable upstream IS declared)
    # and gateway routes that would have forwarded via this URL fail at
    # runtime, which is the intended consequence of retiring the upstream.
    if coll is not None and coll.by_target_id(GATEWAY_UPSTREAM_TARGET_ID) is not None:
        app_settings["DURABLE_ROUTER_URL"] = resolve_durable_router_url(coll, env)
    if resolver is not None:
        for name, store_id in (target.secrets or {}).items():
            if store_id == "azure_automation_webhook":
                # The upstream azure_automation_runbook target's deploy
                # step mints/reuses the webhook and UPSERTs the URL onto
                # this FA. This handler MUST NOT try to resolve it from
                # any local store, and MUST NOT include it in the app-
                # settings batch (a stale or empty value here would
                # clobber the value the runbook deploy already wrote).
                continue
            try:
                app_settings[name] = resolver.resolve(name, env)
            except Exception as exc:
                sys.exit(
                    f"ERROR: failed to resolve secret {name!r} for env={env!r}: {exc}"
                )
    # Rename the gateway-specific AI binding to the canonical Azure
    # Functions app setting name. The deployment manifest holds a
    # GATEWAY_-prefixed key so the gateway and the worker can bind
    # separate connection strings from .env without colliding; the FA
    # itself only reads APPLICATIONINSIGHTS_CONNECTION_STRING.
    if "GATEWAY_APPINSIGHTS_CONNECTION_STRING" in app_settings:
        app_settings["APPLICATIONINSIGHTS_CONNECTION_STRING"] = (
            app_settings.pop("GATEWAY_APPINSIGHTS_CONNECTION_STRING")
        )
    functionapp_set_appsettings(rg, app, app_settings)

    # The gateway FA is a pure HTTP facade — no Durable orchestrations live
    # on it, so there is no orchestration-quiescence gate to wait on. The
    # durable function app (the ACA worker) owns that gate.
    az_push_zip(rg, app, zip_path)
    return f"{app}"


# ═════════════════════════════════════════════════════════════════════════
# Azure Automation Runbook handler (target_kind=azure_automation_runbook)
# ═════════════════════════════════════════════════════════════════════════
#
# Two-step deploy:
#   1. For every secret bound to azure_automation_variable, resolve the value
#      via SecretsResolver and create-or-update the matching Automation
#      Variable on the Automation Account.
#   2. Replace the runbook content with the staged runbook.py bytes and
#      publish (since `replace-content` leaves the runbook in Draft until
#      published).
#
# Schedules are referenced informationally on the target's azure_automation
# block; we do not touch them — the operator created them once and they
# survive deploys.

AUTOMATION_API = "2023-11-01"


def az_subscription_id() -> str:
    r = subprocess.run(
        ["az", "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az account show failed (exit {r.returncode}); "
            f"are you signed in?\n  stderr: {(r.stderr or '').strip()[:500]}"
        )
    return r.stdout.strip()


def az_automation_variable_set(rg: str, aa: str, name: str, value: str) -> None:
    """Idempotent create-or-update of an Automation Variable via REST.

    The `az automation variable` command group doesn't exist; the REST API
    is the supported surface. PUT is create-or-update. The `value` field
    on the wire is a JSON-encoded string (so a plain string becomes
    `"\"actual_value\""`). isEncrypted=true ensures Azure encrypts at rest.
    Values never land on disk locally — they live only in `body` until
    `az rest` consumes them via --body @file (stdin-piped here).
    """
    sub = az_subscription_id()
    base = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}/providers/Microsoft.Automation"
        f"/automationAccounts/{aa}/variables/{name}"
        f"?api-version={_AUTOMATION_API}"
    )
    body = json.dumps({
        "name": name,
        "properties": {
            "value": json.dumps(value),
            "isEncrypted": True,
        },
    })
    r = subprocess.run(
        ["az", "rest", "--method", "put", "--url", base,
         "--headers", "Content-Type=application/json",
         "--body", body, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: PUT automation variable {name!r} failed "
            f"(exit {r.returncode})\n  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def az_automation_runbook_ensure_exists(rg: str, aa: str, runbook: str) -> None:
    """Idempotent runbook ensure. `az automation runbook show` returns
    non-zero if the runbook doesn't exist; we then `az automation runbook
    create` it as a Python 3 runbook so the subsequent replace-content
    call has something to write into. Location is inherited from the AA."""
    show = subprocess.run(
        ["az", "automation", "runbook", "show",
         "--resource-group", rg, "--automation-account-name", aa,
         "--name", runbook, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if show.returncode == 0:
        step(f"  runbook {runbook} exists on {aa} — no-op")
        return
    step(f"  runbook {runbook} missing on {aa} — creating (Python3)")
    loc_show = subprocess.run(
        ["az", "automation", "account", "show",
         "--name", aa, "--resource-group", rg,
         "--query", "location", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if loc_show.returncode != 0 or not loc_show.stdout.strip():
        sys.exit(
            f"ERROR: az automation account show for {aa!r} failed; cannot "
            f"determine location for the new runbook."
        )
    location = loc_show.stdout.strip()
    create = subprocess.run(
        ["az", "automation", "runbook", "create",
         "--resource-group", rg, "--automation-account-name", aa,
         "--name", runbook,
         "--type", "Python3",
         "--location", location,
         "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if create.returncode != 0:
        sys.exit(
            f"ERROR: az automation runbook create failed for {runbook!r} "
            f"(exit {create.returncode})\n  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    step(f"  runbook {runbook} created.")


def az_automation_runbook_replace_content(rg: str, aa: str, runbook: str, content_path: Path) -> None:
    step(f"az automation runbook replace-content --name {runbook}")
    args = [
        "az", "automation", "runbook", "replace-content",
        "--resource-group", rg, "--automation-account-name", aa,
        "--name", runbook,
        "--content", "@" + str(content_path),
        "-o", "none",
    ]
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az automation runbook replace-content failed for {runbook!r} "
            f"(exit {r.returncode})\n  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def az_automation_runbook_publish(rg: str, aa: str, runbook: str) -> None:
    step(f"az automation runbook publish --name {runbook}")
    args = [
        "az", "automation", "runbook", "publish",
        "--resource-group", rg, "--automation-account-name", aa,
        "--name", runbook,
        "-o", "none",
    ]
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az automation runbook publish failed for {runbook!r} "
            f"(exit {r.returncode})\n  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def az_subscription_id() -> str:
    args = ["az", "account", "show", "--query", "id", "-o", "tsv"]
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(f"ERROR: az account show failed: {(r.stderr or '').strip()[:500]}")
    return (r.stdout or "").strip()


# Substrings that, when found in an AA job's `properties.exception` text,
# indicate the runbook NEVER STARTED EXECUTING because the AA's Python3
# package-install step failed. This is what we are catching with the dry-
# fire: the AA's environment cannot host the runbook. Any other exception
# (including the runbook raising on its own when fed an empty payload)
# means the environment is fine and the runbook code ran.
AA_PACKAGE_INSTALL_FAILURE_MARKERS = (
    "could not install",
    "not a supported wheel",
    "Package '",                          # pip "ERROR: Package 'x' requires ..."
    "No matching distribution",
    "ERROR: pip",
)


ORCHESTRATOR_RUNBOOK_NAME = "ChatHealthyDataMigratorOrchestrator"


def health_check_parameters_b64() -> dict:
    """Sub-runbook PUT /jobs parameters for the health-check dry-fire.
    Legacy AA strips quotes from raw parameter values; base64 sidesteps
    that. The receiving runbook decodes via base64.b64decode + json.loads."""
    inner = json.dumps({"health_check": True}).encode("utf-8")
    return {"payload": base64.b64encode(inner).decode("ascii")}


def az_automation_runbook_dry_fire(rg: str, aa: str, runbook: str) -> dict:
    """Start the runbook as an AA job WITHOUT runOn (so it runs on the AA
    sandbox, never on the Hybrid Worker - dry-fire must not provision
    anything), with a `health_check: true` payload that each runbook's
    _main short-circuits on (log the health check, return cleanly).
    Polls until terminal status. Returns {status, exception, job_id}."""
    sub = az_subscription_id()
    aa_job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version=2023-11-01"
    )
    body = json.dumps({
        "properties": {
            "runbook": {"name": runbook},
            "parameters": health_check_parameters_b64(),
        }
    })
    step(f"  health-check dry-fire {runbook} (aa_job_id={aa_job_id})")
    r = subprocess.run(
        ["az", "rest", "--method", "put", "--url", url,
         "--headers", "Content-Type=application/json", "--body", body, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: dry-fire PUT /jobs failed for {runbook!r}: "
            f"{(r.stderr or '').strip()[:1000]}"
        )

    poll_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version=2023-11-01"
    )
    deadline = time.time() + 240
    last_status = "?"
    while time.time() < deadline:
        r = subprocess.run(
            ["az", "rest", "--method", "get", "--url", poll_url, "-o", "json"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
        if r.returncode != 0:
            time.sleep(5)
            continue
        try:
            body_doc = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            time.sleep(5)
            continue
        props = body_doc.get("properties", {}) or {}
        status = props.get("status") or "?"
        last_status = status
        if status in ("Completed", "Failed", "Stopped", "Suspended"):
            return {
                "status": status,
                "exception": props.get("exception") or "",
                "job_id": aa_job_id,
            }
        time.sleep(5)
    return {
        "status": f"Timeout(last={last_status})",
        "exception": "",
        "job_id": aa_job_id,
    }


# Sub-runbooks (provisioner/deprovisioner) are dry-fired via PUT /jobs
# with a base64-encoded health_check payload. The orchestrator is
# webhook-only (production gateway and dry-fire both POST to the
# operator-minted webhook URL); the orchestrator dry-fire path is
# separate (see _az_automation_orchestrator_verify_via_webhook). The
# migrator runs on the Hybrid Worker VM, not the AA sandbox, so
# AA-side dry-fire is meaningless for it - verification is via the
# real migration test the operator fires post-deploy.
HEALTH_CHECK_SUPPORTED_RUNBOOKS = (
    "ChatHealthyDataMigratorProvisioner",
    "ChatHealthyDataMigratorDeprovisioner",
)
ORCHESTRATOR_WEBHOOK_ENV_KEY = "MONGOCLUSTER_MIGRATOR_ORCHESTRATOR_WEBHOOK_URL"


def az_automation_poll_job_to_terminal(
    rg: str, aa: str, aa_job_id: str, timeout_sec: int = 240,
) -> dict:
    """Poll an AA job until terminal status. Returns {status, exception}."""
    sub = az_subscription_id()
    poll_url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version=2023-11-01"
    )
    deadline = time.time() + timeout_sec
    last_status = "?"
    while time.time() < deadline:
        r = subprocess.run(
            ["az", "rest", "--method", "get", "--url", poll_url, "-o", "json"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
        if r.returncode != 0:
            time.sleep(5)
            continue
        try:
            body_doc = json.loads(r.stdout or "{}")
        except json.JSONDecodeError:
            time.sleep(5)
            continue
        props = body_doc.get("properties", {}) or {}
        status = props.get("status") or "?"
        last_status = status
        if status in ("Completed", "Failed", "Stopped", "Suspended"):
            return {"status": status, "exception": props.get("exception") or ""}
        time.sleep(5)
    return {"status": f"Timeout(last={last_status})", "exception": ""}


def az_automation_orchestrator_verify_via_webhook(
    rg: str, aa: str, webhook_url: str, runbook: str,
) -> None:
    """Fire the orchestrator's health-check path via its operator-minted
    webhook URL (same delivery the production gateway uses). The webhook
    body is a JSON object with health_check=true; AA wraps it in
    WebhookData and delivers via sys.argv[1]; the orchestrator unwraps
    and short-circuits. Webhook response carries the AA job_id to poll."""
    body = json.dumps({"health_check": True}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status_code = resp.status
            resp_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        sys.exit(
            f"ERROR: orchestrator webhook returned HTTP {e.code}: "
            f"{e.read().decode('utf-8', errors='replace')[:500]}"
        )
    if status_code != 202:
        sys.exit(
            f"ERROR: orchestrator webhook returned {status_code} "
            f"(expected 202): {resp_text[:500]}"
        )
    try:
        webhook_resp = json.loads(resp_text)
    except json.JSONDecodeError:
        sys.exit(f"ERROR: orchestrator webhook response not JSON: {resp_text[:500]}")
    job_ids = webhook_resp.get("JobIds") or webhook_resp.get("jobIds") or []
    if not job_ids:
        sys.exit(
            f"ERROR: orchestrator webhook response has no JobIds: {resp_text[:500]}"
        )
    aa_job_id = str(job_ids[0])
    step(f"  health-check dry-fire {runbook} via webhook (aa_job_id={aa_job_id})")
    result = az_automation_poll_job_to_terminal(rg, aa, aa_job_id, timeout_sec=240)
    status = result["status"]
    if status == "Completed":
        step(
            f"  health-check verified: {runbook} via webhook status=Completed "
            f"(dry-fire job {aa_job_id})"
        )
        return
    if status.startswith("Timeout"):
        sys.exit(
            f"ERROR: deploy FAILED for {aa}/{runbook} - orchestrator "
            f"health-check via webhook did not reach terminal status in 240s "
            f"(last={status}, job={aa_job_id})."
        )
    sys.exit(
        f"ERROR: deploy FAILED for {aa}/{runbook} - orchestrator "
        f"health-check via webhook ended status={status} (job={aa_job_id}). "
        f"Exception (truncated):\n  {result['exception'][:1500]}"
    )


def az_automation_runbook_verify_runnable(rg: str, aa: str, runbook: str) -> None:
    """Post-deploy verification by health-check dry-fire.

    Shipping the source bytes is necessary but not sufficient: the AA's
    Python3 environment may be broken in ways the management-plane API
    does not surface (e.g. a package wheel built for the wrong Python
    version - provisioningState=Succeeded but the job-time install
    fails). The only way to know the runbook is actually runnable is to
    run it. Each chdm runbook's _main short-circuits on
    `payload.health_check is True` by writing one log line and exiting
    cleanly; the deploy fires that path and requires status=Completed.

    Any non-Completed status is a DEPLOY FAILURE - the runbook the deploy
    just published cannot actually execute on this AA, and shipping the
    next migration request would silently fail in the same way.

    Skipped for runbooks that do not have a health_check path (e.g. the
    ReservationReaper, which existed before this verification was
    introduced and whose health is observable via its own 5-min schedule)."""
    if runbook not in HEALTH_CHECK_SUPPORTED_RUNBOOKS:
        step(
            f"  skipping health-check dry-fire for {runbook} "
            f"(no health_check path; verify via scheduled execution)"
        )
        return

    result = az_automation_runbook_dry_fire(rg, aa, runbook)
    status = result["status"]
    exception_text = result["exception"]
    job_id = result["job_id"]

    if status == "Completed":
        step(
            f"  health-check verified: {runbook} status=Completed "
            f"(dry-fire job {job_id})"
        )
        return

    if status.startswith("Timeout"):
        sys.exit(
            f"ERROR: deploy FAILED for {aa}/{runbook} - health-check "
            f"dry-fire did not reach terminal status in 240s "
            f"(last={status}, job={job_id})."
        )

    sys.exit(
        f"ERROR: deploy FAILED for {aa}/{runbook} - health-check "
        f"dry-fire ended status={status} (job={job_id}). The runbook "
        f"the deploy just published cannot execute on this AA. "
        f"Exception (truncated):\n"
        f"  {exception_text[:1500]}"
    )


chdm_persistent_infra_ensured_for: set[str] = set()
CHDM_TARGET_PREFIX = "target_azure_automation_runbook_chdm_"
CHDM_PROVISIONER_TARGET = "target_azure_automation_runbook_chdm_provisioner"


def ensure_chdm_persistent_infrastructure_once(
    aa_rg: str, aa: str, vm_rg: str, env_key: str,
    *, repo_root: Path, resolver: SecretsResolver, env: str,
) -> str:
    """Run the CHDM persistent-infrastructure ensure block exactly once per
    `local_deploy` process for the given env. Returns the VM subnet ARM
    resource id (cached after the first call for downstream callers in the
    same session). Lands the admin SSH private key on the operator's
    workstation as part of the same once-per-session block."""
    cache_key = f"{env_key}|{aa_rg}|{aa}|{vm_rg}"
    if cache_key in chdm_persistent_infra_ensured_for:
        return chdm_helpers.chdm_ensure_vm_subnet(vm_rg)
    subnet_id = chdm_helpers.chdm_ensure_chdm_persistent_infrastructure(
        vm_rg=vm_rg, aa_rg=aa_rg, aa=aa,
    )
    private_key_b64 = resolver.resolve("AZ_VM_ADMIN_SSH_PRIVATE_KEY_B64", env)
    chdm_helpers.chdm_ensure_admin_private_key_file(repo_root, private_key_b64)
    chdm_persistent_infra_ensured_for.add(cache_key)
    return subnet_id


def az_automation_python3_packages_list(rg: str, aa: str) -> dict[str, dict]:
    """Return a name -> properties map of currently-installed Python3
    packages on the Automation Account. Empty when none installed."""
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/python3Packages?api-version=2018-06-30"
    )
    r = subprocess.run(
        ["az", "rest", "--method", "get", "--url", url, "-o", "json"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: AA python3Packages list failed for {aa!r}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )
    body = json.loads(r.stdout or "{}")
    out: dict[str, dict] = {}
    for item in body.get("value", []):
        name = item.get("name")
        if name:
            out[name] = item.get("properties", {}) or {}
    return out


def az_automation_python3_package_install(
    rg: str, aa: str, name: str, content_url: str,
) -> None:
    """PUT a Python3 package on the AA, polling until terminal state.
    Fails loud on terminal Failed; returns on Succeeded."""
    import time as _time
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/python3Packages/{name}?api-version=2018-06-30"
    )
    body = json.dumps({"properties": {"contentLink": {"uri": content_url}}})
    step(f"  PUT python3 package {name} <- {content_url}")
    r = subprocess.run(
        ["az", "rest", "--method", "put", "--url", url,
         "--headers", "Content-Type=application/json",
         "--body", body, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: AA python3 package PUT failed for {name!r}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )
    deadline = _time.time() + 600  # 10 min cap
    while _time.time() < deadline:
        get_r = subprocess.run(
            ["az", "rest", "--method", "get", "--url", url, "-o", "json"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
        if get_r.returncode != 0:
            sys.exit(
                f"ERROR: AA python3 package GET poll failed for {name!r}: "
                f"{(get_r.stderr or '').strip()[:1500]}"
            )
        st = json.loads(get_r.stdout or "{}")
        props = st.get("properties", {}) or {}
        provisioning_state = props.get("provisioningState")
        error = (props.get("error") or {}).get("message")
        step(f"    {name} provisioningState={provisioning_state}")
        if provisioning_state == "Succeeded":
            return
        if provisioning_state in ("Failed", "Cancelled"):
            sys.exit(
                f"ERROR: AA python3 package {name!r} entered terminal "
                f"state {provisioning_state!r}; error: {error or '<none>'}"
            )
        _time.sleep(10)
    sys.exit(
        f"ERROR: AA python3 package {name!r} did not reach a terminal "
        f"state within 10 minutes; last provisioningState was "
        f"{provisioning_state!r}."
    )


def ensure_runbook_python_packages(
    rg: str, aa: str, packages: list[dict],
) -> None:
    """Iterate declared packages; install any that are missing or whose
    contentLink.uri has drifted from the declared URL. No-op when each
    declared package is already at the named URL in Succeeded state."""
    if not packages:
        return
    existing = az_automation_python3_packages_list(rg, aa)
    for pkg in packages:
        name = pkg["name"]
        url = pkg["content_url"]
        cur = existing.get(name)
        if cur is not None:
            cur_url = (cur.get("contentLink") or {}).get("uri")
            cur_state = cur.get("provisioningState")
            if cur_url == url and cur_state == "Succeeded":
                step(f"  python3 package {name} already installed at declared URL")
                continue
            step(
                f"  python3 package {name} drift (state={cur_state}, "
                f"url match={cur_url == url}); re-installing"
            )
        az_automation_python3_package_install(rg, aa, name, url)


def az_automation_runbook_webhook_list(rg: str, aa: str, runbook: str) -> list[dict]:
    """List webhooks on the Automation Account, filtered to a single
    runbook. Note: the returned objects carry metadata only — Azure
    Automation never re-exposes a webhook's URL after creation."""
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/webhooks?api-version=2018-06-30"
    )
    r = subprocess.run(
        ["az", "rest", "--method", "get", "--url", url, "-o", "json"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: AA webhook list failed for runbook {runbook!r}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )
    body = json.loads(r.stdout or "{}")
    items = body.get("value") or []
    return [
        w for w in items
        if (w.get("properties", {}).get("runbook", {}) or {}).get("name") == runbook
    ]


def az_automation_runbook_webhook_delete(rg: str, aa: str, webhook_name: str) -> None:
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/webhooks/{webhook_name}?api-version=2018-06-30"
    )
    r = subprocess.run(
        ["az", "rest", "--method", "delete", "--url", url, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: AA webhook delete failed for {webhook_name!r}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )


def az_automation_runbook_webhook_create(
    rg: str, aa: str, runbook: str, webhook_name: str,
) -> str:
    """Mint a webhook bound to a runbook. Returns the full URL with
    embedded token. The URL is only available at creation time; Azure
    Automation never re-exposes it after this call returns. Expiry is
    set to ~10 years out, matching the CHDM webhook minted 2026-06-02."""
    from datetime import datetime, timedelta, timezone
    sub = az_subscription_id()
    expires = (
        datetime.now(timezone.utc) + timedelta(days=365 * 10)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/webhooks/{webhook_name}?api-version=2018-06-30"
    )
    body = json.dumps({
        "properties": {
            "isEnabled":  True,
            "expiryTime": expires,
            "runbook":    {"name": runbook},
        }
    })
    r = subprocess.run(
        ["az", "rest", "--method", "put", "--url", url,
         "--headers", "Content-Type=application/json",
         "--body", body, "-o", "json"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: AA webhook create failed for {webhook_name!r}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )
    body_out = json.loads(r.stdout or "{}")
    webhook_url = body_out.get("properties", {}).get("uri") or ""
    if not webhook_url:
        sys.exit(
            f"ERROR: AA webhook PUT for {webhook_name!r} did not return a URI; "
            f"the URL is unrecoverable from this point. Body: {r.stdout!r}"
        )
    return webhook_url


def functionapp_get_appsetting(rg: str, app: str, name: str) -> str:
    """Return the current value of a single FA app setting, or empty
    string if it isn't present."""
    r = subprocess.run(
        ["az", "functionapp", "config", "appsettings", "list",
         "--name", app, "--resource-group", rg, "-o", "json"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az functionapp config appsettings list failed for "
            f"{app!r}: {(r.stderr or '').strip()[:1500]}"
        )
    settings = json.loads(r.stdout or "[]")
    for s in settings:
        if s.get("name") == name:
            return s.get("value") or ""
    return ""


def ensure_runbook_webhook_and_push_to_consumer(
    rg: str, aa: str, runbook: str,
    webhook_block: dict,
    coll: "DeploymentCollection",
    env: str,
) -> None:
    """Ensure a webhook bound to the runbook exists and the consumer
    target carries its URL as the named app setting. Idempotent:
      - If the consumer already has the named app setting populated,
        the existing webhook URL is left in place (we cannot recover
        the URL from Azure to verify identity).
      - Otherwise: delete any stale webhook of the same name (we
        cannot reuse it because the URL was lost), mint a fresh one,
        and UPSERT the URL onto the consumer's app settings."""
    consumer_target_id = webhook_block["consumer_target_id"]
    app_setting_name = webhook_block["app_setting_name"]
    consumer = coll.by_target_id(consumer_target_id)
    if consumer is None:
        sys.exit(
            f"ERROR: runbook {runbook!r} webhook block names consumer "
            f"{consumer_target_id!r} which is not in the manifest."
        )
    consumer_env = next(
        (e for e in consumer.environments if e.env_binding == env), None,
    )
    if consumer_env is None:
        sys.exit(
            f"ERROR: consumer {consumer_target_id!r} has no env_binding "
            f"for env={env!r}; cannot push webhook URL."
        )
    if consumer_env.azure is None:
        sys.exit(
            f"ERROR: consumer {consumer_target_id!r} env={env!r} is not "
            f"an azure_function_app target — only FA consumers are "
            f"wired today for webhook-URL push."
        )
    consumer_rg = consumer_env.azure["resource_group"]
    consumer_app = consumer_env.azure["function_app"]
    webhook_name = f"{runbook}Webhook"

    existing_value = functionapp_get_appsetting(
        consumer_rg, consumer_app, app_setting_name,
    )
    if existing_value:
        step(
            f"  webhook URL already present on {consumer_app}/"
            f"{app_setting_name} — idempotent no-op"
        )
        return

    existing_webhooks = az_automation_runbook_webhook_list(rg, aa, runbook)
    for w in existing_webhooks:
        if w.get("name") == webhook_name:
            step(
                f"  deleting stale webhook {webhook_name} (URL was not "
                f"captured on the consumer; cannot reuse)"
            )
            az_automation_runbook_webhook_delete(rg, aa, webhook_name)
            break

    step(f"  minting webhook {webhook_name} for runbook {runbook}")
    url = az_automation_runbook_webhook_create(rg, aa, runbook, webhook_name)
    step(
        f"  UPSERT webhook URL onto {consumer_app}/{app_setting_name}"
    )
    functionapp_set_appsettings(
        consumer_rg, consumer_app, {app_setting_name: url},
    )


def deploy_azure_automation_runbook(
    build_dir: Path,
    target: TargetRecord,
    env: str,
    resolver: SecretsResolver,
    repo_root: Path,
    coll: "DeploymentCollection | None" = None,
) -> str:
    env_binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if env_binding is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} has no env_binding "
            f"matching {env!r}; cannot deploy."
        )
    aa_block = env_binding.azure_automation
    if aa_block is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} is missing the "
            f"`azure_automation` sub-object (resource_group / automation_account / "
            f"runbook_name) in deployment_architecture.json."
        )
    rg = aa_block["resource_group"]
    aa = aa_block["automation_account"]
    runbook = aa_block["runbook_name"]
    step(
        f"=== azure_automation_runbook {target.target_id} env={env} -> "
        f"{aa}/{runbook} (rg={rg}) ==="
    )

    content_path = build_dir / "runbook.py"
    if not content_path.is_file():
        sys.exit(f"ERROR: runbook.py missing at {content_path}")

    # For any ChatHealthyDataMigrator runbook, the deploy script owns the
    # persistent infrastructure that the chain depends on: undelegated VM
    # subnet, Hybrid Worker Group, AA managed-identity role assignments.
    # Idempotent; runs once per local_deploy process.
    chdm_subnet_id: str | None = None
    if target.target_id.startswith(CHDM_TARGET_PREFIX):
        vm_rg = resolver.resolve("AZ_VM_RESOURCE_GROUP", env)
        if not vm_rg:
            sys.exit(
                "ERROR: AZ_VM_RESOURCE_GROUP must be set in the operator "
                "secret store (Code/.env) — the CHDM persistent-infra ensure "
                "needs to know which resource group hosts the Hybrid Worker VM."
            )
        chdm_subnet_id = ensure_chdm_persistent_infrastructure_once(
            aa_rg=rg, aa=aa, vm_rg=vm_rg, env_key=env,
            repo_root=repo_root, resolver=resolver, env=env,
        )

    # Push every secret binding into the Automation Account as an Automation
    # Variable. Resolver fetches the value from the operator's bound store
    # (Code/.env for local). Values never land on disk; only the az process
    # sees them in argv.
    secret_names = sorted(target.secrets or {})
    step(f"  attempting to publish {len(secret_names)} Automation Variable(s)")
    pushed, skipped = 0, []
    for name in secret_names:
        try:
            value = resolver.resolve(name, env)
        except KeyError:
            skipped.append(name)
            continue
        if value is None or value == "":
            skipped.append(name)
            continue
        az_automation_variable_set(rg, aa, name, value)
        pushed += 1
    if skipped:
        step(
            f"  skipped {len(skipped)} variable(s) not in operator's "
            f"resolved store (likely have runbook-side defaults): "
            f"{', '.join(skipped)}"
        )
    step(f"  pushed {pushed} Automation Variable(s)")

    # Inject the deploy-computed AZ_VM_SUBNET_ID for the provisioner runbook.
    # The subnet ARM resource id only exists after the ensure step ran and
    # is therefore not in the operator's secret store.
    if target.target_id == CHDM_PROVISIONER_TARGET and chdm_subnet_id:
        az_automation_variable_set(rg, aa, "AZ_VM_SUBNET_ID", chdm_subnet_id)
        step("  pushed deploy-computed AZ_VM_SUBNET_ID Automation Variable")

    # Ensure declared Python3 packages are installed on the AA before
    # publishing the runbook content. The runbook's first execution can
    # then import them at module load without ModuleNotFoundError.
    declared_packages = aa_block.get("python_packages") or []
    if declared_packages:
        step(f"  ensuring {len(declared_packages)} python3 package(s) on {aa}")
        ensure_runbook_python_packages(rg, aa, declared_packages)

    # Ensure the runbook resource exists on the AA before pushing content.
    # First-time deploys need a Python3 runbook resource created; subsequent
    # deploys see it and no-op.
    az_automation_runbook_ensure_exists(rg, aa, runbook)
    # Replace runbook bytes + publish (so next scheduled tick uses the new code).
    az_automation_runbook_replace_content(rg, aa, runbook, content_path)
    az_automation_runbook_publish(rg, aa, runbook)
    step(f"  runbook {runbook} published")
    # Post-deploy verification. Shipping source bytes is necessary but not
    # sufficient: if the AA's Python3 package state is broken (wrong-
    # platform wheel, install failure), every Python3 job startup aborts
    # before the runbook script executes. A deploy that produces a non-
    # runnable runbook is a FAILED deploy. The orchestrator is verified
    # via its webhook URL (same path as the production gateway uses);
    # other chdm runbooks are verified via PUT /jobs with base64-encoded
    # health_check payload.
    if runbook == ORCHESTRATOR_RUNBOOK_NAME:
        try:
            webhook_url = resolver.resolve(ORCHESTRATOR_WEBHOOK_ENV_KEY, env)
        except KeyError:
            sys.exit(
                f"ERROR: deploy cannot verify {runbook} - "
                f"{_ORCHESTRATOR_WEBHOOK_ENV_KEY} is not in the operator's "
                f"secret store. Set it in Code/.env."
            )
        az_automation_orchestrator_verify_via_webhook(rg, aa, webhook_url, runbook)
    else:
        az_automation_runbook_verify_runnable(rg, aa, runbook)

    # Webhook lifecycle for runbooks dispatched via HTTP webhook from a
    # consumer target (e.g. the gateway FA forwarding ChatHealthyTask
    # values to AA). Manifest declares the consumer + the app-setting
    # name; this handler owns the mint/reuse/push flow.
    webhook_block = aa_block.get("webhook")
    if webhook_block is not None:
        if coll is None:
            sys.exit(
                f"ERROR: runbook {runbook!r} declares an azure_automation."
                f"webhook block but the deploy handler was invoked without "
                f"a manifest collection — cannot resolve consumer target."
            )
        ensure_runbook_webhook_and_push_to_consumer(
            rg=rg, aa=aa, runbook=runbook,
            webhook_block=webhook_block, coll=coll, env=env,
        )

    return f"{aa}/{runbook}"


# ═════════════════════════════════════════════════════════════════════════
# Azure Container App handler (--env dev|qa|prod, target_kind=azure_container_app)
# ═════════════════════════════════════════════════════════════════════════

def aca_image_repo(aca_coords: dict, env: str) -> str:
    """Derive the ACR image repository from the env's ACA coords.

    `<registry>.azurecr.io/<container_app>` is the convention: one repo
    per Container App, tagged per build_number.
    """
    registry = aca_coords.get("registry")
    if not registry:
        # Default convention: ACR named after the resource group, dashes
        # stripped, lowercased. Operator overrides via the
        # azure_container_app.registry field if needed.
        rg = str(aca_coords["resource_group"])
        registry = rg.lower().replace("-", "")
    container_app = aca_coords["container_app"]
    return f"{registry}.azurecr.io/{container_app}"


def deploy_azure_container_app(
    build_dir: Path,
    target: TargetRecord,
    env: str,
    resolver: SecretsResolver,
    build_n: int,
) -> str:
    env_binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if env_binding is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} has no env_binding "
            f"matching {env!r}; cannot deploy."
        )
    if env_binding.azure_container_app is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} is missing "
            f"the `azure_container_app` sub-object (resource_group / "
            f"container_app / container_app_environment / task_hub) in "
            f"deployment_architecture.json."
        )
    aca = env_binding.azure_container_app
    rg = aca["resource_group"]
    container_app = aca["container_app"]
    image_repo = aca_image_repo(aca, env)
    registry = image_repo.split(".azurecr.io/", 1)[0]
    image_ref = f"{image_repo}:{build_n}"
    step(
        f"=== azure_container_app {target.target_id} env={env} -> "
        f"{container_app} (rg={rg}) image={image_ref} ==="
    )

    # Verify the Dockerfile is present in the build context.
    if not (build_dir / "Dockerfile").is_file():
        sys.exit(
            f"ERROR: Dockerfile missing at {build_dir / 'Dockerfile'}; "
            f"run local_build.py --target aca first."
        )

    # Ensure the Netherite 'partitions' Event Hub matches host.json's
    # partitionCount BEFORE we push a new image. Event Hub partition
    # count is immutable in Azure; this check creates the hub if missing
    # and fails loud if it exists with the wrong count (operator must
    # delete + re-deploy). Per the "deploy owns permanent infrastructure"
    # split: the orchestrator never tries to provision Event Hubs.
    event_hubs_namespace = aca.get("event_hubs_namespace")
    if not event_hubs_namespace:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} azure_container_app "
            f"block is missing 'event_hubs_namespace' — required so deploy "
            f"can verify/provision the Netherite partitions Event Hub."
        )
    partition_count = aca_helpers.aca_read_partition_count_from_host_json(
        find_repo_root(Path(__file__)),
    )
    aca_helpers.aca_ensure_partitions_event_hub(
        event_hubs_namespace, rg, partition_count,
    )
    aca_helpers.aca_ensure_loadmonitor_event_hub(
        event_hubs_namespace, rg,
    )
    aca_helpers.aca_ensure_clients_event_hubs(
        event_hubs_namespace, rg,
    )

    # Ensure the Netherite Storage container for this TaskHub exists too.
    # Pairs with the Event Hub ensure: deploy owns the existence of both
    # pieces of permanent Azure infrastructure; their lazy auto-create by
    # the runtime is what made stale state silently leak across runs.
    task_hub = aca["task_hub"]
    aws_conn = resolver.resolve("AzureWebJobsStorage", env)
    storage_account, storage_account_key = (
        aca_helpers.aca_parse_storage_connection_string(aws_conn)
    )
    aca_helpers.aca_ensure_netherite_storage_container(
        storage_account, storage_account_key, task_hub,
    )

    # Ensure the supporting Azure resources exist before we provision the
    # Container App. Each pipeline owns its own workspace, env, and App
    # Insights component; no shared "pipeline" infrastructure.
    workspace_name = aca.get("log_analytics_workspace")
    app_insights_name = aca.get("application_insights")
    if not workspace_name or not app_insights_name:
        sys.exit(
            f"ERROR: target {target.target_id!r} env={env!r} azure_container_app "
            f"block missing 'log_analytics_workspace' and/or "
            f"'application_insights' — required so deploy can create the "
            f"per-pipeline log destinations."
        )
    workspace_id = aca_helpers.aca_ensure_log_analytics_workspace(
        workspace=workspace_name, resource_group=rg,
    )
    ai_conn = aca_helpers.aca_ensure_app_insights_component(
        component=app_insights_name, resource_group=rg, workspace_id=workspace_id,
    )
    aca_helpers.aca_ensure_container_apps_environment(
        environment=aca["container_app_environment"],
        resource_group=rg,
        workspace=workspace_name,
    )

    # Ensure the Container App itself exists before we try to push secrets
    # or update its template. Created with a placeholder image on first
    # deploy; aca_update_container_app below replaces with the real image.
    aca_helpers.aca_ensure_container_app_exists(
        container_app=container_app,
        resource_group=rg,
        environment=aca["container_app_environment"],
        registry=registry,
        min_replicas=int(aca["min_replicas"]),
        max_replicas=int(aca["max_replicas"]),
        cpu=float(aca["cpu"]),
        memory_gi=float(aca["memory_gi"]),
    )

    aca_helpers.aca_login_to_acr(registry)
    aca_helpers.aca_docker_build(build_dir, image_repo, build_n)
    aca_helpers.aca_docker_push(image_repo, build_n)

    # Resolve env-binding values from the .env / bound stores. The target
    # `secrets` map gives the names + store ids; SecretsResolver returns
    # the per-env value. Skip names that don't resolve (e.g., not present
    # in the local .env yet) — fail loud if a binding exists but the
    # store can't service it.
    env_var_values: dict[str, str] = {}
    for name in (target.secrets or {}).keys():
        env_var_values[name] = resolver.resolve(name, env)
    # Ensure the task_hub from the coord block is shipped as an env var
    # so the Functions worker's Netherite binding lands on the right hub.
    # Two names cover both consumers — TaskHubName for host.json's
    # %TaskHubName% substitution, DURABLE_TASK_HUB for the Python code
    # that builds the durable management URL for entity signalling.
    env_var_values["TaskHubName"]     = aca["task_hub"]
    env_var_values["DURABLE_TASK_HUB"] = aca["task_hub"]
    env_var_values["ENV_PREFIX"] = env
    # Inject the deploy-derived App Insights connection string. The AI
    # component is owned by this deploy (created above if absent), so the
    # connection string is the deploy's source of truth — never read from
    # .env. Overwrites any prior value to keep the binding authoritative.
    env_var_values["APPLICATIONINSIGHTS_CONNECTION_STRING"] = ai_conn

    aca_helpers.aca_set_secrets(container_app, rg, env_var_values)
    secret_names = list(env_var_values.keys())
    # Both reference (via secretref:) so values stay in the ACA secret
    # store and never leak into the revision template.
    aca_helpers.aca_update_container_app(
        container_app, rg, image_ref,
        env_vars=env_var_values, secret_names=secret_names,
        min_replicas=int(aca["min_replicas"]),
        max_replicas=int(aca["max_replicas"]),
        cpu=float(aca["cpu"]),
        memory_gi=float(aca["memory_gi"]),
    )
    aca_helpers.aca_wait_for_revision(container_app, rg)
    fqdn = aca_helpers.aca_query_fqdn(container_app, rg)
    step(f"  container app FQDN: https://{fqdn}")
    return f"{container_app} ({fqdn})"


# ═════════════════════════════════════════════════════════════════════════
# Cloud dispatch (--env dev|qa|prod)
# ═════════════════════════════════════════════════════════════════════════

def current_git_branch(repo_root: Path) -> str:
    """Return the local working tree's current branch name. Fails loud
    in detached-HEAD or any other state we cannot pin to a branch."""
    cp = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    if cp.returncode != 0 or not cp.stdout.strip():
        sys.exit(
            "ERROR: deploy refuses to run from a detached HEAD or non-branch state.\n"
            f"  stderr: {(cp.stderr or '').strip()[:300]}"
        )
    return cp.stdout.strip()


def require_branch_matches_env_binding(
    repo_root: Path,
    target,
    env: str,
) -> None:
    """Refuse the deploy unless the local branch matches the target's
    env_binding.branch for the requested env. Enforces the promote chain
    in code so a dev-branch checkout cannot push to qa or prod (and qa
    cannot push to prod). Per the firm-level promote rule (dev branch
    deploys dev, qa branch deploys qa, main branch deploys prod).

    Targets carrying promote_chain_bound=false in the manifest are
    single-environment shared infrastructure (one pipeline FA, one
    durable router ACA, one Automation Account + its runbooks) and are
    deployed from any branch. The guard skips them.
    """
    if not target.promote_chain_bound:
        return
    binding = next(
        (e for e in target.environments if e.env_binding == env), None,
    )
    if binding is None:
        sys.exit(
            f"ERROR: target {target.target_id!r} has no env_binding for env={env!r}; "
            f"cannot enforce promote-chain guard."
        )
    expected = binding.branch
    if not expected:
        sys.exit(
            f"ERROR: target {target.target_id!r} env_binding {env!r} has no "
            "branch declared in deployment_architecture.json; the deploy refuses "
            "to ship without a manifest-declared expected branch."
        )
    actual = current_git_branch(repo_root)
    if actual != expected:
        sys.exit(
            f"ERROR: promote-chain guard — refusing to deploy.\n"
            f"  target_id     : {target.target_id}\n"
            f"  env           : {env}\n"
            f"  expected branch (manifest): {expected}\n"
            f"  current branch (HEAD)     : {actual}\n"
            f"To deploy {env} you must be on '{expected}' (use the promote workflow "
            f"to land the change there first)."
        )


def deploy_one(
    repo_root: Path,
    target_id: str,
    target_kind: str,
    env: str,
    resolver: SecretsResolver,
    coll: DeploymentCollection,
) -> str:
    build_dir = repo_root / BUILD_ROOT_REL / target_id
    if not build_dir.is_dir():
        sys.exit(f"ERROR: build dir missing: {build_dir}")
    manifest = load_target_manifest(repo_root, target_id)
    build_n = int(manifest["build_number"])
    target = coll.by_target_id(target_id)
    require_branch_matches_env_binding(repo_root, target, env)
    if target_kind == "cloudflare_pages_project":
        return deploy_cloudflare(build_dir, env, resolver, target)
    if target_kind == "hf_space":
        return deploy_hf_space(repo_root, build_dir, build_n, target_id, env, resolver, target)
    if target_kind == "azure_function_app":
        target = coll.by_target_id(target_id)
        return deploy_azure_function_app(build_dir, target, env, resolver, coll)
    if target_kind == "azure_container_app":
        target = coll.by_target_id(target_id)
        return deploy_azure_container_app(build_dir, target, env, resolver, build_n)
    if target_kind == "azure_automation_runbook":
        target = coll.by_target_id(target_id)
        return deploy_azure_automation_runbook(
            build_dir, target, env, resolver, repo_root, coll,
        )
    raise RuntimeError(
        f"target_kind {target_kind!r} not supported in local_deploy."
    )


DEPLOYABLE_KINDS = (
    "cloudflare_pages_project",
    "hf_space",
    "azure_function_app",
    "azure_container_app",
    "azure_automation_runbook",
)


def select_target_ids(coll: DeploymentCollection, target_arg: str) -> list[tuple[str, str]]:
    """Return [(target_id, target_kind), ...] matching the filter."""
    if target_arg == "all":
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind in DEPLOYABLE_KINDS
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
    if target_arg == "azure":
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind in ("azure_function_app", "azure_container_app")
        ]
    if target_arg == "azure_function_app":
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "azure_function_app"
        ]
    if target_arg in ("aca", "azure_container_app"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "azure_container_app"
        ]
    if target_arg in ("automation", "azure_automation_runbook"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "azure_automation_runbook"
        ]
    for t in coll:
        if t.target_id == target_arg:
            return [(t.target_id, t.target_kind)]
    return []


def run_cloud_deploy(env: str, target_arg: str) -> int:
    """Deploy every in-scope target independently. A target either deploys
    completely (its handler succeeds) or is skipped entirely with an error
    captured for the final report — a failure in one target never halts
    another target's deploy. Each handler is responsible for its own
    fast-fail before any state-changing action so a per-target failure
    leaves the live system unchanged for that target.
    """
    repo_root = find_repo_root(Path(__file__))
    step(f"repo_root={repo_root} env={env} target={target_arg}")
    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    env_file = repo_root / "Code" / ".env"
    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    resolver = SecretsResolver.from_collection(coll, env_file=env_file)
    selected = select_target_ids(coll, target_arg)
    if not selected:
        sys.exit(f"ERROR: no targets matched --target={target_arg!r}")
    by_id = {t.target_id: t for t in coll}
    # Firm-level branch guard. Skipped only when EVERY selected target is
    # promote-chain exempt (the pipeline-only carve-out). The per-target
    # guard inside _deploy_one is still the authoritative enforcement;
    # this is the fail-fast surface for promote-chain deploys.
    if any(by_id[tid].promote_chain_bound for tid, _ in selected):
        require_branch_matches_env(env)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []  # (target_id, error_message)
    for target_id, target_kind in selected:
        target = by_id[target_id]
        if env not in target.env_binding_set():
            step(f"  skip {target_id}: no env_binding for {env!r}")
            continue
        try:
            succeeded.append(deploy_one(
                repo_root, target_id, target_kind, env, resolver, coll,
            ))
        except SystemExit as exc:
            msg = str(exc.code) if exc.code else "sys.exit() with no message"
            step(f"  FAILED {target_id}: {msg[:500]}")
            failed.append((target_id, msg))
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc!s}"
            step(f"  FAILED {target_id}: {msg[:500]}")
            failed.append((target_id, msg))
    if succeeded:
        step(f"deployed {len(succeeded)} target(s):")
        for d in succeeded:
            step(f"  {d}")
    if failed:
        step(f"FAILED {len(failed)} target(s):")
        for tid, msg in failed:
            step(f"  {tid}: {msg[:300]}")
        return 1
    if not succeeded:
        sys.exit(
            f"ERROR: nothing deployed for env={env!r} target={target_arg!r}. "
            f"Check env_binding in deployment_architecture.json."
        )
    return 0


# ═════════════════════════════════════════════════════════════════════════
# LocalDeploy class (--env local; subsumes legacy old_local_deploy.LocalDeploy)
# Per EPIC-008-F-012-S-001 REQ-T-001..T-008 + REQ-B-001..B-007.
# ═════════════════════════════════════════════════════════════════════════

REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"


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

    # container_name -> manifest target_id (consumed by runtime_data_collections
    # at startup to find its targets[] entry in ChatHealthyConfig.DBVersions).
    CONTAINER_TARGET_ID = {
        "ch-findcare":  "target_hf_space_findcare_backend",
        "ch-evalcare":  "target_hf_space_evaluatecare_backend",
        "ch-sharedsvc": "target_hf_space_shared_services",
    }

    # Website wrapper runs in its own container per S-002-REQ-T-002 / T-007.
    # Dockerfile lives at WebsiteWrapper/Dockerfile; build context = repo
    # root so the Dockerfile can COPY _start_website.py from its canonical
    # path in the deploy substrate.
    WEBSITE_CONTAINER_NAME = "ch-website"
    WEBSITE_DOCKERFILE = "architecture/DevOpsBuildDeployAndEnvironmentManagement/WebsiteWrapper/Dockerfile"

    def __init__(self) -> None:
        if REPO_ROOT_ENV not in os.environ:
            sys.exit(f"ERROR: {_REPO_ROOT_ENV} env var not set. Cannot resolve paths.")
        self.env = "local"
        self.repo_root = Path(os.environ[REPO_ROOT_ENV]).resolve()
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

    # REQ-T-005 — atomic teardown precondition
    def _teardown_precondition(self) -> None:
        cflags = creation_flags()
        # All containers under local_deploy's lifecycle (REQ-T-007): the 3
        # backends + the Website wrapper. Kafka container stays as separate
        # infra for now (its lifecycle migration is a follow-up).
        for container_name in list(self.BACKEND_CONTAINERS) + [self.WEBSITE_CONTAINER_NAME]:
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
            capture_output=True, text=True, timeout=15, creationflags=creation_flags(),
        )
        if result.returncode != 0:
            sys.exit(
                "ERROR: Docker daemon not available. "
                "FIX: open Docker Desktop and re-run this script. "
                f"`docker version` exit={result.returncode}, "
                f"stderr={result.stderr.strip()[:300]}"
            )

    def _write_build_info(self, build_ctx_abs: Path, container_name: str) -> Path:
        cflags = creation_flags()
        from dotenv import load_dotenv
        from pymongo import MongoClient
        load_dotenv(self.repo_root / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        if not conn:
            sys.exit("ERROR: MONGO_FRONTEND_connectionString not set; cannot read local build counter.")
        latest = MongoClient(conn, serverSelectionTimeoutMS=10000)["admin"]["Versions"].find_one(sort=[("from", -1)])
        build_num = (latest or {}).get("build")
        if build_num is None:
            sys.exit("ERROR: admin.Versions latest record has no 'build' field.")
        build_num = int(build_num)
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(self.repo_root), capture_output=True, text=True,
                creationflags=cflags, check=True,
            ).stdout.strip()
        except Exception:
            commit = "unknown"
        info = {
            "build": build_num,
            "commit": commit,
            "env": self.env,
            "target_id": self.CONTAINER_TARGET_ID.get(container_name),
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
        auth_src = self.repo_root / "sharedServices" / "Code" / "AuthorizationsAndAuthentications"
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
                    capture_output=True, text=True, creationflags=creation_flags(),
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

    def _build_website_container(self) -> None:
        """Build the Website wrapper container per S-002-REQ-T-002 / T-007 /
        T-008. One Dockerfile, build context = repo root so the Dockerfile
        can COPY _start_website.py from the deploy substrate."""
        dockerfile_abs = self.repo_root / self.WEBSITE_DOCKERFILE
        if not dockerfile_abs.is_file():
            sys.exit(
                f"ERROR: Website Dockerfile missing at {dockerfile_abs}. "
                "S-002-REQ-T-002 requires the Website wrapper to run in a "
                "Docker container; its Dockerfile is the source of that image."
            )
        self._step_notice(f"building image {self.WEBSITE_CONTAINER_NAME}")
        result = subprocess.run(
            ["docker", "build",
             "-t", self.WEBSITE_CONTAINER_NAME,
             "-f", str(dockerfile_abs), str(self.repo_root)],
            cwd=str(self.repo_root),
            capture_output=True, text=True, creationflags=creation_flags(),
        )
        if result.returncode != 0:
            sys.exit(
                f"ERROR: docker build failed for {self.WEBSITE_CONTAINER_NAME}: "
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
        # Local stack always binds to env={self.env} regardless of any
        # ENV_PREFIX value in Code/.env. HF Space deploys set this via
        # _hf_set_variable per env; the Azure FA gateway deploy sets it
        # to its own env; the local stack does the same here.
        env_dict["ENV_PREFIX"] = self.env
        # ChatHealthyLoggingService reads CH_LOG_LEVEL (logging_service.py:71),
        # not the legacy LOG_LEVEL. Bridge the legacy var so existing .env
        # settings keep working in the new library. If both are set in .env,
        # CH_LOG_LEVEL wins (only fill when caller hasn't set it explicitly).
        if env_dict.get("CH_LOG_LEVEL") is None and env_dict.get("LOG_LEVEL"):
            env_dict["CH_LOG_LEVEL"] = env_dict["LOG_LEVEL"]
        env_args: list[str] = []
        for k, v in env_dict.items():
            if v is None:
                continue
            env_args.extend(["-e", f"{k}={v}"])
        cflags = creation_flags()
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
        # Website wrapper runs as a Docker container per S-002-REQ-T-002 +
        # REQ-T-007. Mount certs read-only at /certs; mount the website
        # staging dir read-only at /website (the Dockerfile's ENTRYPOINT
        # reads these paths). Map host ports 80 + 443 directly.
        certs_host = str(self.certs_dir).replace("\\", "/")
        website_host = str(self.website_staging_dir).replace("\\", "/")
        subprocess.run(
            ["docker", "rm", "-f", self.WEBSITE_CONTAINER_NAME],
            capture_output=True, text=True, creationflags=cflags,
        )
        run_cmd = [
            "docker", "run", "-d",
            "--name", self.WEBSITE_CONTAINER_NAME,
            "--add-host", "host.docker.internal:host-gateway",
            "-p", "80:80",
            "-p", "443:443",
            "-v", f"{certs_host}:/certs:ro",
            "-v", f"{website_host}:/website:ro",
            self.WEBSITE_CONTAINER_NAME,
        ]
        run_result = subprocess.run(
            run_cmd, capture_output=True, text=True, creationflags=cflags,
        )
        if run_result.returncode != 0:
            sys.exit(
                f"ERROR: docker run {self.WEBSITE_CONTAINER_NAME} failed: "
                f"{run_result.stderr.strip()[:500]}"
            )
        self._step_notice(
            f"docker run {self.WEBSITE_CONTAINER_NAME} -> host ports 80+443 "
            "(per S-002-REQ-T-002)"
        )

    # REQ-B-003 — wait for components
    def _wait_for_all_components(self, timeout_s: int = 180) -> None:
        checks = [
            ("findcare",
             f"https://localhost:{self.PORTS['findcare']}/health",
             # Boot success contract: runtime is up and Mongo is connected.
             # Missing search indexes report status=degraded — the runtime
             # boots fine, but data-touching requests will surface their
             # own errors at call time. Treat both ok and degraded as ready.
             lambda t: '"status":"ok"' in t or '"status":"degraded"' in t),
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
        self._deployment_architecture_gate()
        self._ensure_docker_available()
        self._teardown_precondition()
        self._step_notice("old environment torn down and ready")
        self._validate_prerequisites()
        self._stage_wrapper_website()
        # React build MUST run before backend container build.
        self._build_react_frontend()
        self._build_backend_containers()
        self._build_website_container()
        self._start_backend_processes()
        self._wait_for_all_components()
        self._verify_components()
        self._step_notice("new environment built and verified")
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
# Helper-only module — no main() entry point. Per build_deploy_promote_plan
# v3 §INV-5 the only entry points are build_chathealthy.py + deploy_chathealthy.py
# + promote_chathealthy.py; this module is imported by them, not invoked
# directly.
#
# LocalDeploy is aliased as LocalStandUp for callers that prefer the
# operator-facing name per plan v3 §C.2 / §E.5 RESOLUTION.
LocalStandUp = LocalDeploy
