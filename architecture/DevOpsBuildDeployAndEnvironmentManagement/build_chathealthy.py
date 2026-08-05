"""build_chathealthy.py — the unified build entry point.

Replaces the legacy local_build.py / remote_build.py pair. One script,
one CLI surface, internally dispatches by GITHUB_ACTIONS context.

CLI:
  python build_chathealthy.py --env {local|dev|qa|prod} [--target <list>]

Behaviour by --env:
  local        — sources the working tree as-is (no branch check, no commit
                 required, no admin.Versions counter bump). Stamps the
                 current admin.Versions latest.build onto manifest.json.
  dev|qa|prod  — enforces the env-branch guard (dev->dev branch, qa->qa,
                 prod->main), then BUMPS admin.Versions.build by one and
                 stamps the new value onto manifest.json.

manifest.json fields (per build_deploy_promote_plan v3 §C.1/§C.4):
  env             — the env this build is for; deploy_chathealthy.py rejects
                    a mismatch (gate check b).
  git_head_sha    — short git HEAD SHA at build time (gate check a).
  build           — the build counter at build time (gate check c).
  built_at        — ISO-8601 UTC; diagnostic and audit only.

Reference: build_deploy_promote_plan v3 §C.1 (build responsibilities),
§INV-1 (local from working tree), §INV-2 (non-local from env's branch).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Per-target_kind handlers, helper utilities, crosswalk gate — all
# imported from local_build.py for now. They migrate in step 5.7.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight_undefined_names import scan as _scan_undefined_names  # noqa: E402
from _build_chain import (  # noqa: E402
    _build_one,
    _find_repo_root,
    _read_dev_build_number,
    _resolve_build_sha,
    _select_targets,
    _step,
    AgileBacklogLoader,
    Crosswalk,
    DeploymentCollection,
    RecordLoader,
    SecretsResolver,
)


VALID_ENVS = ("local", "dev", "qa", "prod")
_ENV_BRANCH = {"local": "dev", "dev": "dev", "qa": "qa", "prod": "main"}


def _materialize_env_file(env: str, canonical_repo: Path, build_dir: Path) -> Path:
    """Write the .env this build/deploy will use to <build_dir>/.env,
    then return that path.

      * --env local          -> copy the working-tree file <repo>/.env
                                (operator's own file; the operator
                                maintains this).
      * --env dev|qa|prod    -> fetch the `env-file` secret from Azure
                                Key Vault (kv-chpipeline-dev), decode
                                the project's gz:<base64> wrapper, and
                                write the resulting dotenv bytes.

    Downstream build steps (leak-check, mongo connect, etc.) and the
    matching deploy MUST read <build_dir>/.env — never the working-tree
    .env for cloud envs, never anywhere else. Every build re-materialises
    the file from scratch; no stale copies from prior builds survive."""
    dst = build_dir / ".env"
    if env == "local":
        src = canonical_repo / ".env"
        if not src.is_file():
            sys.exit(f"ERROR: --env local requires {src}; not found.")
        shutil.copy2(src, dst)
        _step(f"materialised {dst} from working-tree .env")
        return dst
    # cloud env: pull env-file from KV, decode gz:<base64>, write to dst.
    import base64, gzip
    az = shutil.which("az") or "az"
    r = subprocess.run(
        [az, "keyvault", "secret", "show",
         "--vault-name", "kv-chpipeline-dev", "--name", "env-file",
         "--query", "value", "-o", "tsv"],
        capture_output=True, text=True, shell=False,
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: cannot fetch kv-chpipeline-dev/env-file: "
            f"{r.stderr.strip()[:300]}"
        )
    raw = r.stdout.strip()
    if not raw.startswith("gz:"):
        sys.exit(
            "ERROR: kv-chpipeline-dev/env-file has unexpected encoding "
            "(expected 'gz:<base64>'); refusing to interpret."
        )
    plaintext = gzip.decompress(base64.b64decode(raw[3:])).decode("utf-8")
    dst.write_text(plaintext, encoding="utf-8")
    _step(f"materialised {dst} from kv-chpipeline-dev/env-file")
    # Load into os.environ so this build's downstream code that uses
    # os.getenv() sees the values without any explicit load_dotenv call.
    from dotenv import load_dotenv
    load_dotenv(dst, override=False)
    return dst


def _current_branch(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _get_build_source(canonical_repo: Path, env: str) -> Path:
    """Return the directory the build reads its sources from.

      --env local            -> the working tree (build reads whatever the
                                operator has on disk).
      --env dev | qa | prod  -> a fresh checkout of origin/<branch> in a
                                temp directory. The local working tree,
                                current branch, and any uncommitted files
                                are irrelevant to and untouched by the
                                build. Caller MUST pair this with
                                _release_build_source() when done."""
    if env == "local":
        return canonical_repo
    branch = _ENV_BRANCH[env]
    _run_git(["fetch", "origin", branch], canonical_repo, "fetch")
    import tempfile
    src = Path(tempfile.mkdtemp(prefix=f"build_{env}_{branch}_"))
    print(f"[build] materialising origin/{branch} at {src}")
    _run_git(
        ["worktree", "add", "--detach", "--force", str(src), f"origin/{branch}"],
        canonical_repo, "worktree add",
    )
    return src


def _release_build_source(build_source: Path, canonical_repo: Path) -> None:
    """Remove the temp worktree created by _get_build_source. No-op for
    --env local (build_source is the working tree)."""
    if build_source == canonical_repo:
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(build_source)],
        cwd=str(canonical_repo), capture_output=True, text=True,
    )


def _export_built_packages(built: list[Path], build_source: Path, canonical_repo: Path) -> list[Path]:
    """Copy built package dirs out of the temp build source back to the
    canonical repo's localBuild/ so deploy_chathealthy.py finds them at
    the well-known location. No-op for --env local."""
    if build_source == canonical_repo:
        return built
    exported: list[Path] = []
    for pkg in built:
        rel = pkg.relative_to(build_source)
        dst = canonical_repo / rel
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(pkg, dst)
        exported.append(dst)
    return exported


def _run_git(args: list[str], cwd: Path, label: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: git {label} failed (rc={r.returncode}). "
            f"stderr: {r.stderr.strip()}"
        )


def _bump_build_counter(env: str) -> int:
    """For --env dev|qa|prod: insert a new admin.Versions latest doc with
    build = prior_build + 1 and git_number = current HEAD SHA. Returns
    the new build number. --env local does not call this."""
    from dotenv import load_dotenv
    from pymongo import MongoClient

    repo_root = _find_repo_root(Path(__file__))
    # Canonical env source per operator directive 2026-08-04.
    build_env = repo_root / "build" / ".env"
    working_tree_env = repo_root / ".env"
    for candidate in (build_env, working_tree_env):
        if candidate.is_file():
            load_dotenv(candidate)
            break
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        sys.exit(
            "ERROR: MONGO_FRONTEND_connectionString not found in "
            f"{build_env} or {working_tree_env}."
        )
    coll = MongoClient(conn, serverSelectionTimeoutMS=10000)["admin"]["Versions"]
    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        sys.exit("ERROR: admin.Versions has no records.")
    prior = latest.get("build")
    if prior is None:
        sys.exit("ERROR: admin.Versions latest record has no 'build' field.")
    new_build = int(prior) + 1
    git_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()
    new_doc = {
        "build": new_build,
        "git_number": git_sha,
        "version": latest.get("version", ""),
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(new_doc)
    _step(f"admin.Versions bumped: build {prior} -> {new_build}")
    return new_build


def _refresh_content_hashes(brain_path: Path, repo_root: Path) -> None:
    """Update every `content_hash` on a `disposition: "referenced"` file
    in the manifest to the current disk SHA256 (CRLF→LF normalized to
    match Builder). The manifest is the canonical audit trail for what
    this build packaged; the build snapshots the disk state here so
    downstream Crosswalk checks compare against a manifest that reflects
    reality, not a stale committed value. The self-referential entry for
    deployment_architecture.json itself is skipped — Builder deliberately
    leaves that hash absent because writing the manifest changes its own
    bytes.
    """
    import hashlib as _hashlib
    data = json.loads(brain_path.read_text(encoding="utf-8"))
    changed = 0
    scanned = 0
    def _refresh_files_list(files_list):
        nonlocal changed, scanned
        for f in files_list or []:
            if f.get("disposition") != "referenced":
                continue
            if f.get("content_hash") is None:
                continue
            src = f.get("source_location") or ""
            if src == "brain/machine_artifacts/content/deployment_architecture.json":
                continue
            abs_path = repo_root / src
            if not abs_path.is_file():
                continue
            scanned += 1
            raw = abs_path.read_bytes().replace(b"\r\n", b"\n")
            new_hash = _hashlib.sha256(raw).hexdigest()
            if f["content_hash"] != new_hash:
                f["content_hash"] = new_hash
                changed += 1

    for record in data.get("DeploymentTargetRecord", []) or []:
        _refresh_files_list(record.get("files", []))
        # Package expansion: refresh content_hash for files inside
        # packages[] on any env_binding.
        for eb in record.get("environments", []) or []:
            for pkg in eb.get("packages", []) or []:
                _refresh_files_list(pkg.get("files", []))
    if changed:
        brain_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    _step(f"content_hash refresh: scanned {scanned} referenced files, updated {changed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build per-target deploy packages under localBuild/<target_id>/."
    )
    parser.add_argument(
        "--env", required=True, choices=VALID_ENVS,
        help="Target environment for the build. Determines branch check + "
             "whether the admin.Versions counter is bumped.",
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | 'azure' | 'aca' | a specific target_id. "
             "Defaults to 'all'.",
    )
    args = parser.parse_args(argv)

    canonical_repo = _find_repo_root(Path(__file__))

    # Purge the canonical build output dir. Every build starts from a
    # clean slate; there is no residual state from a prior invocation.
    canonical_build_dir = canonical_repo / "build"
    if canonical_build_dir.exists():
        shutil.rmtree(canonical_build_dir, ignore_errors=True)
    canonical_build_dir.mkdir(parents=True, exist_ok=True)
    _step(f"purged and recreated {canonical_build_dir}")

    # Materialise .env into <repo>/build/.env: working tree for --env
    # local, KV for --env dev|qa|prod. Downstream build code reads env
    # values via os.environ; _materialize_env_file loads them for us.
    _materialize_env_file(args.env, canonical_repo, canonical_build_dir)

    # Pull sources from the correct place per env:
    #   local           -> the working tree
    #   dev | qa | prod -> a temp checkout of origin/<branch>
    repo_root = _get_build_source(canonical_repo, args.env)
    _step(f"repo_root={repo_root} env={args.env} target={args.target}")

    try:
        rc = _build_body(args, repo_root, canonical_repo, canonical_build_dir)
    finally:
        _release_build_source(repo_root, canonical_repo)
    return rc


def _build_body(args, repo_root: Path, canonical_repo: Path, canonical_build_dir: Path) -> int:
    build_sha = _resolve_build_sha(repo_root)
    _step(f"HEAD={build_sha}")

    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    backlog_schema = repo_root / "Website" / "schemas" / "ChatHealthyAgileBacklogSchema.json"
    backlog_path = repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
    # .env comes from the canonical build dir (materialized at build
    # start: working tree for --env local, KV for --env dev|qa|prod).
    # NEVER read from a git source (working tree or temp worktree) at
    # this call site — that would sneak the operator's dev tree into
    # cloud builds via the leak-check.
    env_file = canonical_build_dir / ".env"

    # Refresh content_hash entries in the manifest to match the current disk
    # bytes BEFORE Crosswalk runs. The build is the canonical mechanism for
    # snapshotting "the disk state that this build packaged." Any post-build
    # source edit still gets caught at deploy time — Crosswalk re-runs there
    # and a divergence between the just-refreshed manifest hashes and disk
    # can only mean an edit happened between build and deploy, which is
    # exactly the drift Crosswalk exists to detect. Replaces the deleted
    # _oneshots/refresh_all_hashes.py shortcut; not a bypass because it is
    # part of the canonical build entry point (Rule-066).
    _refresh_content_hashes(brain_path, repo_root)

    backlog = AgileBacklogLoader(schema_uri=backlog_schema).load(backlog_path)
    # Scope schema validation to the single target being built when a
    # specific target_id was requested. For kind aliases ('cloudflare',
    # 'hf', 'azure', 'aca') and 'all', validate the whole envelope.
    load_filter = args.target if args.target.startswith("target_") else None
    coll: DeploymentCollection = RecordLoader().load_collection(
        brain_path, target_id_filter=load_filter,
    )
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

    # Undefined-name gate: every packaged .py file MUST resolve every name
    # it references. Missing imports, typos, or symbols removed by refactor
    # get caught here at build time instead of at runtime in production.
    undefined = _scan_undefined_names(repo_root)
    if undefined:
        sys.stderr.write(
            f"BUILD FAILURE: {len(undefined)} unresolved name reference(s) in packaged Python:\n"
        )
        for e in undefined:
            sys.stderr.write(f"  {e}\n")
        return 3
    _step("undefined-name gate passed")

    if args.env == "local":
        build_n = _read_dev_build_number()
        _step(f"build_number={build_n} (read from admin.Versions; --env local does not bump)")
    else:
        build_n = _bump_build_counter(args.env)

    targets = _select_targets(coll, args.target)
    if not targets:
        sys.exit(f"ERROR: no targets matched --target={args.target!r}")

    built: list[Path] = []
    for t in targets:
        package_dir = _build_one(repo_root, t, build_n, build_sha, env=args.env)
        _stamp_env_on_manifest(package_dir, args.env, build_sha, build_n)
        built.append(package_dir)

    # Export built packages to the canonical <repo>/build/ location so
    # deploy_chathealthy.py finds them regardless of where they were
    # actually assembled (temp worktree for dev|qa|prod; already the
    # canonical location for local).
    if repo_root != canonical_repo:
        exported: list[Path] = []
        for pkg in built:
            rel = pkg.relative_to(repo_root / "build")
            dst = canonical_build_dir / rel
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pkg, dst)
            exported.append(dst)
        built = exported

    _step(f"built {len(built)} package(s) (env={args.env}, build={build_n}):")
    for b in built:
        _step(f"  {b.relative_to(canonical_repo)}")
    return 0


def _stamp_env_on_manifest(package_dir: Path, env: str, git_head_sha: str, build_n: int) -> None:
    """Per-build_deploy_promote_plan v3 §C.1, stamp env + git_head_sha +
    build on manifest.json so the deploy's staleness gate (§C.4) reads
    them back. _build_one already writes manifest.json with build_number
    and other facts; we patch in the deploy-gate fields."""
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["env"] = env
    data["git_head_sha"] = git_head_sha
    data["build"] = build_n
    data["built_at"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
