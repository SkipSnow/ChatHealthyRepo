"""local_deploy.py - operator's deploy manager.

Ships per-target build packages from local_build/v{N}/<target_id>/ to
their cloud destinations. Reads the package manifest.json for the target
facts; resolves secret VALUES from the local store (Code/.env) via
SecretsResolver bound to the live deployment_architecture.json. The
package itself contains NO secret values.

usage:
    python local_deploy.py --env dev --target cloudflare
    python local_deploy.py --env qa  --version v42
    python local_deploy.py --env prod --target target_cloudflare_pages_website

If --version is omitted, the highest v{N}/ directory present on disk
under local_build/ is used.

Per EPIC-008-F-012-S-001. Phase 4a: cloudflare_pages_project only;
HF Spaces and Azure FA still ship via old_local_publish.py until
follow-up commits subsume them.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord
import old_remote_deploy as rd


_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/local_build")

# Per-env Cloudflare Pages project map. Same as the legacy local_publish
# convention; one project per env. The branch flag passed to wrangler
# selects which deployment slot (dev/qa/prod) the bytes land in.
_CLOUDFLARE_PROJECT: dict[str, str] = {
    "dev":  "chathealthy-website-dev",
    "qa":   "chathealthy-website-qa",
    "prod": "chathealthywebsite",
}
_CLOUDFLARE_BRANCH: dict[str, str] = {
    "dev":  "dev",
    "qa":   "qa",
    "prod": "main",
}


def _step(msg: str) -> None:
    print(f"[local_deploy] {msg}", flush=True)


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


def _deploy_cloudflare(
    build_dir: Path,
    env: str,
    resolver: SecretsResolver,
) -> str:
    project = _CLOUDFLARE_PROJECT[env]
    branch = _CLOUDFLARE_BRANCH[env]
    _step(f"=== cloudflare_pages env={env} project={project} dir={build_dir} ===")
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


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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

    # Variables: derived deploy-time config (not secrets).
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

    # Cert PEMs (variables, not secrets — public certs).
    certs_dir = repo_root / "Code" / "Shared" / "ops" / "certs"

    def _b64(p: Path) -> str:
        return base64.b64encode(p.read_bytes()).decode("ascii")
    rd._hf_set_variable(hf_token, space, "CA_CERT_PEM",       _b64(certs_dir / "ca.crt"))
    rd._hf_set_variable(hf_token, space, "FINDCARE_CERT_PEM", _b64(certs_dir / "findcare.crt"))
    rd._hf_set_variable(hf_token, space, "SHARED_CERT_PEM",   _b64(certs_dir / "shared.crt"))
    rd._hf_set_variable(hf_token, space, "EVALCARE_CERT_PEM", _b64(certs_dir / "evalcare.crt"))

    # Signing key — secret, resolved from store via manifest binding.
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
    """Clone the HF Space repo, replace its tree with a 2-line FROM
    Dockerfile that pulls the GHCR image, push back."""
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


def _deploy_one(
    repo_root: Path,
    build_n: int,
    target_id: str,
    target_kind: str,
    env: str,
    resolver: SecretsResolver,
) -> str:
    build_dir = repo_root / _BUILD_ROOT_REL / f"v{build_n}" / target_id
    if not build_dir.is_dir():
        sys.exit(f"ERROR: build dir missing: {build_dir}")
    if target_kind == "cloudflare_pages_project":
        return _deploy_cloudflare(build_dir, env, resolver)
    if target_kind == "hf_space":
        return _deploy_hf_space(repo_root, build_dir, build_n, target_id, env, resolver)
    raise RuntimeError(
        f"target_kind {target_kind!r} not yet supported in local_deploy. "
        f"Use old_local_publish.py for azure_function_app "
        f"until the Phase 4 refactor lands that handler."
    )


_DEPLOYABLE_KINDS = ("cloudflare_pages_project", "hf_space")


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
    for t in coll:
        if t.target_id == target_arg:
            return [(t.target_id, t.target_kind)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ship per-target build packages from local_build/v{N}/ to cloud envs."
    )
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument(
        "--version", default=None,
        help="vN build to deploy. Default: highest v{N}/ present on disk.",
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | a specific target_id. Default: all (Phase 4a = cloudflare only).",
    )
    args = parser.parse_args(argv)

    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} env={args.env} target={args.target}")

    build_n = _resolve_version(repo_root, args.version)
    _step(f"version=v{build_n}")

    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    env_file = repo_root / "Code" / ".env"

    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    resolver = SecretsResolver.from_collection(coll, env_file=env_file)

    selected = _select_target_ids(coll, args.target)
    if not selected:
        sys.exit(f"ERROR: no targets matched --target={args.target!r}")

    # Restrict to targets that bind to the requested env.
    by_id = {t.target_id: t for t in coll}
    deployed: list[str] = []
    for target_id, target_kind in selected:
        target = by_id[target_id]
        if args.env not in target.env_binding_set():
            _step(f"  skip {target_id}: no env_binding for {args.env!r}")
            continue
        deployed.append(_deploy_one(
            repo_root, build_n, target_id, target_kind, args.env, resolver,
        ))

    if not deployed:
        sys.exit(
            f"ERROR: nothing deployed for env={args.env!r} target={args.target!r}. "
            f"Check env_binding in deployment_architecture.json."
        )
    _step(f"deployed {len(deployed)} target(s):")
    for d in deployed:
        _step(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
