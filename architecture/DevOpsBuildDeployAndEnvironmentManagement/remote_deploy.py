"""Remote deploy (dev/qa/prod) driven by deployment_architecture.json.

The one and only deploy route to dev/qa/prod. Reads the brain artifact;
Crosswalk validates managed bytes byte-equal disk and referenced files
exist on disk; for each TargetRecord with the requested env binding,
dispatches by target_kind to a packaging+push function that materializes
managed bytes over the staging tree (so JSON-owned Dockerfile/workflow
YAML bytes replace any disk copy) and ships the staging to the target.

usage:
  python -m remote_deploy --env dev
  python -m remote_deploy --env qa
  python -m remote_deploy --env prod
"""
from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Local imports (the module lives in the substrate code dir; tests and
# direct-script invocation insert the dir onto sys.path).
from agile_backlog import AgileBacklogLoader
from builder import _EXCLUDE_DIRS, _EXCLUDE_FILE_NAMES, _EXCLUDE_FILE_SUFFIXES
from crosswalk import Crosswalk
from extractor import Extractor
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord


# ── HF Space name convention ───────────────────────────────────────────
# Per the existing workflow YAMLs (preserved here so behavior matches):
#   prod                 -> <Base>
#   dev/qa/feature       -> <env>_<Base>
_HF_SPACE_BASE: dict[str, str] = {
    "target_hf_space_findcare_backend":      "ChatHealthySpace",
    "target_hf_space_evaluatecare_backend":  "EvaluateCareSpace",
    "target_hf_space_shared_services":       "SharedServicesSpace",
}
_HF_ORG: str = "SkipSnow"

# Branch mapping for deploy sources. The deploy snapshot comes from
# origin/<branch>, not from the operator's working tree. This honors
# the F-012-S-003 requirement "Deployments come from correct Code
# branches and file location" — what gets deployed is the committed
# state in git, no working-tree mutation (incl. Windows autocrlf).
_BRANCH_MAP: dict[str, str] = {"dev": "dev", "qa": "qa", "prod": "main"}


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
    print(f"[remote_deploy] {msg}", flush=True)


# ── Committed-tree snapshot (origin/<branch>) ──────────────────────────
def _resolve_committed_build_root(worktree_root: Path, env: str) -> tuple[Path, str, str]:
    """Snapshot origin/<branch> for env into _oneshots/remote_deploy_build/.

    Returns (workspace_path, branch, origin_sha). All subsequent
    deploy operations should use workspace_path as their `repo_root`
    so disk bytes are byte-identical to what Linux GHA / HF runners
    see. The autocrlf=false override is critical on Windows — without
    it, git autocrlf would inject CRLF into the extracted tree and
    cause spurious Crosswalk drift against the LF-canonical JSON.

    Gitignored runtime artifacts (the operator's local Code/.env and
    Code/Shared/ops/certs/*.key signing keys + cert files) are copied
    from the operator's local repo into the snapshot, because they
    are not in git but the deploy needs them to authenticate to HF /
    Cloudflare and to provision HF Space env vars/secrets.
    """
    branch = _BRANCH_MAP[env]
    cflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # 1. Fetch
    r = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=str(worktree_root), capture_output=True, text=True,
        creationflags=cflags,
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: git fetch origin {branch} failed: "
            f"{r.stderr.strip()[:300]}"
        )
    # 2. Resolve origin SHA
    r = subprocess.run(
        ["git", "rev-parse", f"origin/{branch}"],
        cwd=str(worktree_root), capture_output=True, text=True,
        creationflags=cflags,
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: no origin/{branch}: {r.stderr.strip()[:300]}"
        )
    origin_sha = r.stdout.strip()
    origin_sha7 = origin_sha[:7]
    # 3. Workspace
    workspace = (worktree_root / "_oneshots" / "remote_deploy_build"
                 / env / origin_sha7)
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    # 4. git archive | tar -x. autocrlf=false ensures LF bytes match
    #    JSON's embedded_content (and what Linux runners check out).
    archive_proc = subprocess.Popen(
        ["git", "-c", "core.autocrlf=false",
         "archive", f"origin/{branch}"],
        cwd=str(worktree_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=cflags,
    )
    tar_proc = subprocess.Popen(
        ["tar", "-x", "-C", str(workspace)],
        stdin=archive_proc.stdout, stderr=subprocess.PIPE,
        creationflags=cflags,
    )
    if archive_proc.stdout is not None:
        archive_proc.stdout.close()
    tar_err = tar_proc.communicate()[1]
    archive_proc.wait()
    if archive_proc.returncode != 0:
        sys.exit(
            f"ERROR: git archive origin/{branch} failed: "
            f"{(archive_proc.stderr.read().decode() if archive_proc.stderr else '')[:300]}"
        )
    if tar_proc.returncode != 0:
        sys.exit(
            f"ERROR: tar extract into {workspace} failed: "
            f"{(tar_err.decode() if tar_err else '')[:300]}"
        )
    # 5. Copy gitignored runtime artifacts the deploy needs but git
    #    doesn't track. The OPERATOR's local repo has these.
    env_src = worktree_root / "Code" / ".env"
    if env_src.is_file():
        (workspace / "Code").mkdir(parents=True, exist_ok=True)
        shutil.copy2(env_src, workspace / "Code" / ".env")
    certs_src = worktree_root / "Code" / "Shared" / "ops" / "certs"
    certs_dst = workspace / "Code" / "Shared" / "ops" / "certs"
    certs_dst.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.key", "*.csr", "*.enc", "*.srl"):
        for f in certs_src.glob(pattern):
            shutil.copy2(f, certs_dst / f.name)
    _step(
        f"build root: origin/{branch} @ {origin_sha7} -> {workspace}"
    )
    return workspace, branch, origin_sha


# ── HF API: variables + secrets ────────────────────────────────────────
def _hf_curl_delete(token: str, space: str, kind: str, key: str) -> None:
    """Delete a variable or secret on an HF Space (idempotent — 404 is fine)."""
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
        "key": key, "value": value, "description": "Set by remote_deploy",
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
        "key": key, "value": value, "description": "Set by remote_deploy",
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
# These are the directories the deploy ships for each HF Space target.
# Authored to mirror the existing workflow YAMLs exactly (preserves
# current ship behavior); excludes match the workflow rsync filters.
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
    ]


