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
import os
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


def _run_pipeline_test_gate(repo_root: Path) -> int:
    """LLD §9.5 — unit + regression gate before local -> dev promote."""
    if os.environ.get("SKIP_PIPELINE_TESTS", "").lower() in ("1", "true", "yes"):
        print("[promote] SKIP_PIPELINE_TESTS set — skipping pipeline test gate")
        return 0
    script = repo_root / "pipeline" / "Code" / "run_pipeline_tests.py"
    if not script.is_file():
        print(f"[promote] pipeline test gate skipped — {script} not found")
        return 0
    print("[promote] running LLD §9.5 pipeline test gate (unit + regression)")
    return subprocess.call([sys.executable, str(script)], cwd=str(script.parent))


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

    gate_rc = _run_pipeline_test_gate(repo_root)
    if gate_rc != 0:
        print(f"[promote] pipeline test gate FAILED (exit {gate_rc}) — promote aborted")
        return gate_rc

    status = _run_git(["status", "--porcelain"], repo_root).stdout.strip()
    print("[promote] staging modified + untracked files"
          if status else "[promote] working tree clean; will produce empty commit")
    _run_git(["add", "-A"], repo_root)
    auto = f"promote local -> dev ({datetime.now(timezone.utc).isoformat()})"
    msg = f"{label}\n\n{auto}" if label else auto
    print(f"[promote] commit: {msg}")
    # --allow-empty so an operator invocation with no changes STILL
    # produces a commit; that commit triggers the pre-commit hook and
    # Rule-065 gates the promote invocation itself. Without this, a
    # no-op promote silently bypasses every governed gate.
    result = subprocess.run(
        ["git", "commit", "--allow-empty", "-m", msg],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        print("[promote] commit FAILED — check the pre-commit hook output above")
        return result.returncode
    print("[promote] push origin dev")
    result = subprocess.run(["git", "push", "origin", "dev"], cwd=str(repo_root))
    if result.returncode != 0:
        return result.returncode

    # Sync the working-tree .env up to KV so every cloud build/deploy
    # after this promote uses today's canonical secret values. The KV
    # secret is the source of truth for all cloud envs (operator
    # directive 2026-08-04); a local -> dev promote MUST push the
    # operator's current .env or dev will still be running with stale
    # secrets even though the code was just promoted.
    return _sync_env_to_kv(repo_root)


def _sync_env_to_kv(repo_root: Path) -> int:
    """Upload <repo>/.env to kv-chpipeline-dev/env-file using the
    project's gz:<base64> encoding. Returns 0 on success, non-zero on
    failure."""
    import base64, gzip, os, shutil, tempfile
    env_src = repo_root / ".env"
    if not env_src.is_file():
        print(f"[promote] WARNING: {env_src} not found; skipping KV sync")
        return 0
    plaintext = env_src.read_bytes()
    payload = "gz:" + base64.b64encode(gzip.compress(plaintext)).decode("ascii")
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(payload)
        az = shutil.which("az") or "az"
        r = subprocess.run(
            [az, "keyvault", "secret", "set",
             "--vault-name", "kv-chpipeline-dev", "--name", "env-file",
             "--file", path, "-o", "tsv", "--query", "id"],
            capture_output=True, text=True, shell=False,
        )
        if r.returncode != 0:
            print(f"[promote] KV sync FAILED: {r.stderr.strip()[:300]}")
            return r.returncode
        print(f"[promote] KV sync OK: kv-chpipeline-dev/env-file "
              f"({len(plaintext)} plaintext bytes)")
        return 0
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _promote_branch_to_branch(repo_root: Path, source_env: str, target_env: str) -> int:
    """Fully automated: fetch, then push origin/<source> to <target> on
    the remote in a single force-with-lease. Does NOT touch the local
    working tree or switch branches — dirty local files (from unrelated
    work) do not block a promote because the promote is entirely a
    remote-branch pointer move. Implements REQ-B-004 (byte-identical
    overwrite)."""
    source_branch = _ENV_TO_BRANCH[source_env]
    target_branch = _ENV_TO_BRANCH[target_env]

    print("[promote] fetch origin")
    _run_git(["fetch", "origin"], repo_root)

    # Push origin/<source> tip to remote's <target> branch without a
    # local checkout. Refspec form `<src>:refs/heads/<dst>` moves the
    # remote branch pointer directly; the local working tree is never
    # read or modified. --force-with-lease matches the prior semantics
    # (byte-identical overwrite of target with source's tip, guarded
    # against concurrent-writer surprises).
    refspec = f"refs/remotes/origin/{source_branch}:refs/heads/{target_branch}"
    print(f"[promote] push --force-with-lease origin {refspec}")
    result = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", refspec],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        print(f"[promote] push FAILED — origin {target_branch} unchanged")
        return result.returncode

    # Refresh the local remote-tracking ref for the target so subsequent
    # `git log origin/<target>` shows the new tip immediately.
    _run_git(["fetch", "origin", target_branch], repo_root)
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
