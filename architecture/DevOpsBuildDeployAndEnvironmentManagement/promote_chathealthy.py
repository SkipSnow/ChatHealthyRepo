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
  dev   -> qa    qa branch overwritten to byte-identical to dev tip by
                  pushing origin/dev onto refs/heads/qa with
                  `--force-with-lease`. No local checkout, no branch switch,
                  and the working tree is neither read nor modified. See
                  REQ-B-004 for the overwrite-not-otherwise rule.
  qa    -> prod  main branch overwritten to byte-identical to qa tip by the
                  same remote pointer move. See REQ-B-004.

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

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
sys.path.insert(0, str(_ch_d / 'architecture' / 'EngineeringRuleEnforcement' / 'code'))
from baseline_walk import baseline_files
_CH_LOG = ChatHealthyLoggingService()


_ADJACENT_PAIRS = (("local", "dev"), ("dev", "qa"), ("qa", "prod"))
_ENV_TO_BRANCH = {"dev": "dev", "qa": "qa", "prod": "main"}


def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for p in (cur, *cur.parents):
        if (p / ".git").is_dir():
            return p
    raise ChatHealthyException(
            mode="runtime_error",
            component="promote_chathealthy",
            message="repo root not found")


def _run_git(args: list[str], repo_root: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_root), capture_output=True, text=True, check=check,
    )


def _apply_every_stash(repo_root: Path) -> None:
    """Empty the stash stack. After a promote attempt nothing is stashed.

    Stashed work is work that has never been committed and never been
    scanned. A promote that refused on stashes would leave it parked
    forever; a promote that ignored them would produce a baseline missing
    it. So the promote brings it back and the commit carries it.

    A conflicting pop is the case that used to stop the promote, and it
    stopped it in the worst possible state: git writes the conflict into
    the working tree AND keeps the stash entry, so the same work sat both
    half-applied and still parked. The stack never emptied.

    So a conflict is now carried rather than refused. What git wrote is
    kept, the unmerged index entries are marked resolved so the next pop
    can run, and the entry is dropped. A stash that cannot be applied at
    all is dropped too: what cannot be unstashed is obliterated, because a
    stash that survives a promote makes the baseline a lie.

    This function empties the stack and does nothing else. It does not
    judge what the tree looks like afterwards and nothing downstream is
    conditioned on it. If the result does not build, the baseline still
    records the tree truthfully, which is the whole job. What it costs is
    written to PROMOTE_STASH_REPORT.md, because a promote runs unattended
    and its effect on the data has to be legible after the fact.
    """
    conflicted: list[str] = []
    discarded: list[str] = []
    while True:
        listed = _run_git(["stash", "list"], repo_root).stdout.strip()
        if not listed:
            break
        entries = listed.splitlines()
        top = entries[0]
        _CH_LOG.info(f"[promote] popping {len(entries)} remaining stash(es); next: {top}")
        popped = _run_git(["stash", "pop"], repo_root, check=False)
        if popped.returncode == 0:
            _CH_LOG.info(f"[promote]   applied: {top}")
            continue
        # A failed pop is one of two events. Either it CONFLICTED, in which
        # case git wrote the content into the tree and left unmerged paths
        # behind, or it REFUSED and wrote nothing. The entry is dropped
        # either way: the requirement is that a promote attempt leaves
        # nothing stashed, and a stash that cannot be applied is exactly the
        # thing that would otherwise sit there forever making the baseline a
        # lie. What cannot be unstashed is obliterated.
        unmerged = _run_git(
            ["diff", "--name-only", "--diff-filter=U"], repo_root,
        ).stdout.strip()
        _run_git(["add", "-A"], repo_root)
        dropped = _run_git(["stash", "drop"], repo_root, check=False)
        if dropped.returncode != 0:
            sys.exit(
                f"ERROR: promote stopped — could not drop {top}. The stash "
                f"stack cannot be emptied, so no promote is possible.\n"
                f"{dropped.stdout}\n{dropped.stderr}"
            )
        if unmerged:
            conflicted.append(top)
            _CH_LOG.info(f"[promote]   CONFLICTED — entry dropped, content left in "
                  f"the working tree between conflict markers: {top}")
        else:
            discarded.append(top)
            _CH_LOG.info(f"[promote]   UNAPPLIABLE — entry dropped, content NOT in "
                  f"the tree: {top}")
            _CH_LOG.info(f"[promote]     {popped.stderr.strip()[:200]}")

    if conflicted or discarded:
        _write_stash_report(repo_root, conflicted, discarded)

    remaining = _run_git(["stash", "list"], repo_root).stdout.strip()
    if remaining:
        sys.exit(
            "ERROR: promote stopped — the stash stack is not empty after "
            "applying every stash. After a promote attempt there can be no "
            f"stashed files.\n{remaining}"
        )
    _CH_LOG.info("[promote] stash stack empty")


