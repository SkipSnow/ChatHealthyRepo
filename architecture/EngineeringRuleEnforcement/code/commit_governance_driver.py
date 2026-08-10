"""Rule-065 driver — the one place that knows git.

A commit publishes what is staged. Settling what that means is a single
decision, and this is where it is made. Every subordinate enforcer is handed
the resulting array and checks it; none of them enumerates a file set, shells
out to git, or forms an opinion about scope. That is the whole point: the
answer to "what does this commit answer for" exists once.

Two ways in:

    commit     the staged set, exactly as the operator built it.
    promote    the entire tree. Promote hands down --entire-tree and this
               driver empties the stash stack and stages everything, so the
               working tree becomes the staged set and the same rule applies
               without promote owning any git of its own.

The driver hands the array to every subordinate at once, joins them, and
writes one log entry naming every file that passed, every file that failed,
and which enforcement failed it.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

for _d in Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        PROJECT_ROOT = _d
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in sys.path:
            sys.path.insert(0, str(_lib))
        break

from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

ENGINEERING_RULES_PATH = (
    PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
)
AUDIT_LOG_PATH = (
    PROJECT_ROOT
    / "architecture"
    / "EngineeringRuleEnforcement"
    / "ArchitectureDesignAndAuditDocs"
    / "commit_governance.log"
)

RULE_ID = "Rule-065"
SUBORDINATE_HOOK = "pre-commit"

EXIT_OK = 0
EXIT_VIOLATIONS_FOUND = 1
EXIT_DRIVER_ERROR = 2
EXIT_WORKER_SPAWN_FAILURE = 3
EXIT_WORKER_TIMEOUT = 4
EXIT_WORKER_INTERNAL_ERROR = 5

# Worst code wins, same precedence the manager uses.
_PRECEDENCE = (
    EXIT_DRIVER_ERROR,
    EXIT_WORKER_SPAWN_FAILURE,
    EXIT_WORKER_INTERNAL_ERROR,
    EXIT_WORKER_TIMEOUT,
    EXIT_VIOLATIONS_FOUND,
    EXIT_OK,
)

DEFAULT_TIMEOUT_SECONDS = 30


class DriverError(Exception):
    """The driver could not establish what the commit answers for."""


# ── the working-tree duties, moved here from promote ─────────────────────────
# They were promote's because promote was the only caller. They are git, and
# git lives in one place now: emptying the stash, proving the tree can be
# captured at all, and proving `add -A` actually carried everything.


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
            raise DriverError(
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
        raise DriverError(
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
        raise DriverError(
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
        raise DriverError(
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
        raise DriverError(
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


class CommitGovernanceDriver:
    def __init__(self, entire_tree: bool = False,
                 message: str | None = None) -> None:
        self.entire_tree = entire_tree
        self.message = message
        self.files: list[str] = []
        self.results: list[dict[str, Any]] = []

    # ── git — and it lives only here ────────────────────────────────────────
    def _git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise DriverError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def collect(self) -> list[str]:
        """Settle what this commit answers for. The only such decision."""
        if self.entire_tree:
            # Nothing may hide from a whole-tree capture. This empties the
            # stash stack permanently -- what cannot be unstashed is dropped,
            # because a stash that survives makes the baseline a lie -- and
            # refuses on any mechanism that hides a file from `add -A`.
            _require_capturable_tree(PROJECT_ROOT)
            self._git("add", "-A")
            # Verify the claim `add -A` makes rather than trusting it.
            _require_everything_staged(PROJECT_ROOT)

        listed = self._git(
            "diff", "--cached", "--name-only", "--diff-filter=ACMR"
        )
        return [line for line in listed.splitlines() if line.strip()]

    # ── subordinates ────────────────────────────────────────────────────────
    def subordinates(self) -> list[dict[str, Any]]:
        """Rule-065's pre-commit enforcements.

        Each carries its own title, declared in the rules file and required
        by the schema, so the driver reports what ran by name rather than by
        id alone.
        """
        with ENGINEERING_RULES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        for rule in data["rules"]["rule"]:
            if rule.get("id") != RULE_ID:
                continue
            subs = [
                e
                for e in rule.get("enforcements", {}).get("enforcement", [])
                if e.get("hook") == SUBORDINATE_HOOK
            ]
            untitled = [e["enforcement_id"] for e in subs if not e.get("title")]
            if untitled:
                raise DriverError(
                    f"untitled enforcement(s): {', '.join(untitled)}. The "
                    f"driver will not run a check it cannot name."
                )
            return subs
        raise DriverError(f"{RULE_ID} not found in {ENGINEERING_RULES_PATH}")

    def _invoke(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Run one subordinate over the array; collect its exceptions."""
        enforcement_id = entry["enforcement_id"]
        title = entry["title"]
        worker = PROJECT_ROOT / entry["executable_path"]
        timeout = entry.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        payload = "\n".join(self.files) + "\n"

        try:
            proc = subprocess.run(
                [sys.executable, str(worker), enforcement_id],
                cwd=str(PROJECT_ROOT),
                input=payload,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "enforcement_id": enforcement_id,
                "title": title,
                "exit_code": EXIT_WORKER_TIMEOUT,
                "violations": [],
                "detail": f"timed out after {timeout}s",
            }
        except OSError as exc:
            return {
                "enforcement_id": enforcement_id,
                "title": title,
                "exit_code": EXIT_WORKER_SPAWN_FAILURE,
                "violations": [],
                "detail": str(exc),
            }

        violations: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("kind") == "violation":
                violations.append(obj)

        code = proc.returncode
        if code >= EXIT_WORKER_SPAWN_FAILURE:
            code = EXIT_WORKER_INTERNAL_ERROR
        elif code == EXIT_DRIVER_ERROR:
            code = EXIT_WORKER_INTERNAL_ERROR

        return {
            "enforcement_id": enforcement_id,
            "title": title,
            "exit_code": code,
            "violations": violations,
            "detail": proc.stderr.strip()[:2000],
        }

    def dispatch(self, subs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Every subordinate at once, over the same array. Then join."""
        if not subs:
            raise DriverError(
                f"{RULE_ID} declares no {SUBORDINATE_HOOK} enforcement. "
                f"Nothing would be checked, so nothing can be certified."
            )
        if len(subs) == 1:
            return [self._invoke(subs[0])]
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(subs)) as pool:
            return list(pool.map(self._invoke, subs))

    # ── the one log entry ───────────────────────────────────────────────────
    def _record(self, results: list[dict[str, Any]], verdict: int) -> dict[str, Any]:
        failed_by_file: dict[str, list[str]] = {}
        for r in results:
            for v in r["violations"]:
                resource = v.get("resource") or "(unattributed)"
                failed_by_file.setdefault(resource, []).append(
                    f"{r['enforcement_id']} — {r['title']}"
                )

        passed = [f for f in self.files if f not in failed_by_file]

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scope": "entire-tree" if self.entire_tree else "staged",
            "files_examined": len(self.files),
            "verdict": verdict,
            "passed": passed,
            "failed": {f: sorted(set(e)) for f, e in failed_by_file.items()},
            "enforcements": [
                {
                    "enforcement_id": r["enforcement_id"],
                    "title": r["title"],
                    "exit_code": r["exit_code"],
                    "violation_count": len(r["violations"]),
                }
                for r in results
            ],
        }

        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    @staticmethod
    def aggregate(codes: list[int]) -> int:
        for code in _PRECEDENCE:
            if code in codes:
                return code
        return EXIT_OK

    # ── the commit itself, when the driver is asked to make one ────────────
    def _commit_and_push(self, message: str) -> int:
        """Commit the governed tree and push it.

        The commit fires the pre-commit hook, which runs this driver again
        over the same staged set. That second pass is redundant rather than
        circular -- it makes no commit, so it terminates -- and the only ways
        to remove it are --no-verify or a cached verdict, both of which are
        holes in the gate.
        """
        _CH_LOG.info(f"[driver] commit: {message.splitlines()[0]}")
        rc = subprocess.run(
            ["git", "commit", "--allow-empty", "-m", message],
            cwd=str(PROJECT_ROOT),
        ).returncode
        if rc != 0:
            _CH_LOG.error(f"[driver] commit refused (exit {rc}); nothing pushed")
            return rc
        _require_tree_fully_committed(PROJECT_ROOT)
        _CH_LOG.info("[driver] push origin dev")
        return subprocess.run(
            ["git", "push", "origin", "dev"], cwd=str(PROJECT_ROOT),
        ).returncode

    # ── run ─────────────────────────────────────────────────────────────────
    def run(self) -> int:
        try:
            self.files = self.collect()
            subs = self.subordinates()
        except DriverError as exc:
            _CH_LOG.error(f"[driver] {exc}")
            return EXIT_DRIVER_ERROR

        if not self.files:
            _CH_LOG.error("[driver] nothing is staged; there is no commit to govern")
            return EXIT_DRIVER_ERROR

        self.results = self.dispatch(subs)
        verdict = self.aggregate([r["exit_code"] for r in self.results])
        entry = self._record(self.results, verdict)

        failed = entry["failed"]
        _CH_LOG.info(
            f"[driver] {entry['scope']}: {len(self.files)} examined, "
            f"{len(entry['passed'])} passed, {len(failed)} failed "
            f"across {len(subs)} enforcements -> exit {verdict}"
        )
        if verdict == EXIT_OK and self.message is not None:
            return self._commit_and_push(self.message)

        for path, enforcement_ids in sorted(failed.items()):
            _CH_LOG.error(f"[driver]   FAIL {path}  {', '.join(enforcement_ids)}")
        for r in self.results:
            _CH_LOG.info(
                f"[driver]   {r['enforcement_id']}  exit {r['exit_code']}  "
                f"{len(r['violations'])} violation(s)  |  {r['title']}"
            )
        for r in self.results:
            if r["exit_code"] not in (EXIT_OK, EXIT_VIOLATIONS_FOUND):
                _CH_LOG.error(
                    f"[driver]   {r['enforcement_id']} exit {r['exit_code']}: "
                    f"{r['detail'][:400]}"
                )
        return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rule-065 commit governance driver")
    parser.add_argument(
        "--message",
        help=(
            "Commit message. Supplying it makes the driver commit and push "
            "the tree it just governed, rather than only reporting on it."
        ),
    )
    parser.add_argument(
        "--entire-tree",
        action="store_true",
        help=(
            "Promote's instruction: empty the stash stack, stage everything, "
            "and govern the whole working tree rather than the staged set."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return CommitGovernanceDriver(
            entire_tree=args.entire_tree, message=args.message
        ).run()
    except Exception as exc:  # noqa: BLE001 - the gate never dies silently
        _CH_LOG.error(f"[driver] unhandled: {exc}")
        return EXIT_DRIVER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