def _copy_tree(
    src_root: Path, dst_root: Path,
    src_rel: str, dst_rel: str,
) -> None:
    """Copy a subtree into staging using the SAME exclusion rules
    Builder uses to enumerate source_locations. The two paths must
    agree byte-for-byte: a file Builder didn't enumerate must not
    land in the staging that ships to the target.
    """
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


# ── React build for FindCare ──────────────────────────────────────────
def _build_react_frontend(repo_root: Path, env: str) -> None:
    frontend = repo_root / "Code" / "ConversationalUX" / "FindCareChat" / "frontend"
    if not (frontend / "package.json").is_file():
        raise FileNotFoundError(f"frontend package.json missing at {frontend}")
    evalcare_peer = _hf_peer_url("target_hf_space_evaluatecare_backend", env)
    env_for_build = dict(os.environ)
    env_for_build["VITE_API_URL"] = ""
    env_for_build["VITE_EVALCARE_URL"] = evalcare_peer
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


# ── HF Space deploy ────────────────────────────────────────────────────
def _push_to_hf_space(
    repo_root: Path, target: TargetRecord, env: str,
    hf_token: str, env_values: dict[str, str],
) -> None:
    target_id = target.target_id
    space = _hf_space_name(target_id, env)
    _step(f"hf_space target={target_id} env={env} space={_HF_ORG}/{space}")

    # 1) React build (FindCare only — its package ships pre-built static/).
    if target_id == "target_hf_space_findcare_backend":
        _build_react_frontend(repo_root, env)

    # 2) Set per-target env vars + secrets on the HF Space.
    findcare_peer  = _hf_peer_url("target_hf_space_findcare_backend",      env)
    evalcare_peer  = _hf_peer_url("target_hf_space_evaluatecare_backend",  env)
    shared_peer    = _hf_peer_url("target_hf_space_shared_services",        env)
    _hf_set_variable(hf_token, space, "ENV_PREFIX", env)
    if target_id == "target_hf_space_findcare_backend":
        _hf_set_variable(hf_token, space, "EVALCARE_URL", evalcare_peer)
        _hf_set_variable(hf_token, space, "SHARED_SERVICES_URL", shared_peer)
    elif target_id == "target_hf_space_evaluatecare_backend":
        _hf_set_variable(hf_token, space, "FINDCARE_URL", findcare_peer)
        _hf_set_variable(hf_token, space, "SHARED_SERVICES_URL", shared_peer)
    elif target_id == "target_hf_space_shared_services":
        _hf_set_variable(hf_token, space, "FINDCARE_URL", findcare_peer)
        _hf_set_variable(hf_token, space, "EVALCARE_URL", evalcare_peer)

    # Public certs from repo (Code/Shared/ops/certs/*.crt) - base64'd.
    certs_dir = repo_root / "Code" / "Shared" / "ops" / "certs"

    def _b64(p: Path) -> str:
        return base64.b64encode(p.read_bytes()).decode("ascii")
    ca_b64        = _b64(certs_dir / "ca.crt")
    findcare_b64  = _b64(certs_dir / "findcare.crt")
    shared_b64    = _b64(certs_dir / "shared.crt")
    evalcare_b64  = _b64(certs_dir / "evalcare.crt")
    _hf_set_variable(hf_token, space, "CA_CERT_PEM",       ca_b64)
    _hf_set_variable(hf_token, space, "FINDCARE_CERT_PEM", findcare_b64)
    _hf_set_variable(hf_token, space, "SHARED_CERT_PEM",   shared_b64)
    _hf_set_variable(hf_token, space, "EVALCARE_CERT_PEM", evalcare_b64)

    # Private signing key — read from local certs dir (.gitignored).
    signing_map = {
        "target_hf_space_findcare_backend":     ("findcare.key", "FINDCARE_SIGNING_KEY_PEM"),
        "target_hf_space_evaluatecare_backend": ("evalcare.key", "EVALCARE_SIGNING_KEY_PEM"),
        "target_hf_space_shared_services":      ("shared.key",   "SHARED_SIGNING_KEY_PEM"),
    }
    key_file, secret_name = signing_map[target_id]
    signing_b64 = _b64(certs_dir / key_file)
    _hf_set_secret(hf_token, space, secret_name, signing_b64)

    if target_id == "target_hf_space_findcare_backend":
        gemini = env_values.get("GEMINI_API_KEY")
        if not gemini:
            sys.exit("ERROR: GEMINI_API_KEY not set in .env; FindCare backend deploy aborted.")
        _hf_set_secret(hf_token, space, "GEMINI_API_KEY", gemini)

    # 3) Stage source dirs to a clean workspace.
    workspace = repo_root / "_oneshots" / "remote_deploy_build" / env / target_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    if target_id == "target_hf_space_findcare_backend":
        source_set = _findcare_source_set(repo_root)
    elif target_id == "target_hf_space_evaluatecare_backend":
        source_set = _evaluatecare_source_set(repo_root)
    elif target_id == "target_hf_space_shared_services":
        source_set = _sharedservices_source_set(repo_root)
    else:
        raise RuntimeError(f"unknown hf_space target {target_id!r}")
    for src_rel, dst_rel in source_set:
        _step(f"  stage  {src_rel} -> {dst_rel}")
        _copy_tree(repo_root, workspace, src_rel, dst_rel or src_rel)

    # FindCare: copy the React build output into backend/static/.
    if target_id == "target_hf_space_findcare_backend":
        dist = repo_root / "Code" / "ConversationalUX" / "FindCareChat" / "frontend" / "dist"
        static_dst = workspace / "Code" / "ConversationalUX" / "FindCareChat" / "backend" / "static"
        if static_dst.exists():
            shutil.rmtree(static_dst, ignore_errors=True)
        if not dist.is_dir():
            sys.exit(f"ERROR: React dist missing at {dist}; cannot stage findcare backend.")
        shutil.copytree(dist, static_dst)

    # 4) Materialize MANAGED files (Dockerfile + workflow YAMLs) from JSON
    #    over the staging tree. This is the V18 substitution: JSON owns
    #    the bytes, so the deploy ships JSON-canonical bytes regardless
    #    of what the disk copy might have.
    Extractor().materialize(target, workspace)

    # 5) Copy the Dockerfile to the staging root (HF builds from there).
    if target_id == "target_hf_space_findcare_backend":
        shutil.copy2(workspace / "DevOps" / "FindCareBackend" / "Dockerfile", workspace / "Dockerfile")
    elif target_id in ("target_hf_space_evaluatecare_backend", "target_hf_space_shared_services"):
        # These hf_spaces flatten their source dir (evaluateCare/Code/ -> root,
        # sharedServices/Code/ -> root). Their Dockerfile is already at the
        # staging root after _copy_tree placed it via `dst_rel="."`.
        pass

    # 6) git-clone HF Space, empty it (preserving README.md), copy staging in, push.
    import tempfile
    hf_clone = Path(tempfile.mkdtemp(prefix=f"hf_{space}_"))
    repo_url = f"https://{_HF_ORG}:{hf_token}@huggingface.co/spaces/{_HF_ORG}/{space}"
    _step(f"  clone https://huggingface.co/spaces/{_HF_ORG}/{space}")
    subprocess.run(
        ["git", "clone", repo_url, str(hf_clone)],
        check=True, capture_output=True,
    )
    # Install Git LFS for this repo. HF Spaces require LFS for any
    # file over 10MB AND for "binary" types like .pdf regardless of
    # size. Without LFS the pre-receive hook rejects the push.
    subprocess.run(
        ["git", "lfs", "install", "--local"],
        cwd=str(hf_clone), check=True, capture_output=True,
    )
    # Preserve README.md (HF Space config frontmatter)
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
    # Copy staging into the HF clone.
    for item in workspace.iterdir():
        if item.is_dir():
            shutil.copytree(item, hf_clone / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, hf_clone / item.name)
    # Track binary patterns under LFS BEFORE staging. Refresh .gitattributes
    # each push so the pattern set is canonical regardless of what the
    # previous deploy left on HF. Covers PDFs (me/ context docs) plus
    # other binary types that may appear under target source dirs.
    subprocess.run(
        ["git", "lfs", "track",
         "*.pdf", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico",
         "*.zip", "*.gz", "*.tar", "*.7z"],
        cwd=str(hf_clone), check=True, capture_output=True,
    )
    # git-add, commit, push.
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
        commit_msg = f"remote_deploy {env} from deployment_architecture.json"
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