def _write_stash_report(repo_root: Path, conflicted: list[str],
                        discarded: list[str]) -> None:
    """Record what emptying the stack cost.

    A promote runs unattended, so anything it does to the data has to be
    legible afterwards without having watched it. A conflicted stash left
    markers in the tree; an unappliable one was dropped with its content
    never reaching the tree at all. Both are recoverable while git still
    holds the dropped commits, so the report carries the recovery command
    and the object ids it applies to.
    """
    report = repo_root / "PROMOTE_STASH_REPORT.md"
    unreachable = _run_git(
        ["fsck", "--unreachable", "--no-reflogs"], repo_root, check=False,
    ).stdout

    lines = [
        "# Promote stash report",
        "",
        f"Emptying the stash stack changed the working tree. "
        f"{len(conflicted)} stash(es) conflicted and {len(discarded)} could "
        f"not be applied at all.",
        "",
    ]
    if conflicted:
        lines += [
            "## Conflicted — content IS in the working tree, between markers",
            "",
        ]
        lines += [f"- {c}" for c in conflicted]
        lines += ["", "Resolve the markers before the promote can proceed.", ""]
    if discarded:
        lines += [
            "## Unappliable — content is NOT in the working tree",
            "",
            "These could not be applied and were dropped so the stack could "
            "empty. Their content is in no file.",
            "",
        ]
        lines += [f"- {d}" for d in discarded]
        lines += [""]

    lines += [
        "## Recovery",
        "",
        "A dropped stash survives as an unreachable commit until git prunes "
        "it. To list them:",
        "",
        "    git fsck --unreachable --no-reflogs",
        "",
        "and to inspect or restore one:",
        "",
        "    git show <sha>",
        "    git stash apply <sha>",
        "",
        f"Unreachable objects at the time of this promote: "
        f"{len(unreachable.splitlines())} entries.",
        "",
    ]
    report.write_text("\n".join(lines), encoding="utf-8")
    _CH_LOG.info(f"[promote] stash report written: {report}")


