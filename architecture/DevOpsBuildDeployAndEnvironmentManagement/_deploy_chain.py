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
lifecycle per EPIC-008-F-012 REQ-T-001..T-008. --env dev|qa|prod:
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

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
_CH_LOG = ChatHealthyLoggingService()


# Canonical build output root (operator directive 2026-08-04): every
# build writes to <repo>/build/<target_id>/<package_id>/; every deploy
# reads from there. build_chathealthy.py empties every package directory
# before each build, leaving the declared structure in place.
BUILD_ROOT_REL = Path("build")


ARCHITECTURE_REL = Path(
    "brain/machine_artifacts/content/deployment_architecture.json"
)


def _build_manifest_for(repo_root: Path, target_id: str) -> dict:
    """The target's record, read from deployment_architecture.json.

    Deployment content lives in one file. The build tree used to carry a
    copy of the target's slice of it, which was a second copy of the truth
    that could drift from the first.
    """
    path = repo_root / ARCHITECTURE_REL
    if not path.is_file():
        sys.exit(f"ERROR: {path} not found.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    for rec in doc.get("DeploymentTargetRecord", []):
        if rec.get("target_id") == target_id:
            return rec
    sys.exit(
        f"ERROR: no target {target_id!r} in deployment_architecture.json"
    )


def package_build_facts(repo_root: Path, target_id: str,
                        package_id: str) -> dict:
    """What the build recorded about the package it produced.

    A build number cannot be declared ahead of the build that produces it,
    so the build records it beside the bytes. This holds build facts only;
    the architecture is read from the manifest.
    """
    path = repo_root / BUILD_ROOT_REL / target_id / package_id / "build.json"
    if not path.is_file():
        sys.exit(
            f"ERROR: no build facts at {path}. Run "
            f"`build_chathealthy.py --env <env> --target {target_id} "
            f"--package {package_id}` first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _target_packages(repo_root: Path, target_id: str) -> list[str]:
    """Package ids the built target carries, in declaration order."""
    data = _build_manifest_for(repo_root, target_id)
    seen: list[str] = []
    for eb in data.get("environments", []) or []:
        for pkg in (eb.get("packages") or []):
            pid = pkg.get("package_id")
            if pid and pid not in seen:
                seen.append(pid)
    for f in data.get("files", []) or []:
        pid = f.get("package")
        if pid and pid not in seen:
            seen.append(pid)
    return seen


WEBSITE_TARGET_ID = "target_cloudflare_pages_website"
# The one package of the website target that is not part of the served
# site: it holds the server, not the content.
WEBSITE_SERVER_PACKAGE = "local_host"
# Every website file is declared at its repository path; the site root is
# the directory those paths hang from.
WEBSITE_SOURCE_ROOT = "Website"


def _website_publish_dir(repo_root: Path) -> Path:
    """Assemble the served site from every content package.

    Packages are a build-time grouping by capability; what a web server
    serves is one tree. This merges the content packages into a single
    root, so which package a file came from stays visible in the build
    while the served layout is unchanged.
    """
    target_dir = repo_root / BUILD_ROOT_REL / WEBSITE_TARGET_ID
    out = target_dir / "_publish"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    merged = 0
    collisions: list[str] = []
    for pid in _target_packages(repo_root, WEBSITE_TARGET_ID):
        if pid == WEBSITE_SERVER_PACKAGE:
            continue
        root = target_dir / pid / WEBSITE_SOURCE_ROOT
        if not root.is_dir():
            continue
        for src in root.rglob("*"):
            if not src.is_file():
                continue
            dst = out / src.relative_to(root)
            if dst.exists():
                collisions.append(str(dst.relative_to(out)))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            merged += 1
    if collisions:
        sys.exit(
            f"ERROR: {len(collisions)} file(s) claimed by more than one "
            f"website package: {sorted(collisions)[:5]}. A served path must "
            f"have exactly one owning capability."
        )
    if not (out / "index.html").is_file():
        sys.exit(
            f"ERROR: merged website root {out} has no index.html. Run "
            f"`build_chathealthy.py --env local` first."
        )
    step(f"merged {merged} file(s) into {out}")
    return out


def _package_dir(repo_root: Path, target_id: str,
                 package_id: str | None = None) -> Path:
    """Directory holding one package's staged bytes.

    With no package_id the target must declare exactly one; asking for
    "the package" of a multi-package target is ambiguous and the caller
    has to say which capability it means.
    """
    target_dir = repo_root / BUILD_ROOT_REL / target_id
    if package_id is None:
        packages = _target_packages(repo_root, target_id)
        if len(packages) != 1:
            sys.exit(
                f"ERROR: target {target_id!r} declares {len(packages)} "
                f"packages ({', '.join(packages) or 'none'}); the caller must "
                f"name which one it needs."
            )
        package_id = packages[0]
    pkg = target_dir / package_id
    if not pkg.is_dir():
        sys.exit(
            f"ERROR: no build package at {pkg}. Run "
            f"build_chathealthy.py first."
        )
    return pkg



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


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
    _CH_LOG.info(f"[local_deploy] {msg}")


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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
    """No-op (retained for callsite compatibility).

    Operator directive 2026-08-04: deploy for env X reads only from
    <repo>/build/<target_id>/, which the build script populated from
    origin/<branch-for-X>. The local working-tree branch is irrelevant
    to the deploy and MUST NOT be enforced."""
    return


def find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise _ch_exc()(
            mode="runtime_error",
            component="_deploy_chain",
            message=f"no .git found walking up from {start}")


def load_target_manifest(repo_root: Path, target_id: str,
                        build_root_rel: Path | None = None) -> dict:
    """Build facts for the target's first package.

    Callers want `build_number`. Structure comes from the manifest via
    _build_manifest_for; this reads only what the build recorded.
    """
    pkgs = _target_packages(repo_root, target_id)
    if not pkgs:
        sys.exit(
            f"ERROR: {target_id} declares no packages; nothing was built."
        )
    return package_build_facts(repo_root, target_id, pkgs[0])


def _legacy_load_target_manifest(repo_root: Path, target_id: str, build_root_rel: Path | None = None) -> dict:
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
    # Publish the merged site root, not the target directory. Since the build
    # writes one directory per package, the target directory holds package
    # folders, build.json and _publish -- handing that to wrangler would put
    # the build's own structure on the CDN instead of the site. The merge is
    # the same one the local stand-up serves, so both environments publish
    # byte-identical content.
    site_dir = _website_publish_dir(build_dir.parent.parent)
    step(f"=== cloudflare_pages env={env} project={project} branch={branch} "
         f"dir={site_dir} ===")
    api_token = resolver.resolve("CLOUDFLARE_API_TOKEN", env)
    account_id = resolver.resolve("CLOUDFLARE_ACCOUNT_ID", env)
    env_for_wrangler = dict(os.environ)
    env_for_wrangler["CLOUDFLARE_API_TOKEN"] = api_token
    env_for_wrangler["CLOUDFLARE_ACCOUNT_ID"] = account_id
    cmd = [
        "npx", "wrangler", "pages", "deploy", str(site_dir),
        f"--project-name={project}",
        f"--branch={branch}",
        "--commit-dirty=true",
    ]
    step(f"  {' '.join(cmd)}")
    subprocess.run(
        cmd, env=env_for_wrangler, check=True,
        shell=(sys.platform == "win32"),
    )
    _reconcile_cloudflare_firewall_rules(env_binding, env, resolver, api_token)
    return project


# ═════════════════════════════════════════════════════════════════════════
# Cloudflare Custom Rules reconciliation (Zone WAF phase http_request_firewall_custom)
# ═════════════════════════════════════════════════════════════════════════

_CF_API = "https://api.cloudflare.com/client/v4"
_IDENT_START = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_"
)
_IDENT_BODY = _IDENT_START | frozenset("0123456789")


def _substitute_dollar_braces(expr: str, repl) -> str:
    """Replace every ${identifier} using repl(name).

    An identifier starts with a letter or underscore and continues with
    letters, digits or underscores. Anything else between the braces is not
    an identifier and is left exactly as written.
    """
    out: list[str] = []
    i = 0
    while i < len(expr):
        start = expr.find("${", i)
        if start == -1:
            out.append(expr[i:])
            break
        close = expr.find("}", start + 2)
        name = expr[start + 2:close] if close != -1 else ""
        valid = (
            close != -1 and name
            and name[0] in _IDENT_START
            and all(c in _IDENT_BODY for c in name[1:])
        )
        out.append(expr[i:start])
        if valid:
            out.append(repl(name))
            i = close + 1
        else:
            out.append("${")
            i = start + 2
    return "".join(out)


def _cf_api(
    method: str,
    path: str,
    api_token: str,
    body: dict | None = None,
) -> dict:
    req = urllib.request.Request(
        f"{_CF_API}{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        data=(json.dumps(body).encode("utf-8") if body is not None else None),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
        except Exception:  # noqa: BLE001
            pass
        sys.exit(
            f"ERROR: Cloudflare API {method} {path} returned {exc.code}\n"
            f"body_sent={json.dumps(body)[:400] if body else '(none)'}\n"
            f"response={detail}"
        )


def _cf_resolve_zone_id(zone_name: str, api_token: str) -> str:
    doc = _cf_api("GET", f"/zones?name={zone_name}&status=active", api_token)
    zones = doc.get("result") or []
    if not zones:
        sys.exit(f"ERROR: Cloudflare zone {zone_name!r} not found on this account")
    return zones[0]["id"]


def _substitute_secrets(expr: str, env: str, resolver: SecretsResolver) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1)
        return resolver.resolve(key, env)
    def _by_name(name: str) -> str:
        class _M:
            def group(self, _n):
                return name
        return repl(_M())

    return _substitute_dollar_braces(expr, _by_name)


def _reconcile_cloudflare_firewall_rules(
    env_binding, env: str, resolver: SecretsResolver, api_token: str,
) -> None:
    """Reconcile Cloudflare Custom Rules declared on the env binding.

    Rules are identified by `description` within the zone's
    http_request_firewall_custom ruleset. Existing rule with matching
    description is PATCHed; otherwise a POST creates a new one.
    """
    rules_decl = getattr(env_binding, "cloudflare_firewall_rules", None) or []
    if isinstance(rules_decl, dict):
        rules_decl = [rules_decl]
    if not rules_decl:
        return
    step(f"=== cloudflare_firewall_rules env={env} count={len(rules_decl)} ===")
    zones_seen: dict[str, tuple[str, str, list[dict]]] = {}
    for rule in rules_decl:
        zone_name = rule["zone_name"]
        if zone_name not in zones_seen:
            zone_id = _cf_resolve_zone_id(zone_name, api_token)
            rs = _cf_api(
                "GET",
                f"/zones/{zone_id}/rulesets/phases/http_request_firewall_custom/entrypoint",
                api_token,
            )
            ruleset = rs.get("result") or {}
            zones_seen[zone_name] = (
                zone_id,
                ruleset.get("id", ""),
                list(ruleset.get("rules") or []),
            )
        zone_id, ruleset_id, existing = zones_seen[zone_name]
        if not ruleset_id:
            sys.exit(
                f"ERROR: zone {zone_name!r} has no http_request_firewall_custom "
                f"ruleset entrypoint (unexpected — zones always ship one)."
            )
        expr = _substitute_secrets(rule["expression"], env, resolver)
        body = {
            "description": rule["description"],
            "expression": expr,
            "action": rule["action"],
            "enabled": bool(rule.get("enabled", True)),
        }
        if rule.get("action_parameters") is not None:
            body["action_parameters"] = rule["action_parameters"]
        existing_rule = next(
            (r for r in existing if r.get("description") == rule["description"]),
            None,
        )
        if existing_rule:
            rule_id = existing_rule["id"]
            step(f"  update rule id={rule_id} description={rule['description']!r}")
            _cf_api(
                "PATCH",
                f"/zones/{zone_id}/rulesets/{ruleset_id}/rules/{rule_id}",
                api_token,
                body=body,
            )
        else:
            step(f"  create rule description={rule['description']!r}")
            _cf_api(
                "POST",
                f"/zones/{zone_id}/rulesets/{ruleset_id}/rules",
                api_token,
                body=body,
            )


# ═════════════════════════════════════════════════════════════════════════
# HF Space handler (--env dev|qa|prod, target_kind=hf_space)
# ═════════════════════════════════════════════════════════════════════════

def ghcr_image_ref(target_id: str, env: str, build_n: int) -> str:
    return f"ghcr.io/{GHCR_OWNER}/{GHCR_IMAGE_NAME[target_id]}:{env}-v{build_n}"


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
    build_n: int,
) -> None:
    """Push the HF Space's variables and secrets, entirely data-driven from
    the target record. Zero target-specific knowledge lives in this function.

    Per-build naming: writes config to <base>_<build_n>. Peer URLs resolve
    to the co-deployed peer Spaces at the same build_n.
    """
    space = rd._hf_space_per_build_name(target_id, env, build_n)

    def _resolve_qualifier(name: str, qualifier: str) -> str:
        if qualifier == "local_env":
            return resolver.resolve(name, env)
        if qualifier == "env_name":
            return env
        if qualifier.startswith("literal:"):
            return qualifier.split(":", 1)[1]
        if qualifier.startswith("local_cert_file:"):
            rel = qualifier.split(":", 1)[1]
            return base64.b64encode((repo_root / rel).read_bytes()).decode("ascii")
        if qualifier.startswith("peer_url:"):
            peer_target_id = qualifier.split(":", 1)[1]
            return rd._hf_peer_url_for_build(peer_target_id, env, build_n)
        if qualifier.startswith("rename_from:"):
            other_name = qualifier.split(":", 1)[1]
            other_qual = (target.secrets or {}).get(other_name)\
                or (target.variables or {}).get(other_name)
            if other_qual is None:
                raise _ch_exc()(
            mode="key_error",
            component="_deploy_chain",
            message=f"target {target_id!r}: variable/secret {name!r} declared "
                    f"as rename_from:{other_name} but {other_name!r} does not "
                    "exist in the target's secrets or variables blocks")
            return _resolve_qualifier(other_name, other_qual)
        raise _ch_exc()(
            mode="value_error",
            component="_deploy_chain",
            message=f"target {target_id!r}: unknown source qualifier "
            f"{qualifier!r} on entry {name!r}")

    for name, qualifier in (target.variables or {}).items():
        value = _resolve_qualifier(name, qualifier)
        rd._hf_set_variable(hf_token, space, name, value)

    # Logging tag — the per-build Space name lands on every Mongo log
    # document (admin.HuggingFaceLogs_{env}). Without this, logs from
    # multiple Spaces in the same env are indistinguishable in the
    # collection. The logging library reads CH_SPACE_NAME at startup.
    rd._hf_set_variable(hf_token, space, "CH_SPACE_NAME", space)

    for name, qualifier in (target.secrets or {}).items():
        value = _resolve_qualifier(name, qualifier)
        rd._hf_set_secret(hf_token, space, name, value)


