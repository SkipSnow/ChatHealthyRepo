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
import os
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











class CommitGovernanceDriver:
    def __init__(self, handed_list: list[str] | None = None) -> None:
        self.handed_list = handed_list
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
        """The list this run answers for.

        Handed one, that is it: promote manufactures the whole baseline,
        stages it, and passes it here. Otherwise it is the staged set,
        which is what a commit publishes.
        """
        if self.handed_list is not None:
            return self.handed_list
        listed = self._git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
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
                encoding="utf-8",
                errors="surrogateescape",
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
            "scope": "handed" if self.handed_list is not None else "staged",
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
        "--files-from",
        help="Path holding the file list, or '-' for stdin. Promote builds "
             "the baseline list, stages it, and hands it here. Without this "
             "the staged set is governed, which is what a commit publishes.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        handed = None
        if args.files_from:
            raw = (sys.stdin.read() if args.files_from == "-"
                   else Path(args.files_from).read_text(encoding="utf-8"))
            handed = [l for l in raw.splitlines() if l.strip()]
        return CommitGovernanceDriver(handed_list=handed).run()
    except Exception as exc:  # noqa: BLE001 - the gate never dies silently
        _CH_LOG.error(f"[driver] unhandled: {exc}")
        return EXIT_DRIVER_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