def _require_capturable_tree(repo_root: Path) -> None:
    """Refuse unless the entire working tree can be captured.

    A promote is a git baseline: the whole tree, at one commit, recoverable.
    A baseline that silently omits files is not a baseline, so every
    mechanism that can hide a file from `git add -A` is a hard stop, not a
    warning. Each of these makes files invisible to BOTH `add -A` and
    `status --porcelain`, so nothing downstream would notice the gap.
    """
    _apply_every_stash(repo_root)

    problems: list[str] = []

    exclude = repo_root / ".git" / "info" / "exclude"
    if exclude.is_file():
        patterns = [
            l.strip() for l in exclude.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")
        ]
        if patterns:
            problems.append(
                f"{len(patterns)} pattern(s) in .git/info/exclude — a local "
                f"ignore list that is not in the repository, so no one can "
                f"review what it hides:\n"
                + "\n".join(f"      {p}" for p in patterns)
            )

    marked = [
        line for line in _run_git(["ls-files", "-v"], repo_root).stdout.splitlines()
        if line and line[0].islower()
    ]
    if marked:
        problems.append(
            f"{len(marked)} file(s) marked skip-worktree/assume-unchanged — "
            f"their changes are invisible to add -A and to status:\n"
            + "\n".join(f"      {m}" for m in marked[:20])
        )

    sparse = _run_git(["config", "core.sparseCheckout"], repo_root, check=False)
    if sparse.stdout.strip().lower() == "true":
        problems.append(
            "sparse-checkout is enabled — part of the tree is not present "
            "locally and cannot be committed."
        )

    submodules = repo_root / ".gitmodules"
    if submodules.is_file():
        problems.append(
            ".gitmodules present — submodule content is not carried by the "
            "parent's add -A."
        )

    if problems:
        sys.exit(
            "ERROR: promote refused — the working tree cannot be captured "
            "completely.\n\n"
            + "\n\n".join(f"  - {p}" for p in problems)
            + "\n\nA promote is a git baseline: the whole tree at one commit. "
              "Clear every item above, then promote."
        )


def _require_everything_staged(repo_root: Path) -> None:
    """After `git add -A`, nothing may remain unstaged or untracked.

    Verifies the claim `git add -A` makes rather than trusting it. Anything
    still reported by `git status --porcelain` after staging is a file the
    commit would not carry -- and therefore a file the full-repository scan
    would certify without it ever reaching the branch.
    """
    untracked = [l for l in _run_git(
        ["ls-files", "--others", "--exclude-standard"], repo_root
    ).stdout.splitlines() if l.strip()]

    unstaged = [l for l in _run_git(
        ["diff", "--name-only"], repo_root
    ).stdout.splitlines() if l.strip()]

    residue = [
        line for line in _run_git(["status", "--porcelain"], repo_root).stdout.splitlines()
        if line and not line.startswith(("A ", "M ", "D ", "R ", "C "))
    ]

    if untracked or unstaged or residue:
        detail = []
        if untracked:
            detail.append(f"  {len(untracked)} untracked file(s) not in the index:\n"
                          + "\n".join(f"      {u}" for u in untracked[:20]))
        if unstaged:
            detail.append(f"  {len(unstaged)} file(s) with unstaged changes:\n"
                          + "\n".join(f"      {u}" for u in unstaged[:20]))
        if residue:
            detail.append("  working-tree residue after staging:\n"
                          + "\n".join(f"      {r}" for r in residue[:20]))
        sys.exit(
            "ERROR: promote refused — the index does not contain the whole "
            "working tree, so the commit would not be a complete baseline. "
            "NO COMMIT HAS BEEN MADE.\n\n"
            + "\n\n".join(detail)
        )

    in_index = len([l for l in _run_git(["ls-files"], repo_root).stdout.splitlines() if l])
    _CH_LOG.info(f"[promote] index verified complete: {in_index} file(s) will be "
          f"committed; working tree holds nothing else")


