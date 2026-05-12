"""bootstrap_repo_secrets — idempotent mirror of selected Code/.env keys
to GitHub Actions repo secrets so the deploy workflows can push them
on to the HuggingFace Space.

Why this script exists:
  The HF Space backends run pydantic-ai's gemini model (and other
  vendor APIs) that read API keys from environment. The local container
  loads them from Code/.env; the HF Space pulls them via the workflow's
  set_secret helper, which reads ${{ secrets.<NAME> }} on GitHub. If a
  key is missing from repo secrets, the corresponding workflow step
  fails loud — but only AFTER a push, costing a wasted deploy cycle.
  This script provisions the secrets up front from the authoritative
  Code/.env, so a fresh-clone of the repo gets a one-command bootstrap.

Usage:
  python Code/deploy/bootstrap_repo_secrets.py              # apply
  python Code/deploy/bootstrap_repo_secrets.py --dry-run    # report only

Behavior:
  - Reads Code/.env via dotenv (same parser local_deploy.py uses).
  - For each mapping in MIRROR_KEYS: pushes the .env value to the GH
    repo secret with the listed name via `gh secret set`.
  - Fails LOUD (exit 1) if .env is missing, if a required key is unset
    in .env, or if `gh` is not authenticated for this repo.
  - Idempotent: GitHub overwrites; no read-back diff because secrets
    are write-only by design.
"""
from __future__ import annotations
import argparse
import os
import subprocess
import sys
from pathlib import Path

# .env key  ->  GH repo secret name. Keep this list explicit; don't
# bulk-mirror — secrets that leak to GH are visible to anyone with
# write access to the repo.
MIRROR_KEYS: list[tuple[str, str]] = [
    ("GEMINI_API_KEY", "GEMINI_API_KEY"),
]


def _env_file() -> Path:
    here = Path(__file__).resolve().parent
    repo_root = here.parent.parent
    return repo_root / "Code" / ".env"


def _check_gh_auth() -> None:
    r = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(
            "ERROR: gh CLI is not authenticated. Run `gh auth login` first.\n"
            f"gh said: {r.stderr.strip()[:400]}"
        )
    r = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("ERROR: gh cannot view the current repo. Are you inside the repo root?")


def _list_existing_secrets() -> set[str]:
    r = subprocess.run(["gh", "secret", "list"], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ERROR: gh secret list failed: {r.stderr.strip()[:400]}")
    names = set()
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        names.add(line.split()[0])
    return names


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would change; do not call gh secret set.")
    args = p.parse_args()

    env_path = _env_file()
    if not env_path.is_file():
        sys.exit(f"ERROR: env file missing at {env_path}. Cannot bootstrap secrets.")

    try:
        from dotenv import dotenv_values
    except ImportError:
        sys.exit("ERROR: python-dotenv not installed. `pip install python-dotenv`.")

    env_dict = dotenv_values(env_path)
    _check_gh_auth()
    existing = _list_existing_secrets()

    print(f"Mirror plan ({'dry-run' if args.dry_run else 'apply'}): "
          f"{len(MIRROR_KEYS)} key(s) from {env_path}")

    rc = 0
    for env_key, gh_name in MIRROR_KEYS:
        value = env_dict.get(env_key)
        if value is None or value == "":
            print(f"  FAIL  {env_key} -> {gh_name}: missing/empty in Code/.env")
            rc = 1
            continue
        state = "exists (overwrite)" if gh_name in existing else "creating"
        if args.dry_run:
            print(f"  PLAN  {env_key} -> {gh_name}: {state}, value-bytes={len(value)}")
            continue
        # gh secret set reads value from stdin to avoid argv-leakage.
        push = subprocess.run(
            ["gh", "secret", "set", gh_name],
            input=value, text=True, capture_output=True,
        )
        if push.returncode != 0:
            print(f"  FAIL  {env_key} -> {gh_name}: {push.stderr.strip()[:200]}")
            rc = 1
        else:
            print(f"  OK    {env_key} -> {gh_name}: {state}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
