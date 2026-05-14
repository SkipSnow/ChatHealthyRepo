"""Entry-point script for the pre-commit gate.

Reads the staged file list from `git diff --cached --name-only`,
instantiates `PreCommitHook` against the repository root (determined
via `git rev-parse --show-toplevel`), runs the gate, and exits with
the gate's returned code.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pre_commit_hook import PreCommitHook


def _git_repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip()).resolve()


def _git_staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [Path(line) for line in lines]


def main() -> int:
    repo_root = _git_repo_root()
    staged = _git_staged_files()
    hook = PreCommitHook(repo_root=repo_root)
    return hook.on_commit(staged)


if __name__ == "__main__":
    sys.exit(main())
