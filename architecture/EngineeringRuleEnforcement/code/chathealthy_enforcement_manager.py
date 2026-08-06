"""ChatHealthyEnforcementManager — engineering-rules dispatch + aggregation.

Binding contract: CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV20.docx
    §4.0   (Implementation discipline — TR-trace principle)
    §4.4.1 (manager class table)
    §4.3.1 (concurrency model — manager owns the mutex)
    §4.3.2 (runaway-process control — manager owns the timeout)
    TR-1, TR-2, TR-3, TR-8, TR-14
    Table 5 (enforcement-entry schema)

One file, one main(), one class. Read-only on engineering_rules.json. Spawns each
matching enforcement as an isolated subprocess via subprocess.run, aggregates
return codes by the precedence in TR-2, exits with the worst.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout as FileLockTimeout


# ─────────────────────────────────────────────────────────────────────────────
# Repository root resolution
# ─────────────────────────────────────────────────────────────────────────────
# This file lives at <repo>/architecture/EngineeringRuleEnforcement/code/<this>.
# Repo root is three parents up.
_THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = _THIS_FILE.parents[3]

# Git runs a hook with cwd set to the repository being acted on, and tells the
# hook nothing else about which repo that is (GIT_DIR is unset; GIT_INDEX_FILE
# is relative). Workers are spawned with cwd=PROJECT_ROOT, which erases that
# signal, so it is captured here at launch and forwarded to every worker.
HOOK_CWD_ENV = "CHATHEALTHY_HOOK_CWD"
LAUNCH_CWD = os.getcwd()


class UnknownHookError(Exception):
    """Hook name is not a member of HOOKS."""


class ChatHealthyEnforcementManager:
    """Dispatcher for engineering-rule enforcement workers (design V19 §4.4.1)."""

    # ── HOOKS — TR-1 enum (4 Git + 6 Claude Code = 10) ───────────────────────
    HOOKS: tuple[str, ...] = (
        "pre-commit",
        "commit-msg",
        "post-commit",
        "pre-push",
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "Stop",
        "SessionEnd",
        "InstructionsLoaded",
        "PreCompact",
        "PostCompact",
    )

    # ── ENGINEERING_RULES_PATH (V19 Table 3) ─────────────────────────────────
    ENGINEERING_RULES_PATH: Path = (
        PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
    )

    # ── Exit codes (TR-2) ────────────────────────────────────────────────────
    EXIT_OK: int = 0
    EXIT_VIOLATIONS_FOUND: int = 1
    EXIT_MANAGER_ERROR: int = 2
    EXIT_WORKER_SPAWN_FAILURE: int = 3
    EXIT_WORKER_TIMEOUT: int = 4
    EXIT_WORKER_INTERNAL_ERROR: int = 5

    # ── Timeouts (V19 §4.3.2 / Table 5) ──────────────────────────────────────
    DEFAULT_TIMEOUT_SECONDS: int = 30

    # Aggregation precedence — highest wins (TR-2).
    # 2 > 3 > 5 > 4 > 1 > 0
    _PRECEDENCE: tuple[int, ...] = (2, 3, 5, 4, 1, 0)

    # Where advisory file locks live. Per Table 3 row 16: locks/<id>.lock.
    _LOCK_DIR: Path = PROJECT_ROOT / "architecture" / "EngineeringRuleEnforcement" / "locks"

    def __init__(self, hook_name: str) -> None:
        """Validate hook_name; store. (V19 Table 3, ctor row.)"""
        if hook_name not in self.HOOKS:
            raise UnknownHookError(
                f"unknown hook_name {hook_name!r}; "
                f"must be one of {self.HOOKS}"
            )
        self.hook_name: str = hook_name

    # ────────────────────────────────────────────────────────────────────────
    # Public entry point
    # ────────────────────────────────────────────────────────────────────────
    def run(self) -> int:
        """Load rules, filter, dispatch, aggregate (V19 Table 3 row 11).

        Returns one of EXIT_OK / EXIT_VIOLATIONS_FOUND / EXIT_MANAGER_ERROR /
        EXIT_WORKER_SPAWN_FAILURE / EXIT_WORKER_TIMEOUT / EXIT_WORKER_INTERNAL_ERROR.
        """
        rules = self._load_rules()
        enforcements = self._filter_enforcements(rules, self.hook_name)

        if not enforcements:
            return self.EXIT_OK

        exit_codes: list[int] = []
        for enforcement in enforcements:
            code = self._spawn_worker(enforcement)
            exit_codes.append(code)

        return self._aggregate(exit_codes)

    # ────────────────────────────────────────────────────────────────────────
    # Load + filter
    # ────────────────────────────────────────────────────────────────────────
    def _load_rules(self) -> list[dict[str, Any]]:
        """Read-only load of engineering_rules.json (V19 Table 3 row 12)."""
        with self.ENGINEERING_RULES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        return data["rules"]["rule"]

    def _filter_enforcements(
        self,
        rules: list[dict[str, Any]],
        hook: str,
    ) -> list[dict[str, Any]]:
        """Return enforcement entries whose hook matches (V19 Table 3 row 13).

        The result preserves the rule_id of the parent rule on each entry so
        the manager can include it in spawn-failure messages without re-walking.
        """
        out: list[dict[str, Any]] = []
        for rule in rules:
            rule_id = rule.get("id", "")
            enforcements = rule.get("enforcements", {}).get("enforcement", [])
            for entry in enforcements:
                if entry.get("hook") == hook:
                    decorated = dict(entry)
                    decorated.setdefault("rule_id", rule_id)
                    out.append(decorated)
        return out

    # ────────────────────────────────────────────────────────────────────────
    # Lock acquisition / release
    # ────────────────────────────────────────────────────────────────────────
    # Lock-key granularity (Skip risk-register #3): the lock key is the
    # enforcement_id. Two enforcements that mutate the same shared file (e.g.,
    # both write bugs.json) will NOT serialize against each other under this
    # scheme. For V1 it doesn't matter — every V1 worker is read-only and
    # declares requires_lock = False — but flag for follow-on work when writing
    # workers land.
    def _acquire_lock(self, enforcement_id: str) -> FileLock:
        """Acquire an advisory file lock keyed on enforcement_id (TR-8).

        Default mechanism per design V19: file lock at
        architecture/EngineeringRuleEnforcement/locks/<enforcement_id>.lock.
        Blocks until acquired. May be promoted to a distributed claim later
        without touching workers.
        """
        self._LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = self._LOCK_DIR / f"{enforcement_id}.lock"
        lock = FileLock(str(lock_path))
        lock.acquire()
        return lock

    def _release_lock(self, lock: FileLock) -> None:
        """Release the lock (V19 Table 3 row 17)."""
        lock.release()

    # ────────────────────────────────────────────────────────────────────────
    # Spawn (the heart of the framework)
    # ────────────────────────────────────────────────────────────────────────
    def _spawn_worker(self, enforcement: dict[str, Any]) -> int:
        """Spawn one worker subprocess; return its promoted exit code.

        Behavior per V19 Table 3 row 14:
          • Reads enforcement.executable_path.
          • Resolves the timeout (entry's optional `timeout` field, else
            DEFAULT_TIMEOUT_SECONDS).
          • If enforcement.requires_lock is True, wraps the call in
            _acquire_lock / _release_lock.
          • Invokes subprocess.run([sys.executable, executable_path,
            enforcement_id], timeout=N).
          • Catches subprocess.TimeoutExpired, terminates the subprocess,
            returns EXIT_WORKER_TIMEOUT.
          • Promotes any worker exit ≥ 3 to EXIT_WORKER_INTERNAL_ERROR (TR-2).
          • Captures worker stderr (warning #4) and forwards it on stdout
            when the worker exits abnormally so it isn't silently dropped.

        The worker is never told the timeout — enforcement is exclusively in
        this method (V19 §4.3.2).
        """
        enforcement_id = enforcement["enforcement_id"]
        executable_path = (PROJECT_ROOT / enforcement["executable_path"]).resolve()
        timeout_value = enforcement.get("timeout", self.DEFAULT_TIMEOUT_SECONDS)
        requires_lock = bool(enforcement.get("requires_lock", False))

        lock_handle: FileLock | None = None
        if requires_lock:
            lock_handle = self._acquire_lock(enforcement_id)

        try:
            try:
                worker_env = dict(os.environ)
                worker_env[HOOK_CWD_ENV] = LAUNCH_CWD
                completed = subprocess.run(
                    [sys.executable, str(executable_path), enforcement_id],
                    timeout=timeout_value,
                    capture_output=True,
                    text=True,
                    check=False,
                    cwd=str(PROJECT_ROOT),
                    env=worker_env,
                )
            except subprocess.TimeoutExpired as exc:
                # Manager-owned hard kill on timeout (V19 §4.3.2).
                print(
                    f"[manager] timeout {enforcement_id}: exceeded "
                    f"{timeout_value}s",
                    file=sys.stderr,
                )
                if exc.stderr:
                    stderr_text = (
                        exc.stderr.decode("utf-8", errors="replace")
                        if isinstance(exc.stderr, (bytes, bytearray))
                        else str(exc.stderr)
                    )
                    print(
                        f"[manager] worker stderr (timeout): {stderr_text}",
                        file=sys.stderr,
                    )
                return self.EXIT_WORKER_TIMEOUT
            except OSError as exc:
                # Spawn failure path (TR-2).
                print(
                    f"[manager] spawn-failure {enforcement_id}: {exc}",
                    file=sys.stderr,
                )
                return self.EXIT_WORKER_SPAWN_FAILURE
        finally:
            if lock_handle is not None:
                self._release_lock(lock_handle)

        # Forward worker stdout (telemetry + violations) to our stdout.
        if completed.stdout:
            sys.stdout.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                sys.stdout.write("\n")

        worker_rc = completed.returncode

        # Capture worker stderr on abnormal exit (warning #4): never silently
        # drop it.
        if worker_rc >= 2 and completed.stderr:
            print(
                f"[manager] worker stderr ({enforcement_id}, rc={worker_rc}):",
                file=sys.stderr,
            )
            sys.stderr.write(completed.stderr)
            if not completed.stderr.endswith("\n"):
                sys.stderr.write("\n")

        # Promote per TR-2: worker exit codes are 0/1/2; anything ≥ 3 from a
        # worker is promoted to EXIT_WORKER_INTERNAL_ERROR.
        if worker_rc == 0:
            return self.EXIT_OK
        if worker_rc == 1:
            return self.EXIT_VIOLATIONS_FOUND
        if worker_rc == 2:
            return self.EXIT_WORKER_INTERNAL_ERROR
        # ≥ 3 → promote
        return self.EXIT_WORKER_INTERNAL_ERROR

    # ────────────────────────────────────────────────────────────────────────
    # Aggregation
    # ────────────────────────────────────────────────────────────────────────
    def _aggregate(self, exit_codes: list[int]) -> int:
        """Apply precedence 2 > 3 > 5 > 4 > 1 > 0. Empty → 0 (V19 Table 3 row 15)."""
        if not exit_codes:
            return self.EXIT_OK
        for rank in self._PRECEDENCE:
            if rank in exit_codes:
                return rank
        # Unknown codes: be safe — surface as manager error.
        return self.EXIT_MANAGER_ERROR


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point — single main() per V19 Table 0 row 2
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    """Hook entry point. Invoked by every Git / Claude Code hook with hook_name."""
    parser = argparse.ArgumentParser(
        prog="chathealthy_enforcement_manager",
        description="ChatHealthy engineering-rule enforcement dispatcher (V19).",
    )
    parser.add_argument(
        "--hook",
        required=True,
        choices=ChatHealthyEnforcementManager.HOOKS,
        help="The Git or Claude Code hook event firing.",
    )
    args = parser.parse_args(argv)

    try:
        manager = ChatHealthyEnforcementManager(args.hook)
    except ValueError as exc:
        print(f"[manager] {exc}", file=sys.stderr)
        return ChatHealthyEnforcementManager.EXIT_MANAGER_ERROR

    return manager.run()


if __name__ == "__main__":
    sys.exit(main())
