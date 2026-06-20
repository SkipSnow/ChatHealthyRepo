# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""Tests for the two promote-workflow invariants on EPIC-008-F-012-S-003.

Both tests are intentionally hermetic — they do NOT run a real promote.
A test that drives a real promote would risk mutating the project's git
state on a green run and, on a red run, could leave a half-promoted branch
behind. Instead:

  * REQ-B-003 (invariant pair set) is tested by invoking the script with
    every invalid (source, destination) pair and asserting it rejects at
    the input layer, with no fetch / no checkout / no push. The runs
    cannot promote anything because every pair is invalid by definition.

  * REQ-B-004 (byte-identical overwrite, never a merge) is tested by
    statically inspecting the script's source. The implementation must
    contain `git reset --hard origin/<source>` and `git push --force-
    with-lease` and MUST NOT contain `git merge`, `git rebase`, `git
    cherry-pick`, or any docstring / comment that calls the operation
    a "merge" or "fast-forward merge."
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "promote_chathealthy.py"


# ════════════════════════════════════════════════════════════════════
# TEST-1 for EPIC-008-F-012-S-003-REQ-B-003
# Every invalid pair MUST be rejected at the input layer with no git
# side-effect.
# ════════════════════════════════════════════════════════════════════

INVALID_PAIRS = [
    ("local", "qa"),    ("local", "prod"),
    ("dev",   "prod"),  ("dev",   "local"),
    ("qa",    "dev"),   ("qa",    "local"),
    ("prod",  "qa"),    ("prod",  "dev"),  ("prod", "local"),
    ("local", "local"), ("dev",   "dev"),
    ("qa",    "qa"),    ("prod",  "prod"),
]


def _snapshot_refs(repo_root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo_root),
         "for-each-ref", "--format=%(refname) %(objectname)"],
        text=True,
    )


@pytest.mark.parametrize("src,dst", INVALID_PAIRS)
def test_b003_invalid_pair_rejected_with_no_git_sideeffect(src, dst):
    repo_root = HERE.parents[1]
    refs_before = _snapshot_refs(repo_root)

    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--from", src, "--to", dst],
        capture_output=True, text=True,
    )

    assert r.returncode != 0, (
        f"promote {src!r}->{dst!r} should reject but returned 0; "
        f"stdout: {r.stdout[:200]!r} stderr: {r.stderr[:200]!r}"
    )
    combined = (r.stdout + r.stderr).lower()
    assert any(tok in combined for tok in (src, dst, "invalid", "choose", "adjacent")), (
        f"rejection should name the pair or call it invalid/non-adjacent; "
        f"got: {(r.stdout+r.stderr)[:300]!r}"
    )

    refs_after = _snapshot_refs(repo_root)
    assert refs_before == refs_after, (
        f"invalid promote {src!r}->{dst!r} mutated a git ref. "
        f"This is a contract violation; before:\n{refs_before}\nafter:\n{refs_after}"
    )


# ════════════════════════════════════════════════════════════════════
# TEST-1 for EPIC-008-F-012-S-003-REQ-B-004
# Static inspection: the script MUST do reset+force-push and MUST NOT
# do merge/rebase/cherry-pick. Docstring/comments MUST NOT describe
# the operation as a merge.
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def script_source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_b004_uses_reset_hard_and_force_with_lease(script_source):
    assert re.search(r'["\']reset["\']\s*,\s*["\']--hard["\']\s*,', script_source), (
        "promote_chathealthy.py MUST use `git reset --hard` for the destination overwrite"
    )
    assert re.search(r'["\']push["\']\s*,\s*["\']--force-with-lease["\']', script_source), (
        "promote_chathealthy.py MUST push with `--force-with-lease`"
    )


def test_b004_forbids_merge_rebase_cherrypick_verbs(script_source):
    for forbidden_verb in ("merge", "rebase", "cherry-pick"):
        bad = re.search(rf'["\']{re.escape(forbidden_verb)}["\']', script_source)
        assert not bad, (
            f"promote_chathealthy.py MUST NOT invoke `git {forbidden_verb}`; "
            f"REQ-B-004 forbids merge/rebase/cherry-pick semantics in the promote workflow"
        )


def test_b004_no_merge_language_in_docstring_or_comments(script_source):
    # Find every line that is part of a docstring or a # comment.
    lines = script_source.splitlines()
    in_triple = False
    flagged: list[str] = []
    for ln in lines:
        if '"""' in ln:
            in_triple = not in_triple if ln.count('"""') % 2 == 1 else in_triple
            line_is_doc = True
        else:
            line_is_doc = in_triple
        line_is_comment = ln.lstrip().startswith("#")
        if not (line_is_doc or line_is_comment):
            continue
        if re.search(r"\bfast[- ]forward merge\b", ln, flags=re.IGNORECASE):
            flagged.append(f"  [fast-forward merge] {ln.strip()}")
            continue
        if re.search(r"\bmerge\b", ln, flags=re.IGNORECASE):
            flagged.append(f"  [merge] {ln.strip()}")
    assert not flagged, (
        "promote_chathealthy.py docstrings/comments MUST NOT describe the "
        "operation as a merge per REQ-B-004; offending lines:\n"
        + "\n".join(flagged)
    )
