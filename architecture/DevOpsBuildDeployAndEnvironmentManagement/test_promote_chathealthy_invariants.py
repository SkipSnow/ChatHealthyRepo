# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""Tests for the two promote-workflow invariants on EPIC-008-F-012.

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
    push with `--force-with-lease` and MUST NOT contain `git merge`,
    `git rebase`, `git cherry-pick`, `git pull`, or any docstring /
    comment that calls the operation a "merge" or "fast-forward merge."

Inspection reads the parsed module — argument vectors, string constants,
comment tokens and docstrings — rather than the characters of the file,
so the word "merge" inside prose is judged as prose and a git verb is
judged as a git verb.
"""
from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "promote_chathealthy.py"


def _argv_lists(source: str) -> list[list[str | None]]:
    """Every list or tuple literal, as its string constants in position.

    A git invocation is written as an argument vector, so the vectors are
    what the invariants are about — not the characters of the file. An
    element that is not a literal string (a variable holding a refspec,
    say) is kept as None so the positions of its neighbours stay true.
    """
    out: list[list[str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
            continue
        parts: list[str | None] = [
            e.value if isinstance(e, ast.Constant) and isinstance(e.value, str)
            else None
            for e in node.elts
        ]
        if any(p is not None for p in parts):
            out.append(parts)
    return out


def _adjacent(vectors: list[list[str | None]], first: str, second: str) -> bool:
    return any(a == first and b == second
               for v in vectors for a, b in zip(v, v[1:]))


def _string_constants(source: str) -> set[str]:
    return {n.value for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def _prose_lines(source: str) -> list[str]:
    """Every comment and every docstring line, tokenized rather than guessed."""
    out: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        doc = ast.get_docstring(node) if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                   ast.AsyncFunctionDef)) else None
        if doc:
            out.extend(doc.splitlines())
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            out.append(tok.string)
    return out


def _words(line: str) -> list[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in line.lower())
    return cleaned.split()


# ════════════════════════════════════════════════════════════════════
# TEST-1 for EPIC-008-F-012
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
# TEST-1 for EPIC-008-F-012
# Static inspection: the script MUST do reset+force-push and MUST NOT
# do merge/rebase/cherry-pick. Docstring/comments MUST NOT describe
# the operation as a merge.
# ════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def script_source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_b004_overwrites_destination_with_force_with_lease(script_source):
    vectors = _argv_lists(script_source)
    assert _adjacent(vectors, "push", "--force-with-lease"), (
        "promote_chathealthy.py MUST push with `--force-with-lease` — the "
        "destination is overwritten with the source tip, never reconciled with it"
    )
    assert not any("pull" in v for v in vectors), (
        "promote_chathealthy.py MUST NOT use `git pull`; a pull reconciles two "
        "histories, and REQ-B-004 requires the destination be overwritten"
    )


def test_b004_forbids_merge_rebase_cherrypick_verbs(script_source):
    constants = _string_constants(script_source)
    for forbidden_verb in ("merge", "rebase", "cherry-pick"):
        assert forbidden_verb not in constants, (
            f"promote_chathealthy.py MUST NOT invoke `git {forbidden_verb}`; "
            f"REQ-B-004 forbids merge/rebase/cherry-pick semantics in the promote workflow"
        )


def test_b004_no_merge_language_in_docstring_or_comments(script_source):
    flagged: list[str] = []
    for ln in _prose_lines(script_source):
        words = _words(ln)
        pairs = list(zip(words, words[1:]))
        if ("fast", "forward") in pairs and "merge" in words:
            flagged.append(f"  [fast-forward merge] {ln.strip()}")
            continue
        if "merge" in words:
            flagged.append(f"  [merge] {ln.strip()}")
    assert not flagged, (
        "promote_chathealthy.py docstrings/comments MUST NOT describe the "
        "operation as a merge per REQ-B-004; offending lines:\n"
        + "\n".join(flagged)
    )