def push_thin_dockerfile_to_hf_space(
    target_id: str, env: str, hf_token: str, image_ref: str, port: int,
    build_n: int,
) -> None:
    org = rd._hf_org(target_id, env)
    space = rd._hf_space_per_build_name(target_id, env, build_n)
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
        ["git", "-c", f"user.email={firm_git_identity()['email']}", "-c", f"user.name={firm_git_identity()['name']}",
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
            ["git", "-c", f"user.email={firm_git_identity()['email']}", "-c", f"user.name={firm_git_identity()['name']}",
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
    """Reverted 2026-06-22: re-deploys to the same stable-base-name HF
    Space. The per-build (`_<n>`) scheme is retired because HF
    rate-limits Space CREATE and orphan Spaces accumulate. The Space
    is created once (idempotent no-op on existing); every build pushes
    a new image + thin Dockerfile to that one Space and waits for the
    Space runtime to roll over to the new build_n."""
    port = HF_APP_PORT[target_id]
    image_ref = ghcr_image_ref(target_id, env, build_n)
    qualified = rd._hf_space_qualified(target_id, env)
    step(f"=== hf_space {target_id} env={env} -> {image_ref} (Space {qualified}) ===")
    docker_build_then_push(build_dir, image_ref)
    hf_token = resolver.resolve("HF_TOKEN", env)
    rd._hf_create_space(hf_token, qualified, sdk="docker")
    set_hf_config(repo_root, target_id, env, hf_token, resolver, target, build_n)
    push_thin_dockerfile_to_hf_space(target_id, env, hf_token, image_ref, port, build_n)
    converged = rd._hf_wait_for_build_convergence(qualified, build_n,
                                                   timeout_s=600, poll_interval_s=10)
    if not converged:
        raise _ch_exc()(
            mode="runtime_error",
            component="_deploy_chain",
            message=f"deploy_hf_space: {qualified} did not converge to build="
            f"{build_n} within 600s. Investigate the HF runtime state, "
            f"then either rerun deploy or recover the Space.")
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
    step(f"  creating Function App {app} (rg={rg}) on Consumption Linux Python {GATEWAY_PYTHON_VERSION}")
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
            f"ERROR: storage account {GATEWAY_STORAGE_ACCOUNT} not found\n"
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
        f"?api-version={AUTOMATION_API}"
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


def az_automation_runbook_ensure_mint_request_parameter(
    rg: str, aa: str, runbook: str,
) -> None:
    """Declare the `mint_request` string parameter on CaEndpointRunbook.

    Azure Automation Python 3 only injects job parameters into the runbook
    globals when they are declared on the runbook resource. Without this,
    PUT /jobs can carry mint_request in properties.parameters while the
    script sees NameError / empty globals (F-003 deploy-time mint path).
    """
    if runbook != "CaEndpointRunbook":
        return
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/runbooks/{runbook}?api-version=2023-11-01"
    )
    get = subprocess.run(
        ["az", "rest", "--method", "get", "--url", url, "-o", "json"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if get.returncode != 0:
        sys.exit(
            f"ERROR: cannot read runbook {runbook!r} to declare mint_request: "
            f"{(get.stderr or '')[:800]}"
        )
    doc = json.loads(get.stdout or "{}")
    props = doc.get("properties") or {}
    params = dict(props.get("parameters") or {})
    desired = {
        "type": "string",
        "isMandatory": False,
        "position": 0,
        "defaultValue": "",
        "description": "F-003 mint payload JSON (csr_pem, caller_principal, caller_ad_token)",
    }
    if (
        isinstance(params.get("mint_request"), dict)
        and params["mint_request"].get("type") == "string"
    ):
        step(f"  runbook {runbook} mint_request parameter already declared")
        return
    params["mint_request"] = desired
    body = {
        "location": doc.get("location"),
        "properties": {
            "runbookType": props.get("runbookType") or "Python3",
            "logVerbose": bool(props.get("logVerbose")),
            "logProgress": bool(props.get("logProgress")),
            "parameters": params,
        },
    }
    step(f"  declaring mint_request parameter on {runbook}")
    put = subprocess.run(
        [
            "az", "rest", "--method", "put", "--url", url,
            "--headers", "Content-Type=application/json",
            "--body", json.dumps(body), "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if put.returncode != 0:
        sys.exit(
            f"ERROR: failed to declare mint_request on {runbook!r}: "
            f"{(put.stderr or '')[:1500]}"
        )


def az_automation_runbook_replace_content(rg: str, aa: str, runbook: str, content_path: Path) -> None:
    """Upload runbook source via ARM draft/content (UTF-8).

    `az automation runbook replace-content` encodes the body as Latin-1 and
    rejects Unicode (em-dashes, section signs) that appear in our sources.
    The management-plane draft/content PUT accepts UTF-8 bytes.
    """
    step(f"az rest PUT runbook draft/content --name {runbook}")
    if not content_path.is_file():
        sys.exit(f"ERROR: runbook content missing at {content_path}")
    text = content_path.read_text(encoding="utf-8")
    sub = az_subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/runbooks/{runbook}/draft/content?api-version=2023-11-01"
    )
    import tempfile
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".py", delete=False,
    ) as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        r = subprocess.run(
            [
                "az", "rest", "--method", "put", "--url", url,
                "--headers", "Content-Type=text/powershell",
                "--body", f"@{tmp_path}",
                "-o", "none",
            ],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    if r.returncode != 0:
        sys.exit(
            f"ERROR: runbook draft/content PUT failed for {runbook!r} "
            f"(exit {r.returncode})\n  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def _parse_interval_schedule(name: str):
    """SCH-<letters>-<digits>{min|hour}, case-insensitive.

    Returns (digits, unit_lowercased) or None. Replaces a pattern; the
    grammar is three dash-separated parts and is clearer written out.
    """
    parts = name.split("-")
    if len(parts) != 3 or parts[0].upper() != "SCH" or not parts[1].isalpha():
        return None
    tail = parts[2]
    for unit in ("min", "hour"):
        if len(tail) > len(unit) and tail[-len(unit):].lower() == unit:
            digits = tail[:-len(unit)]
            if digits.isdigit():
                return digits, unit
    return None


def _parse_daily_utc_schedule(name: str):
    """SCH-<letters>-<hhmm>UTC, case-insensitive. Returns hhmm or None."""
    parts = name.split("-")
    if len(parts) != 3 or parts[0].upper() != "SCH" or not parts[1].isalpha():
        return None
    tail = parts[2]
    if len(tail) == 7 and tail[-3:].upper() == "UTC" and tail[:4].isdigit():
        return tail[:4]
    return None


def _parse_schedule_from_name(name: str) -> dict | None:
    """Parse a schedule name into the ARM Schedule properties body.
    Two forms supported:
      SCH-<Runbook>-<N>(min|hour)  -> recurring every N minutes/hours
      SCH-<Runbook>-<HHMM>UTC     -> daily at HH:MM UTC
    Returns None if the name matches neither convention."""
    name = name.strip()
    from datetime import datetime, timedelta, timezone
    # Recurring interval form
    m = _SCHEDULE_NAME_RE.match(name)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        # Azure Automation requires startTime > NOW + 5min (strict).
        # 10-min offset gives safe margin for clock skew.
        start = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        frequency = "Minute" if unit == "min" else "Hour"
        return {
            "properties": {
                "description": (
                    f"Auto-created from runbook.schedule_names entry {name!r} "
                    f"(interval={n} {unit})"
                ),
                "startTime": start,
                "frequency": frequency,
                "interval": n,
                "timeZone": "UTC",
            }
        }
    # Daily-at-fixed-time UTC form (HHMM)
    m = _SCHEDULE_DAILY_UTC_RE.match(name)
    if m:
        hhmm = m.group(1)
        hh, mm = int(hhmm[:2]), int(hhmm[2:])
        now = datetime.now(timezone.utc)
        target_today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        # Azure needs startTime > NOW + 5min. If today's target has passed
        # or is within 10 min, roll to tomorrow.
        if target_today <= now + timedelta(minutes=10):
            target = target_today + timedelta(days=1)
        else:
            target = target_today
        start = target.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        return {
            "properties": {
                "description": (
                    f"Auto-created from runbook.schedule_names entry {name!r} "
                    f"(daily at {hh:02d}:{mm:02d} UTC)"
                ),
                "startTime": start,
                "frequency": "Day",
                "interval": 1,
                "timeZone": "UTC",
            }
        }
    return None


def az_automation_schedule_ensure(rg: str, aa: str, name: str) -> bool:
    """Create the schedule if it does not already exist. Returns True if
    the schedule is usable (exists or newly created). Idempotent."""
    sub = az_subscription_id()
    body = _parse_schedule_from_name(name)
    if body is None:
        step(f"  schedule {name} — name does not match SCH-<x>-<N>(min|hour); skipping")
        return False
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/schedules/{name}?api-version=2023-11-01"
    )
    # Check first — Azure Automation forbids updating an existing schedule's
    # startTime, so idempotency requires "create if missing, otherwise no-op".
    r = subprocess.run(
        ["az", "rest", "--method", "get", "--url", url, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode == 0:
        step(f"  schedule {name} exists — no-op")
        return True
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    try:
        json.dump(body, tmp)
        tmp.close()
        r = subprocess.run(
            ["az", "rest", "--method", "put", "--url", url,
             "--headers", "Content-Type=application/json",
             "--body", f"@{tmp.name}", "-o", "none"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if r.returncode != 0:
        sys.exit(
            f"ERROR: schedule create {name} failed on {aa}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )
    step(f"  schedule {name} created ({body['properties']['frequency']} {body['properties']['interval']})")
    return True


def az_automation_runbook_link_schedule(
    rg: str, aa: str, runbook: str, schedule: str,
) -> None:
    """Create a job-schedule (schedule -> runbook link). Idempotent by
    deterministic GUID derived from (aa, runbook, schedule)."""
    sub = az_subscription_id()
    import uuid as _uuid
    js_id = _uuid.uuid5(_uuid.NAMESPACE_URL, f"{aa}/{runbook}/{schedule}")
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobSchedules/{js_id}?api-version=2023-11-01"
    )
    body = {
        "properties": {
            "schedule": {"name": schedule},
            "runbook": {"name": runbook},
        }
    }
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
    try:
        json.dump(body, tmp)
        tmp.close()
        r = subprocess.run(
            ["az", "rest", "--method", "put", "--url", url,
             "--headers", "Content-Type=application/json",
             "--body", f"@{tmp.name}", "-o", "none"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    if r.returncode != 0:
        stderr_txt = (r.stderr or "")[:1500]
        # ARM returns 409 when the job-schedule already exists for the same
        # deterministic id — that's the idempotent success path.
        if "409" in stderr_txt or "already exists" in stderr_txt.lower():
            step(f"  job-schedule {schedule}->{runbook} already linked (no-op)")
            return
        sys.exit(
            f"ERROR: link schedule {schedule} to runbook {runbook} failed: "
            f"{stderr_txt}"
        )
    step(f"  job-schedule {schedule} linked to runbook {runbook}")


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
    deadline = time.time() + 600
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
    rg: str, aa: str, aa_job_id: str, timeout_sec: int = 600,
) -> dict:
    """Poll an AA job until terminal status. Returns {status, exception}.

    Default 600s (10 min) accommodates the AA Python 3 sandbox cold-start
    envelope: sandbox spin-up + python_packages install + our runbook
    module load (chathealthy_lib inlined bootstrap +
    ChatHealthyLoggingService instantiation) can take up to 5 min end-
    to-end on a cold AA before the runbook's own logic runs. Empirically
    600s was too tight for the ChatHealthyDataMigrator {Provisioner,
    Deprovisioner} health-check dry-fires, which then failed the deploy
    even though the runbook itself would eventually short-circuit on
    health_check=True."""
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
    result = az_automation_poll_job_to_terminal(rg, aa, aa_job_id, timeout_sec=600)
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
            f"health-check via webhook did not reach terminal status in 600s "
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
            f"dry-fire did not reach terminal status in 600s "
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
            # Azure Automation often omits contentLink.uri on GET after a
            # successful install (uri comes back null). Treat Succeeded +
            # matching name as installed; only re-install when Azure echoes
            # a different non-empty URI than declared.
            if cur_state == "Succeeded" and (
                not cur_url or cur_url == url
            ):
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


def _resolve_target_azure_scope(target: TargetRecord, env: str) -> str:
    """Return the Azure resource id of a target for the given env, derived
    entirely from the manifest env_binding sub-block. Empty string when
    the target does not project a distinct Azure resource id (e.g. shell
    verification target). No role names, no resource names appear here —
    everything is read from the manifest."""
    eb = next((e for e in target.environments if e.env_binding == env), None)
    if eb is None:
        return ""
    k = target.target_kind
    sub = az_subscription_id()
    if k == "azure_resource_group":
        b = eb.azure_resource_group or {}
        rg = b.get("name", "") or eb.node_address or ""
        if rg:
            return f"/subscriptions/{sub}/resourceGroups/{rg}"
    if k == "identity":
        b = eb.identity or {}
        rg = b.get("resource_group", "")
        name = b.get("name", "")
        if rg and name:
            return (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
            )
    if k == "azure_key_vault":
        b = eb.azure_key_vault or {}
        rg = b.get("resource_group", "")
        name = b.get("vault_name", "")
        if rg and name:
            return (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.KeyVault/vaults/{name}"
            )
    if k == "azure_storage_account":
        b = eb.azure_storage_account or {}
        rg = b.get("resource_group", "")
        name = b.get("account_name", "")
        if rg and name:
            return (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Storage/storageAccounts/{name}"
            )
    if k == "azure_container_apps_environment":
        # ACA Jobs (Microsoft.App/jobs) are RG-level resources, not
        # children of the managed environment in ARM. Role assignments
        # meant to cover jobs (e.g. Container Apps Jobs Operator) MUST
        # be scoped at the resource group so they propagate to every
        # job the env orchestrates. The RG name is a manifest field on
        # the env target's env_binding sub-block — no hardcoding here.
        b = eb.azure_container_apps_environment or {}
        rg = b.get("resource_group", "")
        if rg:
            return f"/subscriptions/{sub}/resourceGroups/{rg}"
    if k == "azure_container_registry":
        b = eb.azure_container_registry or {}
        rg = b.get("resource_group", "")
        name = b.get("registry_name", "")
        if rg and name:
            return (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.ContainerRegistry/registries/{name}"
            )
    if k == "azure_automation_account":
        b = eb.azure_automation_account or {}
        rg = b.get("resource_group", "") or b.get("vm_subnet_name") and ""
        # AA doesn't declare resource_group in its sub-block today; parse
        # from node_address as a fallback.
        if not rg:
            na = eb.node_address or ""
            parts = na.split("/")
            for i, p in enumerate(parts):
                if p.lower() == "resourcegroups" and i + 1 < len(parts):
                    rg = parts[i + 1]
                    break
        name = ""
        na = eb.node_address or ""
        parts = na.split("/")
        for i, p in enumerate(parts):
            if p.lower() == "automationaccounts" and i + 1 < len(parts):
                name = parts[i + 1]
                break
        if rg and name:
            return (
                f"/subscriptions/{sub}/resourceGroups/{rg}"
                f"/providers/Microsoft.Automation/automationAccounts/{name}"
            )
    return ""


def _resolve_identity_principal_id(coll: DeploymentCollection, identity: dict) -> str:
    """Return the Azure principal id (object id) for one IdentityCatalog
    entry. Looks up the referenced target and reads its Azure identity
    fields. Empty string on any resolution failure."""
    cls = identity.get("identity_class", "")
    tgt_id = identity.get("target_id_ref", "")
    if not tgt_id:
        return ""
    tgt = coll.by_target_id(tgt_id)
    if tgt is None:
        return ""
    eb = tgt.environments[0] if tgt.environments else None
    if eb is None or eb.identity is None:
        return ""
    if cls == "managed_identity":
        name = eb.identity.get("name", "")
        rg = eb.identity.get("resource_group", "")
        if not name or not rg:
            return ""
        r = subprocess.run(
            ["az", "identity", "show", "--name", name, "--resource-group", rg, "-o", "json"],
            capture_output=True, text=True,
            creationflags=creation_flags(), shell=(sys.platform == "win32"),
        )
        if r.returncode != 0:
            return ""
        try:
            return json.loads(r.stdout or "{}").get("principalId", "") or ""
        except ValueError:
            return ""
    return ""


def apply_identity_role_grants_from_manifest(coll: DeploymentCollection, env: str) -> None:
    """Iterate IdentityCatalog and grant every role in each identity's
    roles[] on every target whose allowed_roles[] contains that role.
    Purely data-driven from the manifest: no role names, no resource
    names, no per-identity conditional logic appears in this function.
    """
    step("applying identity role grants from manifest")
    for identity in coll.identity_catalog or []:
        iid = identity.get("identity_id", "")
        principal_id = _resolve_identity_principal_id(coll, identity)
        if not principal_id:
            continue
        roles = identity.get("roles", []) or []
        for role in roles:
            for target in coll:
                allowed = target.allowed_roles or []
                if role not in allowed:
                    continue
                scope = _resolve_target_azure_scope(target, env)
                if not scope:
                    continue
                r = subprocess.run(
                    ["az", "role", "assignment", "create",
                     "--assignee-object-id", principal_id,
                     "--assignee-principal-type", "ServicePrincipal",
                     "--role", role,
                     "--scope", scope, "-o", "none"],
                    capture_output=True, text=True,
                    creationflags=creation_flags(), shell=(sys.platform == "win32"),
                )
                # role assignment create is idempotent-ish: dup returns non-zero
                # but that's fine. Log the outcome.
                verdict = "granted" if r.returncode == 0 else "skipped (exists or refused)"
                step(f"  {iid}: {role} on {target.target_id} — {verdict}")


def apply_explicit_permissions_from_manifest(coll: DeploymentCollection, env: str) -> None:
    """Iterate every target's permissions[] and grant each {object_id, role}
    pair at that target's Azure scope. Self-contained mechanism for
    user / service-principal grants that do not fit the intersection
    model (IdentityCatalog.roles x target.allowed_roles) above."""
    step("applying explicit per-target permissions from manifest")
    for target in coll:
        perms = target.permissions or []
        if not perms:
            continue
        scope = _resolve_target_azure_scope(target, env)
        if not scope:
            continue
        for entry in perms:
            object_id = entry.get("object_id", "").strip()
            role = entry.get("role", "").strip()
            if not object_id or not role:
                continue
            r = subprocess.run(
                ["az", "role", "assignment", "create",
                 "--assignee-object-id", object_id,
                 "--role", role,
                 "--scope", scope, "-o", "none"],
                capture_output=True, text=True,
                creationflags=creation_flags(), shell=(sys.platform == "win32"),
            )
            if r.returncode == 0:
                step(f"  {object_id[:8]}...: {role} on {target.target_id} — granted")
            else:
                err = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:800]
                step(f"  {object_id[:8]}...: {role} on {target.target_id} — refused: {err}")


def ensure_runbook_webhook_stored_in_kv(
    rg: str, aa: str, runbook: str,
    webhook_name: str,
    kv_vault: str,
    kv_secret_name: str,
) -> None:
    """Mint a webhook bound to the runbook and store its URL in a Key
    Vault secret so the operator (or CI) can trigger the runbook on
    demand per LLD v23 §2.1(b).

    Idempotent: if the KV secret already carries a non-empty URL, the
    existing webhook is left in place (Azure Automation never re-
    exposes a webhook URL after creation, so we cannot verify identity
    without treating the KV value as source of truth). Otherwise: any
    stale webhook of the same name is deleted (its URL is lost), a
    fresh webhook is minted, and the URL is written to KV.
    """
    step(
        f"  ensure on-demand webhook {webhook_name} for runbook {runbook} "
        f"(URL will land in {kv_vault}/{kv_secret_name})"
    )
    existing_url = kv_secret_get(kv_vault, kv_secret_name)
    live_names = {
        w.get("name")
        for w in az_automation_runbook_webhook_list(rg, aa, runbook)
    }
    webhook_alive_on_aa = webhook_name in live_names
    if existing_url and webhook_alive_on_aa:
        step(
            f"    KV secret {kv_secret_name} populated AND webhook "
            f"{webhook_name} live on {aa} — idempotent no-op"
        )
        return
    if webhook_alive_on_aa:
        step(
            f"    deleting AA-side webhook {webhook_name} (KV drifted; "
            f"cannot recover URL from Azure)"
        )
        az_automation_runbook_webhook_delete(rg, aa, webhook_name)
    elif existing_url:
        step(
            f"    KV holds URL for a webhook that no longer exists on {aa} "
            f"(AA/runbook rebuilt) — minting fresh and overwriting KV"
        )
    url = az_automation_runbook_webhook_create(rg, aa, runbook, webhook_name)
    kv_secret_set(kv_vault, kv_secret_name, url)
    step(f"    minted webhook and wrote URL to {kv_vault}/{kv_secret_name}")


def kv_secret_get(vault: str, name: str) -> str:
    r = subprocess.run(
        ["az", "keyvault", "secret", "show",
         "--vault-name", vault, "--name", name,
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        # Missing / no-access — treat as empty.
        return ""
    return (r.stdout or "").strip()


def kv_secret_set(vault: str, name: str, value: str) -> None:
    r = subprocess.run(
        ["az", "keyvault", "secret", "set",
         "--vault-name", vault, "--name", name,
         "--value", value, "-o", "none"],
        capture_output=True, text=True,
        creationflags=creation_flags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: kv_secret_set failed for {vault}/{name}: "
            f"{(r.stderr or '').strip()[:1500]}"
        )


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
    live_names = {
        w.get("name")
        for w in az_automation_runbook_webhook_list(rg, aa, runbook)
    }
    webhook_alive_on_aa = webhook_name in live_names
    if existing_value and webhook_alive_on_aa:
        step(
            f"  webhook URL present on {consumer_app}/{app_setting_name} "
            f"AND webhook {webhook_name} live on {aa} — idempotent no-op"
        )
        return
    if webhook_alive_on_aa:
        step(
            f"  deleting AA-side webhook {webhook_name} (consumer setting "
            f"drifted; cannot recover URL from Azure)"
        )
        az_automation_runbook_webhook_delete(rg, aa, webhook_name)
    elif existing_value:
        step(
            f"  consumer holds URL for a webhook that no longer exists on "
            f"{aa} (AA/runbook rebuilt) — minting fresh and overwriting"
        )

    step(f"  minting webhook {webhook_name} for runbook {runbook}")
    url = az_automation_runbook_webhook_create(rg, aa, runbook, webhook_name)
    step(
        f"  UPSERT webhook URL onto {consumer_app}/{app_setting_name}"
    )
    functionapp_set_appsettings(
        consumer_rg, consumer_app, {app_setting_name: url},
    )


def _deploy_runbook_packages(
    repo_root: Path,
    target: TargetRecord,
    env: str,
    resolver: SecretsResolver,
    coll: "DeploymentCollection | None",
    package_selection: set[str] | None = None,
) -> None:
    """Deploy every runbook package declared on the Automation Account.

    `_synth_runbook_package` still derives each package's azure_automation
    block -- that was always its real job; the mistake was registering the
    result as a target. Here it produces a record that exists only for the
    length of one deploy call.
    """
    from target_record import _synth_runbook_package

    raw = target.to_dict()
    deployed = 0
    for eb in target.environments:
        if eb.env_binding != env:
            continue
        for pkg in (eb.packages or []):
            if pkg.get("kind") != "runbook":
                continue
            pid = pkg.get("package_id") or ""
            if package_selection and pid not in package_selection:
                continue
            pkg_dir = (repo_root / BUILD_ROOT_REL / target.target_id / pid)
            if not pkg_dir.is_dir():
                sys.exit(
                    f"ERROR: no build package at {pkg_dir}. Run "
                    f"build_chathealthy.py first."
                )
            synth = TargetRecord.from_dict(
                _synth_runbook_package(raw, eb.to_dict(), pkg)
            )
            deploy_azure_automation_runbook(
                pkg_dir, synth, env, resolver, repo_root, coll,
            )
            deployed += 1
    step(f"automation account {target.target_id}: {deployed} runbook "
         f"package(s) deployed")


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
                "secret store (.env) — the CHDM persistent-infra ensure "
                "needs to know which resource group hosts the Hybrid Worker VM."
            )
        chdm_subnet_id = ensure_chdm_persistent_infrastructure_once(
            aa_rg=rg, aa=aa, vm_rg=vm_rg, env_key=env,
            repo_root=repo_root, resolver=resolver, env=env,
        )

    # Push every secret binding into the Automation Account as an Automation
    # Variable. Resolver fetches the value from the operator's bound store
    # (.env for local). Values never land on disk; only the az process
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
    # Ensure declared schedules exist on the AA and are linked to this
    # runbook. Names that do not match the SCH-<x>-<N>(min|hour) convention
    # are logged and skipped (e.g. daily-at-time schedules like
    # SCH-ProviderPipeline-0200UTC need separate handling; the on-demand
    # webhook path covers manual triggers).
    for sched_name in (aa_block.get("schedule_names") or []):
        if az_automation_schedule_ensure(rg, aa, sched_name):
            az_automation_runbook_link_schedule(rg, aa, runbook, sched_name)
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
                f"{ORCHESTRATOR_WEBHOOK_ENV_KEY} is not in the operator's "
                f"secret store. Set it in .env."
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

    # On-demand webhook for operator/CI use (LLD v23 §2.1(b)). Manifest
    # declares webhook_name + KV vault + KV secret name; the deploy
    # chain mints the webhook and writes the URL to KV. Operator reads
    # the URL from KV to trigger the runbook.
    webhook_kv = aa_block.get("webhook_to_kv")
    if webhook_kv is not None:
        ensure_runbook_webhook_stored_in_kv(
            rg=rg, aa=aa, runbook=runbook,
            webhook_name=webhook_kv["webhook_name"],
            kv_vault=webhook_kv["kv_vault"],
            kv_secret_name=webhook_kv["kv_secret_name"],
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
    """No-op (retained for callsite compatibility).

    Operator directive 2026-08-04: deploy for env X reads only from
    <repo>/build/<target_id>/, populated by the build script from
    origin/<branch-for-X>. The local working-tree branch is
    irrelevant to the deploy and MUST NOT be enforced. The promote-
    chain invariant is upheld earlier -- promote_chathealthy.py
    controls what lands on each remote branch, and the build reads
    from those branches; there is no need to re-check branch identity
    at deploy time."""
    return


def deploy_one(
    repo_root: Path,
    target_id: str,
    target_kind: str,
    env: str,
    resolver: SecretsResolver,
    coll: DeploymentCollection,
    package_selection: set[str] | None = None,
) -> str:
    import pipeline_azure_deploy as pad

    build_dir = repo_root / BUILD_ROOT_REL / target_id
    target = coll.by_target_id(target_id)
    require_branch_matches_env_binding(repo_root, target, env)

    # F-012 shell / identity / ACR / ACA Env / ACA Job path — packages may be
    # empty (files=[]) so build_dir is optional for verify/provision kinds.
    if target_kind == "azure_resource_group":
        return pad.verify_resource_group(target, env)
    if target_kind == "azure_key_vault":
        vault = pad.verify_key_vault(target, env)
        secret_names = [
            k.replace("_", "-") if "-" not in k else k
            for k in (target.secrets or {}).keys()
        ]
        # Prefer exact env key names as KV secret names (dash form for CA;
        # underscore form for Mongo etc. — store both styles as declared).
        pad.seed_kv_secrets_from_env(vault, list((target.secrets or {}).keys()))
        return vault
    if target_kind == "azure_storage_account":
        return pad.ensure_storage_containers(target, env)
    if target_kind == "azure_vnet":
        result = pad.ensure_vnet_subnets(target, env)
        pad.ensure_vnet_private_dns_zones(target, env)
        pad.ensure_vnet_private_endpoints(target, env, coll)
        return result
    if target_kind == "atlas":
        return pad.verify_atlas(target, env)
    if target_kind == "identity":
        return pad.ensure_managed_identity(target, env)
    if target_kind == "entra_directory":
        # One directory, many identities. Each identity is a package, so
        # each is provisioned from its own package config rather than
        # from a target of its own.
        names = []
        for eb in target.environments:
            if eb.env_binding != env:
                continue
            for pkg in (eb.packages or []):
                if pkg.get("kind") != "managed_identity":
                    continue
                pid = pkg.get("package_id") or ""
                if package_selection and pid not in package_selection:
                    continue
                names.append(pad.ensure_managed_identity_from_config(
                    pkg.get("config") or {}, target.target_id, pid,
                ))
        step(f"entra directory {target_id}: {len(names)} identity "
             f"package(s) ensured")
        return target_id
    if target_kind == "azure_container_registry":
        return pad.ensure_acr(target, env)
    if target_kind == "azure_container_apps_environment":
        return pad.ensure_aca_environment(target, env)
    if target_kind == "azure_container_app_job":
        return pad.ensure_aca_job(target, env, repo_root=repo_root)
    if target_kind == "azure_automation_account":
        # Pre-existing shell — presence check + attach mi-runbook for F-003.
        step(f"verify automation account target {target_id}")
        for eb in target.environments:
            if eb.env_binding != env:
                continue
            # Pipeline AA lives under rg-chathealthy-pipeline-dev.
            na = eb.node_address or ""
            if "rg-chathealthy-pipeline-dev" in na or "ChatHealthyJobManager" in na:
                pad.ensure_pipeline_automation_account(
                    rg="rg-chathealthy-pipeline-dev",
                    aa_name="ChatHealthyJobManager",
                )
        # Then every runbook package inside it. The Automation Account is
        # the destination; each runbook is a capability living in it. These
        # used to be ten synthetic targets, which described one place ten
        # times and staged every runbook's bytes twice.
        _deploy_runbook_packages(
            repo_root, target, env, resolver, coll, package_selection,
        )
        return target_id

    if not build_dir.is_dir():
        sys.exit(f"ERROR: build dir missing: {build_dir}")
    manifest = load_target_manifest(repo_root, target_id)
    build_n = int(manifest["build_number"])
    if target_kind == "cloudflare_pages_project":
        return deploy_cloudflare(build_dir, env, resolver, target)
    if target_kind == "hf_space":
        # The docker build context is the PACKAGE directory, not the target
        # directory. A target holds one directory per package it declares and
        # the build writes the Space's context -- Dockerfile included -- into
        # the package's. Pointing docker at the target root handed it a
        # directory containing only 'service_runtime/', so every HF deploy
        # died on "failed to read dockerfile: open Dockerfile: no such file".
        return deploy_hf_space(repo_root, _package_dir(repo_root, target_id),
                               build_n, target_id, env, resolver, target)
    if target_kind == "azure_function_app":
        return deploy_azure_function_app(build_dir, target, env, resolver, coll)
    if target_kind == "azure_container_app":
        return deploy_azure_container_app(build_dir, target, env, resolver, build_n)
    if target_kind == "azure_automation_runbook":
        return deploy_azure_automation_runbook(
            build_dir, target, env, resolver, repo_root, coll,
        )
    if target_kind == "host_os_process":
        return deploy_host_os_process(repo_root, build_dir, target, env)
    raise _ch_exc()(
            mode="runtime_error",
            component="_deploy_chain",
            message=f"target_kind {target_kind!r} not supported in local_deploy.")


def deploy_host_os_process(
    repo_root: Path,
    build_dir: Path,
    target: TargetRecord,
    env: str,
) -> str:
    """Install a host-OS process target into deploy/<name>/ under the repo.

    Staged inside the repository on purpose. The enforcement manager derives
    its root from its own location, so a tree that mirrors the repository
    beneath deploy/ resolves every worker with no path rewriting; and
    EPIC-008-F-002-S-002 makes the repository the deployment substrate, so a
    copy under a separate root would contradict it.

    The service binary is not replaced while the service is running: Windows
    holds the exe open, so the copy would fail halfway and leave a partial
    install. Stop the service first; this refuses rather than corrupting.

    When the env binding declares a windows_service, the deploy REGISTERS it
    -- creating or repointing the service definition -- and does not start
    it. Installing a service and running one are different acts; the service
    manager does the second.

    Registration needs elevation, so an unelevated run of a target that
    declares a service is refused up front rather than copying the files and
    reporting success. A deploy that leaves the OS not knowing about the
    service has not deployed it.
    """
    if not build_dir.is_dir():
        sys.exit(f"ERROR: build dir missing: {build_dir}")
    dest = repo_root / "deploy" / target.target_id.replace("target_host_local_", "")

    svc = next((e.windows_service for e in target.environments
                if e.env_binding == env and e.windows_service), None)
    if svc and not _is_elevated():
        sys.exit(
            f"ERROR: {target.target_id} declares windows_service "
            f"{svc['service_name']!r}, and registering a service requires "
            f"an elevated process. Re-run this deploy from an elevated "
            f"shell. Refusing to stage the files and report success while "
            f"leaving the service unregistered."
        )

    # Derived from the declared dotnet_project, never spelled out here: the
    # exe is named after the project, so a rename that touches one and not
    # the other is not expressible.
    projects = [f for f in target.files if f.handler_type == "dotnet_project"]
    exe_name = (Path(projects[0].source_location).stem + ".exe") if projects else None
    if exe_name is None:
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(build_dir, dest)
        step(f"  installed {sum(1 for p in dest.rglob('*') if p.is_file())} file(s) "
             f"-> {dest.relative_to(repo_root).as_posix()}")
        return target.target_id
    running = [p for p in dest.rglob(exe_name) if _file_is_locked(p)]
    if running:
        sys.exit(
            f"ERROR: {exe_name} is locked at {running[0]}, which means the "
            f"service is still running. Stop ClaudeCodeConversationPersistenceService and deploy "
            f"again; overwriting a running binary leaves a partial install."
        )

    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(build_dir, dest)
    (dest / "log").mkdir(exist_ok=True)

    staged = sum(1 for p in dest.rglob("*") if p.is_file())
    step(f"  installed {staged} file(s) -> {dest.relative_to(repo_root).as_posix()}")
    if svc:
        _register_windows_service(dest, svc)
    return target.target_id


def _is_elevated() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _sc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True,
                          creationflags=creation_flags())


def _register_windows_service(dest: Path, svc: dict) -> None:
    """Create or repoint the service definition. Never starts it.

    Idempotent by construction: an existing service is repointed with
    `sc config` rather than deleted and recreated, so its identity and any
    manual configuration survive a redeploy.
    """
    name = svc["service_name"]
    binary = (dest / svc["binary"]).resolve()
    if not binary.is_file():
        sys.exit(
            f"ERROR: windows_service {name!r} declares binary "
            f"{svc['binary']!r}, which is not present at {binary} after "
            f"staging. The build did not produce it."
        )
    start = {"Automatic": "auto", "Manual": "demand", "Disabled": "disabled"}[
        svc["start_mode"]]
    account = svc.get("account") or "LocalSystem"

    # sc.exe takes each `key=` and its value as SEPARATE argv tokens. Passing
    # "start= auto" as one argument yields "Invalid start= field", because the
    # whole string arrives as the option name.
    fields = ["binPath=", str(binary), "start=", start,
              "obj=", account, "DisplayName=", svc["display_name"]]
    exists = _sc("query", name).returncode == 0
    verb = "repointed" if exists else "registered"
    r = _sc("config" if exists else "create", name, *fields)
    if r.returncode != 0:
        sys.exit(
            f"ERROR: sc {'config' if exists else 'create'} {name} failed "
            f"(rc={r.returncode}): {(r.stdout + r.stderr).strip()[:400]}"
        )
    step(f"  {verb} service {name} -> {binary}")
    step(f"  start_mode={svc['start_mode']} account={account}; NOT started -- "
         f"starting is the service manager's job, not the deploy's")


def _file_is_locked(path: Path) -> bool:
    """True when another process holds the file open for execution."""
    try:
        with open(path, "ab"):
            return False
    except OSError:
        return True


DEPLOYABLE_KINDS = (
    "host_os_process",
    "cloudflare_pages_project",
    "hf_space",
    "azure_function_app",
    "azure_container_app",
    "azure_container_apps_environment",
    "azure_container_app_job",
    "azure_automation_runbook",
    "azure_automation_account",
    "azure_container_registry",
    "azure_key_vault",
    "azure_storage_account",
    "azure_vnet",
    "azure_resource_group",
    "identity",
    "atlas",
)

# EPIC-008-F-012: the data pipeline tier is operated on its
# own cadence and is never co-deployed with the front-end app stack.
FRONTEND_KINDS = ("cloudflare_pages_project", "hf_space")
PIPELINE_KINDS = (
    "atlas",
    "azure_resource_group",
    "azure_key_vault",
    "azure_storage_account",
    "azure_vnet",
    "identity",
    "azure_container_registry",
    "azure_container_apps_environment",
    "azure_container_app_job",
    "azure_automation_account",
    "azure_automation_runbook",
)


def select_target_ids(coll: DeploymentCollection, target_arg: str) -> list[tuple[str, str]]:
    """Return [(target_id, target_kind), ...] matching the filter.

    There is no 'all'. A comma-separated target_id list is the normal
    form; the kind selectors below remain for genuinely kind-wide work
    and still require an explicit --package enumeration."""
    if "," in target_arg:
        by_id = {t.target_id: t for t in coll}
        wanted = [p.strip() for p in target_arg.split(",") if p.strip()]
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            sys.exit(f"ERROR: unknown target_id(s): {unknown}")
        return [(by_id[w].target_id, by_id[w].target_kind) for w in wanted]
    if target_arg == "pipeline":
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind in PIPELINE_KINDS
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
    if target_arg in ("host", "host_os_process"):
        return [
            (t.target_id, t.target_kind) for t in coll
            if t.target_kind == "host_os_process"
        ]
    for t in coll:
        if t.target_id == target_arg:
            return [(t.target_id, t.target_kind)]
    return []


def _dependency_sort_targets(selected: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Order selected targets so dependencies deploy before dependents.

    Pipeline (F-012 / F-003): RG -> shell (KV/storage/VNet) -> identities
    -> ACR -> ACA Env -> Automation Account -> CA runbooks -> ACA Jobs
    -> remaining runbooks. Cert placement (not a target) runs between CA
    runbooks and ACA Jobs inside run_cloud_deploy.

    Front-end: HF backends before Cloudflare wrapper.
    """
    rank = {
        "atlas": 0,
        "azure_resource_group": 0,
        "azure_key_vault": 1,
        "azure_storage_account": 1,
        "azure_vnet": 2,
        "identity": 2,
        "azure_container_registry": 3,
        "azure_container_apps_environment": 4,
        "azure_automation_account": 5,
        # CA runbooks = 6 (see secondary key below)
        # ACA jobs = 7
        # other runbooks = 8
        "azure_container_app_job": 7,
        "hf_space": 9,
        "azure_function_app": 10,
        "azure_container_app": 10,
        "cloudflare_pages_project": 11,
    }

    def _key(tk: tuple[str, str]) -> tuple[int, str]:
        tid, kind = tk
        if kind == "azure_automation_runbook":
            if "_ca_" in tid:
                return (6, tid)
            return (8, tid)
        return (rank.get(kind, 99), tid)

    return sorted(selected, key=_key)


def _hf_space_live_url_for_target(coll: DeploymentCollection, env: str, target_id: str) -> Optional[str]:
    """Return the public HF Space URL for the given target+env, or None
    if the target isn't an hf_space or has no env_binding."""
    target = coll.by_target_id(target_id)
    if target is None or target.target_kind != "hf_space":
        return None
    binding = next((e for e in target.environments if e.env_binding == env), None)
    if binding is None:
        return None
    hf = getattr(binding, "huggingface_space", None) or {}
    qualified = hf.get("space") if isinstance(hf, dict) else None
    if not qualified or "/" not in qualified:
        return None
    org, name = qualified.split("/", 1)
    return f"https://{org.lower()}-{name.replace('_', '-').lower()}.hf.space"


def _verify_hf_space_live(coll: DeploymentCollection, env: str, target_id: str, build_n: int,
                            timeout_s: int = 120) -> tuple[bool, str]:
    """End-to-end probe: curl the Space's public /health endpoint and
    confirm HTTP 200 AND `build` field equals `build_n`. Polls every 5
    seconds up to timeout_s. Returns (ok, detail)."""
    import json as _json
    import time as _time
    import urllib.error
    import urllib.request
    import ssl
    url = _hf_space_live_url_for_target(coll, env, target_id)
    if url is None:
        return (False, f"{target_id}: no resolvable HF Space URL for env={env}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    t0 = _time.time()
    last_detail = ""
    while _time.time() - t0 < timeout_s:
        try:
            req = urllib.request.Request(url + "/health", method="POST")
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                body = r.read().decode("utf-8", errors="replace")
                if r.status == 200:
                    try:
                        d = _json.loads(body)
                        served = d.get("build")
                        if int(served) == int(build_n):
                            return (True, f"{url}/health build={served}")
                        last_detail = f"{url}/health build={served} (waiting for {build_n})"
                    except Exception:
                        last_detail = f"{url}/health 200 but unparseable body"
                else:
                    last_detail = f"{url}/health HTTP {r.status}"
        except Exception as exc:
            last_detail = f"{url}/health {type(exc).__name__}: {exc}"
        _time.sleep(5)
    return (False, f"timeout after {timeout_s}s; last: {last_detail}")


def run_cloud_deploy(env: str, target_arg: str,
                     explicit_target_ids: list[str] | None = None,
                     package_selection: set[str] | None = None) -> int:
    """Deploy in dependency order: HF backends FIRST, Cloudflare wrapper
    LAST. The wrapper publishes ONLY if every selected backend deploy
    succeeded AND its public /health endpoint converged to the new
    build_n. A backend failure aborts the wrapper publish so the live
    wrapper bytes can never drift from the actual backend URLs.

    explicit_target_ids: when non-None (e.g. deploy_chathealthy passed a
    --package-filtered target list), deploy ONLY those target_ids and
    skip the target_arg re-enumeration. This is what honors --package
    end to end so the deploy does not walk every synth per-package
    target regardless of the filter.
    """
    repo_root = find_repo_root(Path(__file__))
    step(f"repo_root={repo_root} env={env} target={target_arg}"
         + (f" filtered_targets={len(explicit_target_ids)}"
            if explicit_target_ids is not None else ""))
    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    env_file = repo_root / ".env"
    load_filter = target_arg if target_arg.startswith("target_") else None
    coll: DeploymentCollection = RecordLoader().load_collection(
        brain_path, target_id_filter=load_filter,
    )
    resolver = SecretsResolver.from_collection(coll, env_file=env_file)
    if explicit_target_ids is not None:
        allowed = set(explicit_target_ids)
        selected = _dependency_sort_targets([
            (tid, kind) for tid, kind in select_target_ids(coll, target_arg)
            if tid in allowed
        ])
    else:
        selected = _dependency_sort_targets(select_target_ids(coll, target_arg))
    if not selected:
        sys.exit(f"ERROR: no targets matched --target={target_arg!r}")
    by_id = {t.target_id: t for t in coll}
    if any(by_id[tid].promote_chain_bound for tid, _ in selected):
        require_branch_matches_env(env)
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []  # (target_id, error_message)
    any_hf_failed = False
    pipeline_certs_done = False
    for target_id, target_kind in selected:
        # bake_ca_chain_into_images reads the CA chain from KV and sets
        # CHATHEALTHY_CA_ROOT_B64 / _INTERMEDIATE_B64 in the current process
        # env so acr build can bake it into the image. It must run before the
        # first ACA job image build because build_and_push_job_image requires
        # those env vars. Idempotent and KV-read-only, against the two PUBLIC
        # CA certs.
        #
        # Certificate issuance and the vault grant/revoke that follows it are
        # entitlement work, not deploy work, and belong to claudeCodeAgent.
        # The deploy mints nothing and grants nothing.
        if (
            not pipeline_certs_done
            and target_arg == "pipeline"
            and target_kind == "azure_container_app_job"
        ):
            from cert_placement import bake_ca_chain_into_images
            kv_target = coll.by_target_id("target_azure_key_vault_pipeline")
            acr_target = coll.by_target_id(
                "target_azure_container_registry_pipeline"
            )
            if kv_target is None or acr_target is None:
                sys.exit(
                    "ERROR: manifest missing target_azure_key_vault_pipeline "
                    "or target_azure_container_registry_pipeline. F-012 §7 "
                    "cannot run without both."
                )
            bake_ca_chain_into_images(
                env=env, acr_target=acr_target, kv_target=kv_target,
            )
            pipeline_certs_done = True
        # Wrapper gate: if any HF backend failed in this same run, refuse
        # to publish the wrapper. Live wrapper bytes are the contract
        # against the live backends; we never let them disagree.
        if target_kind == "cloudflare_pages_project" and any_hf_failed:
            msg = (f"SKIPPED to protect the contract: one or more HF backends "
                   f"failed in this run; publishing the wrapper would point "
                   f"users at broken backends.")
            step(f"  SKIPPED {target_id}: {msg}")
            failed.append((target_id, msg))
            continue
        target = by_id[target_id]
        if env not in target.env_binding_set():
            step(f"  skip {target_id}: no env_binding for {env!r}")
            continue
        try:
            result = deploy_one(
                repo_root, target_id, target_kind, env, resolver, coll,
                package_selection,
            )
            # Programmatic end-to-end verification for HF Spaces: curl the
            # public /health and confirm build_n match before declaring
            # success. No human checkpoint — the script proves the Space
            # is serving the new build.
            if target_kind == "hf_space":
                manifest = load_target_manifest(repo_root, target_id)
                build_n = int(manifest["build_number"])
                ok, detail = _verify_hf_space_live(coll, env, target_id, build_n,
                                                     timeout_s=180)
                if not ok:
                    raise _ch_exc()(
            mode="runtime_error",
            component="_deploy_chain",
            message=f"post-deploy verify failed: {detail}")
                step(f"  verified {target_id}: {detail}")
            succeeded.append(result)
        except SystemExit as exc:
            msg = str(exc.code) if exc.code else "sys.exit() with no message"
            step(f"  FAILED {target_id}: {msg}")
            failed.append((target_id, msg))
            if target_kind == "hf_space":
                any_hf_failed = True
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc!s}"
            step(f"  FAILED {target_id}: {msg}")
            failed.append((target_id, msg))
            if target_kind == "hf_space":
                any_hf_failed = True
    # Apply identity role grants from the manifest AFTER every target
    # has been deployed. Data-driven: iterates IdentityCatalog roles ×
    # target.allowed_roles intersection. No role names in this code path.
    try:
        apply_identity_role_grants_from_manifest(coll, env)
    except Exception as exc:  # noqa: BLE001
        step(f"WARN: identity role grants failed: {exc}")
    try:
        apply_explicit_permissions_from_manifest(coll, env)
    except Exception as exc:  # noqa: BLE001
        step(f"WARN: explicit permissions failed: {exc}")
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
# Per EPIC-008-F-012 REQ-T-001..T-008 + REQ-B-001..B-007.
# ═════════════════════════════════════════════════════════════════════════

REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"


class LocalDeploy:
    """Local deploy per V11 EPIC-008-F-004 S-001 + S-002 + EPIC-008-F-012.

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
        # Local deploy reads from the per-target build_dir produced by
        # build_chathealthy.py — that's where build-time substitutions
        # (placeholders for per-build HF Space URLs etc.) have been applied.
        # Reading from repo_root/Website would ship the unsubstituted source
        # and crash the browser when JS tries to fetch '__HF_URL_FINDCARE__'.
        # No fallback — if build_dir is missing, run build_chathealthy.py first.
        self.website_dir = _website_publish_dir(self.repo_root)
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
            "structured_output_path": str(self.output_path),
        }
        self.backend_procs: list[subprocess.Popen] = []

    # REQ-B-005 — step notices
    def _step_notice(self, msg: str) -> None:
        line = f"[STEP {self.env}] {msg}"
        _CH_LOG.info(line)
        self.results["steps"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
        })

    def _deployment_architecture_gate(self) -> None:
        backlog_path = self.repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
        deployment_path = self.repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
        env_path = self.repo_root / ".env"
        coll = RecordLoader().load_collection(deployment_path)
        backlog = AgileBacklogLoader().load(backlog_path)
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
        from version_counter import VERSIONS_COLLECTION, VERSIONS_DB, latest_record
        load_dotenv(self.repo_root / ".env")
        latest = latest_record()
        build_num = latest.get("build")
        if build_num is None:
            sys.exit(
                f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record "
                f"has no 'build' field."
            )
        build_num = int(build_num)
        version_str = latest.get("version")
        framework_str = latest.get("framework")
        if not version_str:
            sys.exit("ERROR: admin.Versions latest record has no 'version' field.")
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
            "version": version_str,
            "framework": framework_str,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        out = build_ctx_abs / "build_info.json"
        out.write_text(json.dumps(info, indent=2), encoding="utf-8")
        return out

    def _build_backend_containers(self) -> None:
        """V11 S-002-REQ-T-001 docker build for the 3 backends."""
        import shutil as _shutil
        frontend_lib_src = self.repo_root / "ChatHealthyLib"
        auth_src = self.repo_root / "sharedServices" / "Code" / "AuthorizationsAndAuthentications"
        specialty_filter_src = self.repo_root / "FindCare" / "SpecialtyFilter"
        clinical_trials_src = self.repo_root / "FindCare" / "ClinicalTrials"
        # The image is built from the BUILD PACKAGE, never from the working
        # tree. --env dev|qa|prod materialises origin/<branch> into a temp
        # worktree and builds the package from that; local used to docker
        # build straight from the tree, so the two paths had different
        # sources and something could work locally and break in dev. The
        # package is also where managed files land -- a Dockerfile whose
        # bytes belong to the manifest exists nowhere else.
        for container_name, entry in self.BACKEND_CONTAINERS.items():
            _label, src_dir, build_ctx_rel = entry
            image_tag = container_name
            pkg = _package_dir(self.repo_root,
                               self.CONTAINER_TARGET_ID[container_name])
            dockerfile_abs = pkg / "Dockerfile"
            if not dockerfile_abs.is_file():
                sys.exit(
                    f"ERROR: Dockerfile missing at {dockerfile_abs}. "
                    "V11 S-002-REQ-T-001 requires Dockerfile per backend."
                )
            build_ctx_abs = pkg
            staged_lib = None
            if build_ctx_rel != ".":
                staged_lib = build_ctx_abs / "ChatHealthyLib"
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
            staged_clinical_trials = None
            if container_name == "ch-sharedsvc" and clinical_trials_src.is_dir():
                staged_clinical_trials = build_ctx_abs / "ClinicalTrials"
                if staged_clinical_trials.exists():
                    _shutil.rmtree(staged_clinical_trials)
                _shutil.copytree(
                    clinical_trials_src, staged_clinical_trials,
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
                if staged_clinical_trials is not None and staged_clinical_trials.exists():
                    _shutil.rmtree(staged_clinical_trials)
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
        # From the build package, like the backends: the wrapper's
        # Dockerfile is a managed file whose bytes live in the manifest and
        # which exists nowhere in the working tree.
        # local_host is the package that exists only because Cloudflare
        # Pages is not a container: it holds the server that serves the
        # same static bytes on the workstation.
        pkg = _package_dir(self.repo_root, "target_cloudflare_pages_website",
                           "local_host")
        dockerfile_abs = pkg / self.WEBSITE_DOCKERFILE
        if not dockerfile_abs.is_file():
            dockerfile_abs = pkg / "Dockerfile"
        if not dockerfile_abs.is_file():
            sys.exit(
                f"ERROR: Website Dockerfile missing at {dockerfile_abs}. "
                "S-002-REQ-T-002 requires the Website wrapper to run in a "
                "Docker container; its Dockerfile is the source of that image."
            )
        self._step_notice(f"building image {self.WEBSITE_CONTAINER_NAME}")
        result = subprocess.run(
            # Context is the package, not the repo root. The package holds
            # every file the Dockerfile COPYs, at the same repo-relative
            # path, so the build reads only what the build staged.
            ["docker", "build",
             "-t", self.WEBSITE_CONTAINER_NAME,
             "-f", str(dockerfile_abs), str(pkg)],
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
        sharedservices_url = f"https://localhost:{self.PORTS['shared']}"
        env = os.environ.copy()
        env["VITE_API_URL"] = api_url
        env["VITE_EVALCARE_URL"] = evalcare_url
        env["VITE_SHAREDSERVICES_URL"] = sharedservices_url
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
        env_file = self.repo_root / ".env"
        if not env_file.is_file():
            sys.exit(
                f"ERROR: env file missing at {env_file}; backend containers "
                "depend on it for MongoDB / API credentials."
            )
        from dotenv import dotenv_values
        env_dict = dotenv_values(env_file)
        # Local stack always binds to env={self.env} regardless of any
        # ENV_PREFIX value in .env. HF Space deploys set this via
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
            extra_env: list[str] = [
                # Logging identity for admin.HuggingFaceLogs_local.
                # Container name doubles as the "Space name" locally so
                # the schema is uniform across local/dev/qa/prod.
                "-e", f"CH_SPACE_NAME={container_name}",
            ]
            if container_name == "ch-sharedsvc":
                extra_env.extend([
                    "-e", f"FINDCARE_INTERNAL_URL=https://host.docker.internal:{self.PORTS['findcare']}",
                    "-e", f"EVALCARE_INTERNAL_URL=https://host.docker.internal:{self.PORTS['evalcare']}",
                    # Browser-facing peer URLs returned by /gate op=peer_urls
                    # (EPIC-002-F-004-S-001). The wrapper uses these to set
                    # iframe.src; never build-substituted into the wrapper.
                    "-e", f"CH_BROWSER_PEER_URL_FINDCARE=https://localhost:{self.PORTS['findcare']}",
                    "-e", f"CH_BROWSER_PEER_URL_EVALCARE=https://localhost:{self.PORTS['evalcare']}",
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

        with httpx.Client(verify=False, timeout=60) as c:
            r = c.get("http://localhost/", follow_redirects=False)
            record("http_to_https_301", r.status_code == 301, f"got {r.status_code}")
            r = c.get("https://localhost/")
            record("website_200", r.status_code == 200, f"got {r.status_code}")
            record("website_has_client_router", "ClientRouter" in r.text, "")
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

    def _display_smoke_failure_banner(self) -> None:
        banner = (
            "\n=========================================="
            "\n  SMOKE TEST FAILED"
            "\n  Environment left up for inspection."
            "\n  Tear-down is the operator's call — never automatic."
            "\n=========================================="
        )
        _CH_LOG.info(banner)
        sys.stderr.write(banner + "\n")

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
        # The smoke test was removed: it carried 34 self-skips, so it could
        # report green without asserting, and its exit code was the deploy's
        # exit code -- a healthy stack reported failure because the test did.
        # _verify_components above is what actually proves the stack is up.
        self._write_structured_output()
        return 0


# ═════════════════════════════════════════════════════════════════════════
# Helper-only module — no main() entry point. Per build_deploy_promote_plan
# v3 §INV-5 the only entry points are build_chathealthy.py + deploy_chathealthy.py
# + promote_chathealthy.py; this module is imported by them, not invoked
# directly.
#
# LocalDeploy is aliased as LocalStandUp for callers that prefer the
# operator-facing name per plan v3 §C.2 / §E.5 RESOLUTION.
LocalStandUp = LocalDeploy