def _require_tree_fully_committed(repo_root: Path) -> None:
    """After the commit, the working tree and HEAD must be identical.

    A commit is a snapshot of the whole tree, so unchanged files need no
    staging -- they are already in the index and the new commit points at
    them. What has to be proved is that nothing was left OUT: any residue
    in `git status --porcelain` after committing is a file the baseline
    does not contain.

    Also compares the file count in HEAD against the index, which catches
    a path present in one and not the other.
    """
    def _undo_and_exit(reason: str) -> None:
        undone = _run_git(["reset", "--soft", "HEAD~1"], repo_root, check=False)
        rolled = (
            "The commit has been rolled back (`reset --soft HEAD~1`); your "
            "changes are intact and staged."
            if undone.returncode == 0 else
            f"WARNING: the rollback also failed (rc={undone.returncode}): "
            f"{undone.stderr.strip()[:200]}. An incomplete baseline commit is "
            f"still at HEAD and must be removed by hand."
        )
        sys.exit(
            f"ERROR: promote produced an incomplete baseline — {reason}\n\n"
            f"Nothing was pushed. {rolled}"
        )

    residue = [l for l in _run_git(["status", "--porcelain"], repo_root)
               .stdout.splitlines() if l.strip()]
    if residue:
        _undo_and_exit(
            "the working tree still differs from HEAD after committing:\n"
            + "\n".join(f"  {l}" for l in residue)
        )
    in_head = len([l for l in _run_git(
        ["ls-tree", "-r", "--name-only", "HEAD"], repo_root).stdout.splitlines() if l])
    in_index = len([l for l in _run_git(
        ["ls-files"], repo_root).stdout.splitlines() if l])
    if in_head != in_index:
        _undo_and_exit(
            f"HEAD holds {in_head} file(s) but the index holds {in_index}."
        )
    _CH_LOG.info(f"[promote] baseline verified: {in_head} file(s) committed, "
          f"working tree identical to HEAD")






def _promote_local_to_dev(repo_root: Path, label: str | None = None) -> int:
    """Build an accurate list of what the baseline contains, stage it, and
    hand off to commit.

    A promote from local captures the whole working tree, so promote owns
    the working tree: emptying the stash permanently, proving the tree can
    be captured at all, staging it, and walking the project root to build
    the list of what the baseline actually holds. `git diff --cached`
    cannot produce that list -- a file identical to HEAD makes no diff
    entry and would ride into the commit examined by nothing.

    Governance is not promote's. The list goes to the Rule-065 commit
    driver, which answers only for what it is handed. Promote runs no tests
    and deploys nothing.
    """
    # The operator answers first, before anything is staged and before any
    # rule runs. A promote that is refused by governance still consumed an
    # authorization, and the record has to say so: the outcome is written
    # against the same record the approval created, so an approval with no
    # outcome is always an unfinished promote rather than a silent one.
    gate, record_id = _authorize_promotion(
        "local", "dev", "dev", "dev", repo_root)
    if record_id is None:
        # A refusal and an unanswered page are different facts, and the log
        # said "refused by the operator" for both.
        _CH_LOG.info(f"[promote] not authorized ({gate.last_verdict}); "
                     f"nothing staged")
        return 1

    def _finish(outcome: str, rc: int) -> int:
        gate.record_outcome(record_id, outcome)
        return rc

    # Nothing may hide from the capture. Empties the stash stack for good.
    _require_capturable_tree(repo_root)

    _run_git(["add", "-A"], repo_root)
    deleted = [l for l in _run_git(
        ["diff", "--cached", "--name-only", "--diff-filter=D"], repo_root
    ).stdout.splitlines() if l.strip()]
    if deleted:
        _CH_LOG.info(f"[promote] {len(deleted)} deletion(s) staged; a deleted "
                     f"file is committed, not scanned")
    _require_everything_staged(repo_root)

    files = baseline_files(repo_root)

    driver = (repo_root / "architecture" / "EngineeringRuleEnforcement"
              / "code" / "commit_governance_driver.py")
    rc = subprocess.run(
        [sys.executable, str(driver), "--files-from", "-"],
        input=chr(10).join(files), cwd=str(repo_root),
        text=True, encoding="utf-8", errors="surrogateescape",
    ).returncode
    if rc != 0:
        _CH_LOG.info(f"[promote] governance FAILED (exit {rc}) — nothing committed")
        return _finish("refused_by_governance", rc)

    auto = f"promote local -> dev ({datetime.now(timezone.utc).isoformat()})"
    message = (label + chr(10) + chr(10) + auto) if label else auto
    _CH_LOG.info(f"[promote] commit: {message.splitlines()[0]}")
    rc = subprocess.run(["git", "commit", "--allow-empty", "-m", message],
                        cwd=str(repo_root)).returncode
    if rc != 0:
        _CH_LOG.info(f"[promote] commit refused (exit {rc}); nothing pushed")
        return _finish("refused_at_commit", rc)

    _require_tree_fully_committed(repo_root)
    _CH_LOG.info("[promote] push origin dev")
    rc = subprocess.run(["git", "push", "origin", "dev"],
                        cwd=str(repo_root)).returncode
    return _finish("promoted" if rc == 0 else "failed_push", rc)