# ── Cloudflare Pages deploy ────────────────────────────────────────────
def _push_to_cloudflare_pages(
    repo_root: Path, target: TargetRecord, env: str,
    env_values: dict[str, str],
) -> None:
    project = {"dev": "chathealthy-website-dev", "qa": "chathealthy-website-qa",
               "prod": "chathealthy-website-prod"}[env]
    _step(f"cloudflare_pages_project target={target.target_id} project={project}")
    branch_for_wrangler = env
    api_token = env_values.get("CLOUDFLARE_ACCOUN_TOKEN", "")
    account_id = env_values.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not api_token or not account_id:
        sys.exit("ERROR: CLOUDFLARE_ACCOUN_TOKEN / CLOUDFLARE_ACCOUNT_ID missing in .env.")
    env_for_wrangler = dict(os.environ)
    env_for_wrangler["CLOUDFLARE_API_TOKEN"] = api_token
    env_for_wrangler["CLOUDFLARE_ACCOUNT_ID"] = account_id

    # Stage Website/ (the cloudflare target ships from the on-disk Website tree).
    workspace = repo_root / "_oneshots" / "remote_deploy_build" / env / target.target_id
    if workspace.exists():
        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    _copy_tree(repo_root, workspace, "Website", ".")
    # Materialize managed files (none today on this target, but the call
    # is the V18 contract and will pick up future Docker/YAML additions).
    Extractor().materialize(target, workspace)

    cmd = [
        "npx", "wrangler", "pages", "deploy", str(workspace),
        f"--project-name={project}",
        f"--branch={branch_for_wrangler}",
        "--commit-dirty=true",
    ]
    _step("  " + " ".join(cmd))
    subprocess.run(
        cmd, env=env_for_wrangler, check=True,
        shell=(sys.platform == "win32"),
    )


