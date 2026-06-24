"""promote_chathealthy.py — the unified promote entry point.

Advances the substrate by one environment in the promote chain. Does NOT
rebuild. Does NOT have special git privileges — the commit/push it creates
passes through every normal pre-commit and post-commit gate (Rule-008,
Rule-065, etc.) exactly like a hand-authored commit.

CLI:
  python promote_chathealthy.py --from {local|dev|qa} --to {dev|qa|prod}

The (from, to) pair MUST be adjacent in the promote chain:
  local -> dev   atomic full-tree commit + push to the dev branch.
                  Stages every modified + untracked file, commits with a
                  generated message, pushes. Solves the operator's named
                  partial-commit forensic-recovery-point pain.
  dev   -> qa    qa branch overwritten to byte-identical to dev tip via
                  `git reset --hard origin/dev` + `git push --force-with-lease
                  origin qa`. See REQ-B-004 for the overwrite-not-otherwise rule.
  qa    -> prod  main branch overwritten to byte-identical to qa tip via
                  `git reset --hard origin/qa` + `git push --force-with-lease
                  origin main`. See REQ-B-004.

Any non-adjacent pair is rejected per REQ-B-003.

Reference: build_deploy_promote_plan v3 §C.3 (promote responsibilities),
§INV-5 (promote is the only path between envs), §INV-7 (code is authored
only on the local workstation).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_ADJACENT_PAIRS = (("local", "dev"), ("dev", "qa"), ("qa", "prod"))
_ENV_TO_BRANCH = {"dev": "dev", "qa": "qa", "prod": "main"}


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for p in (cur, *cur.parents):
        if (p / ".git").is_dir():
            return p
    raise RuntimeError("repo root not found")


def _run_git(args: list[str], repo_root: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True, check=check,
    )


def _current_branch(repo_root: Path) -> str:
    return _run_git(["symbolic-ref", "--short", "HEAD"], repo_root).stdout.strip()


def _promote_local_to_dev(repo_root: Path, label: str | None = None) -> int:
    """Stage every modified + untracked file under the working tree,
    commit, push to dev. Atomic: no partial commits.

    Optional `label` becomes the commit-message subject line. The
    auto-generated `promote local -> dev (<ts>)` line follows as the body.
    Label survives every downstream branch advance (dev -> qa -> prod)
    because those just move the branch pointer to the same commit.
    """
    branch = _current_branch(repo_root)
    if branch != "dev":
        sys.exit(
            f"ERROR: --from local --to dev requires current branch dev; "
            f"current is {branch!r}. Local source comes from the working "
            f"tree on the dev branch."
        )

    status = _run_git(["status", "--porcelain"], repo_root).stdout.strip()
    if not status:
        print("[promote] nothing to commit; working tree clean")
        return 0

    print("[promote] staging modified + untracked files")
    _run_git(["add", "-A"], repo_root)
    auto = f"promote local -> dev ({datetime.now(timezone.utc).isoformat()})"
    msg = f"{label}\n\n{auto}" if label else auto
    print(f"[promote] commit: {msg}")
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        print("[promote] commit FAILED — check the pre-commit hook output above")
        return result.returncode
    print("[promote] push origin dev")
    result = subprocess.run(["git", "push", "origin", "dev"], cwd=str(repo_root))
    return result.returncode


def _promote_branch_to_branch(repo_root: Path, source_env: str, target_env: str) -> int:
    """Fully automated: fetch, checkout target, reset target to source tip,
    force-push, return to the original branch. The operator does not have
    to switch branches at any point. Implements REQ-B-004 (byte-identical
    overwrite)."""
    source_branch = _ENV_TO_BRANCH[source_env]
    target_branch = _ENV_TO_BRANCH[target_env]
    original_branch = _current_branch(repo_root)

    status = _run_git(["status", "--porcelain"], repo_root).stdout.strip()
    if status:
        sys.exit(
            f"ERROR: working tree is not clean. Commit or stash changes "
            f"on branch {original_branch!r} before promoting.\n{status}"
        )

    print("[promote] fetch origin")
    _run_git(["fetch", "origin"], repo_root)

    if original_branch != target_branch:
        print(f"[promote] checkout {target_branch}")
        result = subprocess.run(
            ["git", "checkout", target_branch],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            print(f"[promote] checkout {target_branch} FAILED")
            return result.returncode

    try:
        print(f"[promote] reset --hard origin/{source_branch} (wipe out any divergence on {target_branch})")
        result = subprocess.run(
            ["git", "reset", "--hard", f"origin/{source_branch}"],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            print(f"[promote] reset FAILED on {target_branch}")
            return result.returncode

        print(f"[promote] push --force-with-lease origin {target_branch}")
        result = subprocess.run(
            ["git", "push", "--force-with-lease", "origin", target_branch],
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            return result.returncode
    finally:
        if original_branch != target_branch:
            print(f"[promote] checkout back to {original_branch}")
            subprocess.run(
                ["git", "checkout", original_branch],
                cwd=str(repo_root),
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Promote the substrate one env forward in the promote chain. "
                    "Does NOT rebuild; runs through the normal pre-commit gates."
    )
    parser.add_argument("--from", dest="from_env", required=True,
                        choices=["local", "dev", "qa"])
    parser.add_argument("--to", dest="to_env", required=True,
                        choices=["dev", "qa", "prod"])
    parser.add_argument("--label", dest="label", default=None,
                        help="Commit message subject for local -> dev promotes. "
                             "Ignored on branch-to-branch promotes (those don't "
                             "create commits; the label rides on the underlying "
                             "commit advanced by the branch pointer move).")
    args = parser.parse_args(argv)

    pair = (args.from_env, args.to_env)
    if pair not in _ADJACENT_PAIRS:
        sys.exit(
            f"ERROR: ({args.from_env} -> {args.to_env}) is not an adjacent pair "
            f"in the promote chain. Valid pairs: {_ADJACENT_PAIRS}."
        )

    repo_root = _repo_root()
    if args.from_env == "local":
        return _promote_local_to_dev(repo_root, label=args.label)
    return _promote_branch_to_branch(repo_root, args.from_env, args.to_env)


if __name__ == "__main__":
    raise SystemExit(main())