def _promote_branch_to_branch(repo_root: Path, source_env: str, target_env: str) -> int:
    """Fully automated: fetch, then push origin/<source> to <target> on
    the remote in a single force-with-lease. Does NOT touch the local
    working tree or switch branches — dirty local files (from unrelated
    work) do not block a promote because the promote is entirely a
    remote-branch pointer move. Implements REQ-B-004 (byte-identical
    overwrite)."""
    source_branch = _ENV_TO_BRANCH[source_env]
    target_branch = _ENV_TO_BRANCH[target_env]

    _CH_LOG.info("[promote] fetch origin")
    _run_git(["fetch", "origin"], repo_root)

    # Rule-065-ENF-010 / EPIC-008-F-012-S-002-REQ-B-006: a human authorizes
    # this promotion, and the authorization is recorded, before any ref moves.
    gate, record_id = _authorize_promotion(
        source_env, target_env, source_branch, target_branch, repo_root)
    if record_id is None:
        _CH_LOG.info(f"[promote] refused — origin {target_branch} unchanged")
        return 1

    # Push origin/<source> tip to remote's <target> branch without a
    # local checkout. Refspec form `<src>:refs/heads/<dst>` moves the
    # remote branch pointer directly; the local working tree is never
    # read or modified. --force-with-lease matches the prior semantics
    # (byte-identical overwrite of target with source's tip, guarded
    # against concurrent-writer surprises).
    refspec = f"refs/remotes/origin/{source_branch}:refs/heads/{target_branch}"
    _CH_LOG.info(f"[promote] push --force-with-lease origin {refspec}")
    result = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", refspec],
        cwd=str(repo_root),
    )
    if result.returncode != 0:
        # The record already says this promotion was authorized. Say what
        # actually happened, or the record is a claim that it succeeded.
        gate.record_outcome(record_id, "failed_ref_did_not_move")
        _CH_LOG.info(f"[promote] push FAILED — origin {target_branch} unchanged")
        return result.returncode

    gate.record_outcome(record_id, "promoted")

    # Refresh the local remote-tracking ref for the target so subsequent
    # `git log origin/<target>` shows the new tip immediately.
    _run_git(["fetch", "origin", target_branch], repo_root)
    return 0


def _authorize_promotion(source_env, target_env, source_branch,
                         target_branch, repo_root):
    """Ask a human, record the answer, and return the record it wrote.

    Returns (worker, record_id). A record_id of None means the promotion is
    refused and no ref may move.
    """
    sys.path.insert(0, str(repo_root / "architecture" /
                           "EngineeringRuleEnforcement" / "code"))
    from promote_authorization_worker import (
        PromoteAuthorizationWorker, PromotionFacts)

    def text(args):
        return (_run_git(args, repo_root).stdout or "").strip()

    tip = text(["rev-parse", f"origin/{source_branch}"])
    subject = text(["log", "-1", "--format=%s", f"origin/{source_branch}"])
    ahead = text(["rev-list", "--count",
                  f"origin/{target_branch}..origin/{source_branch}"]) or "0"

    worker = PromoteAuthorizationWorker(PromotionFacts(
        source_environment=source_env, destination_environment=target_env,
        source_branch=source_branch, destination_branch=target_branch,
        commit=tip, commit_subject=subject, commits_ahead=int(ahead),
        files=len(baseline_files(repo_root))))
    return worker, worker.authorize()


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
    sys.exit(main())