# ── Health polling per env ────────────────────────────────────────────
def _poll_backends_health(env: str, timeout_min: int = 12) -> None:
    import urllib.error
    import urllib.request
    backends = [
        _hf_peer_url("target_hf_space_findcare_backend",     env),
        _hf_peer_url("target_hf_space_evaluatecare_backend", env),
        _hf_peer_url("target_hf_space_shared_services",      env),
    ]
    deadline = time.time() + timeout_min * 60
    for url in backends:
        target_url = f"{url}/health"
        _step(f"  polling {target_url}")
        while True:
            if time.time() > deadline:
                sys.exit(f"TIMED OUT waiting for {target_url}")
            try:
                req = urllib.request.Request(target_url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        _step(f"    {target_url} -> 200")
                        break
            except (urllib.error.URLError, urllib.error.HTTPError):
                pass
            time.sleep(10)


# ── Orchestrator ──────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remote deploy driven by deployment_architecture.json"
    )
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument(
        "--skip-smoke", action="store_true",
        help="Skip the master smoke test after deploy (debug only).",
    )
    args = parser.parse_args(argv)
    env = args.env

    # Discover repo root by walking up from this module's __file__
    # looking for a .git entry. Survives any future relocation of the
    # substrate dir (no hardcoded parents[N] arithmetic).
    def _find_repo_root(start: Path) -> Path:
        p = start.resolve()
        while p != p.parent:
            if (p / ".git").exists():
                return p
            p = p.parent
        raise RuntimeError(
            f"no .git found walking up from {start}; "
            f"remote_deploy.py must run inside a git repo"
        )
    worktree_root = _find_repo_root(Path(__file__))
    _step(f"worktree_root={worktree_root} env={env}")

    # The deploy reads from a snapshot of origin/<branch>, NOT from
    # the operator's working tree. This is the F-012-S-003 contract:
    # "Deployments come from correct Code branches and file location."
    # On Windows the working tree may have autocrlf-mutated bytes that
    # diverge from what Linux runners see; the snapshot kills that.
    repo_root, deploy_branch, deploy_sha = _resolve_committed_build_root(
        worktree_root, env,
    )

    # Load + validate against the snapshot.
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
        coll=coll, backlog=backlog, repo_root=repo_root, env_values=env_values_for_leak,
    )
    if not report.is_pass:
        sys.stderr.write(report.format() + "\n")
        return report.exit_code()
    _step(f"crosswalk gate passed (targets={len(coll)}, violations=0)")

    # Materialize SecretsResolver-style env access for deploy-time auth.
    env_kv: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env_kv[k.strip()] = v.strip().strip('"').strip("'")
    hf_token = env_kv.get("HF_TOKEN", "")
    if not hf_token:
        sys.exit("ERROR: HF_TOKEN missing in Code/.env; cannot deploy to HF Spaces.")

    # Dispatch each target whose env binding includes the requested env.
    deployed: list[str] = []
    for target in coll:
        if env not in target.env_binding_set():
            continue
        kind = target.target_kind
        if kind == "hf_space":
            _push_to_hf_space(repo_root, target, env, hf_token, env_kv)
            deployed.append(target.target_id)
        elif kind == "cloudflare_pages_project":
            _push_to_cloudflare_pages(repo_root, target, env, env_kv)
            deployed.append(target.target_id)
        elif kind == "github_actions_workflow_runner":
            # No remote action — workflows live in the repo. Crosswalk
            # already validated managed YAMLs == disk bytes.
            _step(f"workflow_runner: no remote action (crosswalk validated)")
            deployed.append(target.target_id)
        else:
            sys.exit(f"unsupported target_kind {kind!r} on target {target.target_id!r}")

    # Poll backend health then smoke.
    _step(f"deployed targets: {deployed}")
    _step("polling backend /health endpoints")
    _poll_backends_health(env)

    if args.skip_smoke:
        _step("smoke skipped (--skip-smoke)")
        return 0
    _step("running master smoke test against env")
    smoke_env = dict(os.environ)
    smoke_env["SMOKE_TEST_ENV"] = env
    smoke_env["CHATHEALTHY_PROJECT_ROOT"] = str(repo_root)
    # Smoke test lives next to this module (substrate sibling); use
    # __file__-relative discovery so the path survives any future move
    # of the substrate dir.
    smoke_path = Path(__file__).resolve().parent / "localSmokeTestPyTest.py"
    rc = subprocess.call(
        [sys.executable, "-m", "pytest", str(smoke_path), "-v"],
        env=smoke_env,
    )
    _step(f"smoke test ended: rc={rc}")
    # NOTE: build-counter bump for qa/prod is owned by the promote
    # gate (promote-build.yml), not by the deploy script. The
    # promotion IS the moment qa := dev (or prod := qa); the deploy
    # is verification that the promotion's bytes work.
    return rc


if __name__ == "__main__":
    # Allow direct script invocation: ensure the substrate code dir is on sys.path.
    HERE = Path(__file__).resolve().parent
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    sys.exit(main())
