"""Rule-006-ENF-001 git-state gate -- EPIC-008-F-002-S-013-REQ-B-001.

One story-level test for the requirement: every command Claude Code issues that
CHANGES a git repository's state must reach the operator for approval, while a
read of the repository is never governed. Each case below is either the
requirement's core contract or a regression for a false-classification this
gate has fixed:

  - a bare `git config <key>` GET read (was gated as a write);
  - `git -C <path>` and `git -c <k=v>` value options preceding a read subcommand
    (the value was once parsed as the subcommand and a read gated);
  - flag-dependent subcommands (config, stash, remote) in their read form;
  - a `.py` file read is not a script being executed and is not walked/gated;
  - separators and pipes are separate commands, so a read segment stays a read;
  - push / commit / add / fetch pass through -- the commit-msg gate owns them.

The gate is fed a payload identical in shape to the PreToolUse hook's.
"""
from __future__ import annotations

import pytest

from git_state_change_authorization_worker import GitStateChangeAuthorization


def _gated(command: str) -> bool:
    worker = GitStateChangeAuthorization(
        {"tool_name": "Bash", "tool_input": {"command": command}})
    return bool(worker.findings())


# (command, must_gate). must_gate is False for a read or a pass-through the gate
# must let through, and True for a state change that must reach the operator.
CASES = [
    # a read of the repository is never governed
    ("git status", False),
    ("git log -1", False),
    ("git diff", False),
    ("git show HEAD", False),
    ("git rev-parse HEAD", False),
    # config GET is a read (regression: a bare `git config <key>` was gated)
    ("git config core.hooksPath", False),
    ("git config --get user.email", False),
    ("git config --list", False),
    # value options precede a read subcommand (regression: value read as subcmd)
    ("git -C /repo status", False),
    ("git -c core.pager=cat log", False),
    # flag-dependent subcommands in their read form
    ("git stash list", False),
    ("git remote -v", False),
    # a .py file read is not a script being executed
    ("cat foo.py", False),
    # segmentation: separators and pipes are separate commands
    ("echo a; git status; echo b", False),
    ("ls .git/hooks | grep x", False),
    # pass-throughs the commit-msg gate already owns
    ("git push origin dev", False),
    ("git commit -m x", False),
    ("git add .", False),
    ("git fetch", False),
    # state changes: a human must approve each
    ("git config core.hooksPath /tmp/x", True),
    ("git config --unset core.hooksPath", True),
    ("git rm foo", True),
    ("git reset --hard HEAD~1", True),
    ("git checkout -- foo", True),
    ("git tag -a v1 -m msg", True),
    ("git branch -D foo", True),
    ("git merge foo", True),
    ("git rebase main", True),
]


@pytest.mark.parametrize("command, must_gate", CASES)
def test_git_state_change_classification(command: str, must_gate: bool) -> None:
    got = _gated(command)
    assert got is must_gate, (
        f"{command!r}: expected {'GATE' if must_gate else 'allow'}, "
        f"got {'GATE' if got else 'allow'}")
