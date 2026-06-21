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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Per-target_kind handlers, helper utilities, crosswalk gate — all
# imported from local_build.py for now. They migrate in step 5.7.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _build_chain import (  # noqa: E402
    _build_one,
    _find_repo_root,
    _read_dev_build_number,
    _require_local_context,
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


def _current_branch(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _enforce_env_branch_check(repo_root: Path, env: str) -> None:
    """INV-2: --env dev|qa|prod require the matching branch. --env local
    has no branch requirement (INV-1: working-tree source)."""
    if env == "local":
        return
    expected = _ENV_BRANCH[env]
    actual = _current_branch(repo_root)
    if actual != expected:
        sys.exit(
            f"ERROR: --env {env} requires branch {expected}, but current "
            f"branch is {actual}; check out the right branch or use the "
            f"promote workflow"
        )


def _bump_build_counter(env: str) -> int:
    """For --env dev|qa|prod: insert a new admin.Versions latest doc with
    build = prior_build + 1 and git_number = current HEAD SHA. Returns
    the new build number. --env local does not call this."""
    from dotenv import load_dotenv
    from pymongo import MongoClient

    repo_root = _find_repo_root(Path(__file__))
    load_dotenv(repo_root / "Code" / ".env")
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        sys.exit("ERROR: MONGO_FRONTEND_connectionString not set in env or Code/.env")
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

    _require_local_context()
    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} env={args.env} target={args.target}")

    _enforce_env_branch_check(repo_root, args.env)

    build_sha = _resolve_build_sha(repo_root)
    _step(f"HEAD={build_sha}")

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

    _step(f"built {len(built)} package(s) (env={args.env}, build={build_n}):")
    for b in built:
        _step(f"  {b.relative_to(repo_root)}")
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
