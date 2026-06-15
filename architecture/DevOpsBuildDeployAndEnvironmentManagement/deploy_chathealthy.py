"""deploy_chathealthy.py — the unified deploy entry point.

Replaces the legacy local_deploy.py / remote_deploy.py pair. One script,
one CLI surface, internally dispatches by GITHUB_ACTIONS context and --env.

CLI:
  python deploy_chathealthy.py --env {local|dev|qa|prod} [--target <list>] [--tests <test_list>]

Behaviour by --env:
  local        — runs LocalDeploy (today's local stack stand-up: Docker
                 containers + host-OS Website wrapper).
  dev|qa|prod  — ships per-target packages from localBuild/<target_id>/
                 to cloud destinations (Cloudflare Pages, HuggingFace Spaces,
                 Azure Function App, etc.).

Three-check staleness gate (per build_deploy_promote_plan v3 §C.4) runs
BEFORE any handler is dispatched. Any failed check rejects the deploy
with a fix-it message naming the stale fact:
  (a) git_head_sha  — manifest's git_head_sha must equal current HEAD short SHA
                      [non-local envs only; local builds source from working
                      tree, not HEAD, so SHA drift does not invalidate]
  (b) env           — manifest.env must equal --env
  (c) build counter — manifest.build must equal admin.Versions.latest.build
                      [non-local envs only; --env local does not bump and
                      the counter relationship is not load-bearing]

Reference: build_deploy_promote_plan v3 §C.2 (deploy responsibilities),
§C.4 (staleness gate), §C.7 (push-outcome reporting), §C.8 (smoke policy),
§INV-4 (env-mismatch rejection).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _deploy_chain import (  # noqa: E402
    LocalStandUp,
    require_local_context,
    run_cloud_deploy,
    BUILD_ROOT_REL,
)


VALID_ENVS = ("local", "dev", "qa", "prod")
_ENV_BRANCH = {"local": "dev", "dev": "dev", "qa": "qa", "prod": "main"}


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for p in (cur, *cur.parents):
        if (p / ".git").is_dir() or (p / "Code" / ".env").is_file():
            return p
    raise RuntimeError("repo root not found")


def _current_branch(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _enforce_env_branch_check(repo_root: Path, env: str) -> None:
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


def _current_head_sha(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _latest_admin_build() -> int | None:
    from dotenv import load_dotenv
    from pymongo import MongoClient

    load_dotenv(_repo_root() / "Code" / ".env")
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        return None
    try:
        latest = MongoClient(conn, serverSelectionTimeoutMS=10000)["admin"]["Versions"].find_one(
            sort=[("from", -1)]
        )
    except Exception:
        return None
    if latest is None:
        return None
    return latest.get("build")


def _staleness_gate(repo_root: Path, env: str, target_ids: list[str]) -> None:
    """Three-check staleness gate per plan v3 §C.4. Fails fast on any
    target's manifest mismatch. For --env local, skip checks (a) and (c)
    per §INV-1 (working-tree source)."""
    head_sha = _current_head_sha(repo_root) if env != "local" else None
    latest_build = _latest_admin_build() if env != "local" else None

    for target_id in target_ids:
        manifest_path = repo_root / BUILD_ROOT_REL / target_id / "manifest.json"
        if not manifest_path.is_file():
            sys.exit(
                f"ERROR: no fresh build for {target_id} env {env}; "
                f"run build_chathealthy.py --env {env} first"
            )
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        manifest_env = data.get("env")
        if manifest_env != env:
            sys.exit(
                f"ERROR: build was for env {manifest_env!r}, deploy "
                f"requested {env!r} (target={target_id}); rebuild for {env}"
            )

        if env == "local":
            continue

        manifest_sha = data.get("git_head_sha")
        if manifest_sha and manifest_sha != head_sha:
            sys.exit(
                f"ERROR: build at {manifest_sha} does not match current "
                f"checkout {head_sha} (target={target_id}); rebuild with "
                f"build_chathealthy.py --env {env}"
            )

        if latest_build is not None:
            manifest_build = data.get("build")
            if manifest_build is not None and int(manifest_build) != int(latest_build):
                sys.exit(
                    f"ERROR: build_number {manifest_build} is older than "
                    f"admin.Versions latest {latest_build} for env {env} "
                    f"(target={target_id}); rebuild"
                )


def _collect_target_ids_for_env(repo_root: Path, env: str, target_arg: str) -> list[str]:
    """Mirror local_deploy._select_target_ids logic minus the env filter.
    For staleness-gate purposes we need the set of target_ids the deploy
    is going to touch."""
    from target_record import DeploymentCollection
    from record_loader import RecordLoader
    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    from _deploy_chain import select_target_ids
    selected = select_target_ids(coll, target_arg)
    out = []
    for tid, _kind in selected:
        target = coll.by_target_id(tid)
        if env in target.env_binding_set():
            out.append(tid)
    return out


def _run_tests(env: str, tests: list[str]) -> int:
    """Run the named tests after the deploy completes. SMOKE_TEST_ENV is
    set so the test modules pick up the right URL set."""
    if not tests:
        return 0
    test_map = {
        "find_care_smoke": "architecture/DevOpsBuildDeployAndEnvironmentManagement/find_care_smoke_test.py",
        "ur_um_regression": "architecture/DevOpsBuildDeployAndEnvironmentManagement/findcare_ur_um_regression_test.py",
    }
    test_paths = []
    for name in tests:
        path = test_map.get(name)
        if not path:
            print(f"WARN: unknown test {name!r}; skipping")
            continue
        test_paths.append(path)
    if not test_paths:
        return 0
    cmd = ["python", "-m", "pytest"] + test_paths + ["-v"]
    env_dict = dict(os.environ)
    env_dict["SMOKE_TEST_ENV"] = env
    print(f"[deploy] running tests: {tests} against env={env}")
    return subprocess.run(cmd, env=env_dict).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy per-target packages: --env local stands up the local "
                    "host stack; --env dev|qa|prod ships cloud targets."
    )
    parser.add_argument("--env", required=True, choices=VALID_ENVS)
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | 'azure' | 'aca' | a specific target_id. "
             "Defaults to 'all'.",
    )
    parser.add_argument(
        "--tests", default="",
        help="Comma-separated list of test names to run after deploy "
             "(e.g. 'find_care_smoke,ur_um_regression'). Empty by default.",
    )
    args = parser.parse_args(argv)
    require_local_context()
    repo_root = _repo_root()

    _enforce_env_branch_check(repo_root, args.env)

    if args.env == "local":
        # LocalStandUp owns the local stack lifecycle (Docker containers
        # + host-OS Website wrapper). It does not consume per-target
        # manifests; deployment_architecture.json's HF Space targets are
        # cloud-bound and not env_binding=local. Skip the per-target
        # staleness gate for local.
        rc = LocalStandUp().run()
    else:
        target_ids = _collect_target_ids_for_env(repo_root, args.env, args.target)
        if not target_ids:
            sys.exit(f"ERROR: no targets matched --env={args.env} --target={args.target!r}")
        _staleness_gate(repo_root, args.env, target_ids)
        rc = run_cloud_deploy(args.env, args.target)

    if rc == 0:
        tests = [t.strip() for t in args.tests.split(",") if t.strip()]
        tests_rc = _run_tests(args.env, tests)
        if tests_rc != 0:
            return tests_rc

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
