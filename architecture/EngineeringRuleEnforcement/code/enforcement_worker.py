"""EnforcementWorker — abstract base class for engineering-rule workers.

Binding contract: CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV17.docx
    §4.4.2  (base-class member table)
    §4.5    (scope-list mechanism, precedence walk, _load_scopes contract)
    TR-4, TR-5, TR-6, TR-7, TR-14

Owns:
  • __init__(enforcement_id)  — load own entry from engineering_rules.json
  • _load_scopes()            — virtual; default returns self.entry["scopes"]
  • is_in_scope()             — fixed-precedence walk
  • main()                    — CLI entry; subclass-specific run() invoked here
  • _emit_violation / _emit_telemetry — structured stdout
  • Exit-code semantics       — 0 clean, 1 violations, 2 worker error

Workers MUST NOT install signal handlers, watchdog threads, or any in-process
timeout mechanism. Per design V17 §4.3.2, timeout enforcement is exclusively
the manager's responsibility — the worker does not see the timeout value, and
the manager hard-kills the worker subprocess on the wall-clock budget.
Adding any in-process timeout primitive here is a contract violation.
"""

from __future__ import annotations

import abc
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Repository root (repo>/architecture/EngineeringRuleEnforcement/code/<this>)
# ─────────────────────────────────────────────────────────────────────────────
_THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = _THIS_FILE.parents[3]

ENGINEERING_RULES_PATH: Path = (
    PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
)


# Worker-side exit codes (TR-2 — worker subset).
EXIT_OK: int = 0
EXIT_VIOLATIONS_FOUND: int = 1
EXIT_WORKER_ERROR: int = 2
EXIT_WORKER_INTERNAL_ERROR: int = 5  # see TR-2: missing-schema → exit 5


# Canonical scope_list_type values (BR-14).
SCOPE_LIST_TYPES: tuple[str, ...] = (
    "allowed_exact",
    "allowed_pattern",
    "excluded_exact",
    "excluded_pattern",
)


class WorkerInternalError(Exception):
    """Raised when a worker hits an unrecoverable configuration error.

    Per V17 §4.9.3: missing schema file, malformed schema, schema invalid
    against Draft 2020-12 — any of these surface as WorkerInternalError, which
    the worker's main() converts to exit code 5 (EXIT_WORKER_INTERNAL_ERROR).
    """


class ViolationRecord:
    """Violation record shape per TR-6.

    Fields: enforcement_id, rule_id, resource, message, severity ∈
    {info, warn, error}.
    """

    __slots__ = ("enforcement_id", "rule_id", "resource", "message", "severity")

    def __init__(
        self,
        enforcement_id: str,
        rule_id: str,
        resource: str,
        message: str,
        severity: str = "error",
    ) -> None:
        if severity not in ("info", "warn", "error"):
            raise ValueError(f"severity must be info|warn|error, got {severity!r}")
        self.enforcement_id = enforcement_id
        self.rule_id = rule_id
        self.resource = resource
        self.message = message
        self.severity = severity

    def to_dict(self) -> dict[str, str]:
        return {
            "enforcement_id": self.enforcement_id,
            "rule_id": self.rule_id,
            "resource": self.resource,
            "message": self.message,
            "severity": self.severity,
        }


class EnforcementWorker(abc.ABC):
    """Abstract base for every engineering-rule enforcement worker (V17 §4.4.2)."""

    # SCOPE_DEFAULT is per-check semantics (V17 §4.5 / Table 9). Subclasses
    # that have a single-policy default may set it as a class attribute; per-
    # check semantics are looked up via the SCOPE_DEFAULTS class attribute
    # (mapping function_name → bool) on subclasses that need different
    # defaults across check methods.
    SCOPE_DEFAULT: bool = False
    SCOPE_DEFAULTS: dict[str, bool] = {}  # subclasses override per-method

    def __init__(self, enforcement_id: str) -> None:
        """Store enforcement_id; load entry; load scopes (V17 §4.4.2 ctor row).

        On AttributeError from getattr against a function_name in scopes,
        fail loudly per V17 §4.5 ("a typo in the method name surfaces at
        worker startup as an AttributeError rather than as a silently dropped
        rule").
        """
        self.enforcement_id: str = enforcement_id
        self.entry: dict[str, Any] = self._load_enforcement()
        self.executable_path: str = self.entry.get("executable_path", "")
        self.rule_id: str = self.entry.get("rule_id", "")
        self.hook: str = self.entry.get("hook", "")
        self.scopes: list[list[Any]] = self._load_scopes()
        # Validate that every function_name in scopes is a real method.
        self._validate_scope_function_names()

    # ────────────────────────────────────────────────────────────────────────
    # Load
    # ────────────────────────────────────────────────────────────────────────
    def _load_enforcement(self) -> dict[str, Any]:
        """Find this worker's entry in engineering_rules.json by enforcement_id."""
        with ENGINEERING_RULES_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        for rule in data["rules"]["rule"]:
            rule_id = rule.get("id", "")
            for entry in rule.get("enforcements", {}).get("enforcement", []):
                if entry.get("enforcement_id") == self.enforcement_id:
                    decorated = dict(entry)
                    decorated.setdefault("rule_id", rule_id)
                    return decorated
        raise WorkerInternalError(
            f"enforcement_id {self.enforcement_id!r} not found in "
            f"engineering_rules.json"
        )

    def _load_scopes(self) -> list[list[Any]]:
        """Virtual. Default returns self.entry["scopes"] (V17 §4.5 / Table 4).

        Subclasses MAY override if scope is intrinsically code-driven. The
        default — metadata-driven — is preferred. ScanFilesEnforcementWorker
        does NOT override this (V17 §4.9.1).
        """
        scopes = self.entry.get("scopes", [])
        if not isinstance(scopes, list):
            raise WorkerInternalError(
                f"scopes on {self.enforcement_id} must be a list, got {type(scopes).__name__}"
            )
        return scopes

    def _validate_scope_function_names(self) -> None:
        """Fail loudly if any function_name in scopes isn't a worker method.

        Per V17 §4.5 / §4.9.2: the base class resolves function_name to a
        bound method via getattr(self, row[0]) at startup; a typo surfaces as
        AttributeError instead of a silently dropped rule.
        """
        seen: set[str] = set()
        for row in self.scopes:
            if not isinstance(row, list) or len(row) != 3:
                raise WorkerInternalError(
                    f"scope row malformed on {self.enforcement_id}: {row!r}"
                )
            function_name, scope_list_type, _terms = row
            if scope_list_type not in SCOPE_LIST_TYPES:
                raise WorkerInternalError(
                    f"scope_list_type {scope_list_type!r} on "
                    f"{self.enforcement_id} not in {SCOPE_LIST_TYPES}"
                )
            if function_name in seen:
                continue
            seen.add(function_name)
            if not hasattr(self, function_name):
                raise AttributeError(
                    f"scope function_name {function_name!r} is not a method "
                    f"on {type(self).__name__}; check engineering_rules.json "
                    f"entry {self.enforcement_id}"
                )

    # ────────────────────────────────────────────────────────────────────────
    # Scope evaluation (V17 §4.5 / TR-5 — fixed precedence; lives in base only)
    # ────────────────────────────────────────────────────────────────────────
    def is_in_scope(self, resource: str, function_name: str) -> bool:
        """Walk this resource's matching rows in self.scopes (TR-5).

        Fixed precedence:
            excluded_exact → excluded_pattern → allowed_exact →
            allowed_pattern → SCOPE_DEFAULT (per-check).
        Excludes always trump allows. Exact always trumps pattern. The walk
        is identical for every worker, every check, every resource.
        """
        rows = [r for r in self.scopes if r[0] == function_name]

        # Pull terms by type once.
        excluded_exact: list[str] = []
        excluded_pattern: list[str] = []
        allowed_exact: list[str] = []
        allowed_pattern: list[str] = []
        for _fn, scope_type, terms in rows:
            if scope_type == "excluded_exact":
                excluded_exact.extend(terms)
            elif scope_type == "excluded_pattern":
                excluded_pattern.extend(terms)
            elif scope_type == "allowed_exact":
                allowed_exact.extend(terms)
            elif scope_type == "allowed_pattern":
                allowed_pattern.extend(terms)

        # 1. excluded_exact wins absolutely.
        if resource in excluded_exact:
            return False
        # 2. excluded_pattern next.
        for pat in excluded_pattern:
            if re.search(pat, resource):
                return False
        # 3. allowed_exact next.
        if resource in allowed_exact:
            return True
        # 4. allowed_pattern next.
        for pat in allowed_pattern:
            if re.search(pat, resource):
                return True
        # 5. fall through to per-check default.
        return self._scope_default_for(function_name)

    def _scope_default_for(self, function_name: str) -> bool:
        """Resolve the per-check SCOPE_DEFAULT (V17 Table 9 / §4.5)."""
        if function_name in self.SCOPE_DEFAULTS:
            return self.SCOPE_DEFAULTS[function_name]
        return self.SCOPE_DEFAULT

    # ────────────────────────────────────────────────────────────────────────
    # Subclass contract
    # ────────────────────────────────────────────────────────────────────────
    @abc.abstractmethod
    def run(self) -> int:
        """Subclass orchestration. Return 0 (clean) or 1 (violations).

        Per TR-6 / V17 Table 4 row 10: subclasses iterate resources, gate by
        is_in_scope(), call their domain check method(s) in defined order,
        aggregate violations, and return the status. main() converts uncaught
        exceptions to exit code 2.
        """

    # ────────────────────────────────────────────────────────────────────────
    # Telemetry / violation emission (TR-6, TR-7; V17 §4.4.2)
    # ────────────────────────────────────────────────────────────────────────
    def _emit_violation(self, v: ViolationRecord) -> None:
        """Emit one ViolationRecord per TR-6 — one JSON object per line."""
        payload = {"kind": "violation", **v.to_dict()}
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    def _emit_telemetry(
        self,
        phase: str,
        duration_ms: int,
        files_scanned: int,
        violation_count: int,
        exit_code: int,
    ) -> None:
        """Emit telemetry envelope per TR-7 — one JSON object per line."""
        payload = {
            "kind": "telemetry",
            "enforcement_id": self.enforcement_id,
            "hook": self.hook,
            "phase": phase,
            "duration_ms": duration_ms,
            "files_scanned": files_scanned,
            "violation_count": violation_count,
            "exit_code": exit_code,
        }
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    # ────────────────────────────────────────────────────────────────────────
    # CLI entry (V17 Table 4 row 6)
    # ────────────────────────────────────────────────────────────────────────
    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        """Parse enforcement_id; instantiate cls; emit telemetry; run; return.

        Per V17 Table 4 row 6: traps any uncaught exception (exit 2). Per
        V17 §4.9.3: a WorkerInternalError surfaces as exit 5
        (EXIT_WORKER_INTERNAL_ERROR — the manager promotes worker exit ≥ 3 to
        the same code).
        """
        argv_list = list(sys.argv[1:] if argv is None else argv)
        if len(argv_list) != 1:
            print(
                f"[worker] usage: {cls.__name__} <enforcement_id>",
                file=sys.stderr,
            )
            return EXIT_WORKER_ERROR

        enforcement_id = argv_list[0]

        try:
            worker = cls(enforcement_id)
        except WorkerInternalError as exc:
            print(f"[worker] internal-error: {exc}", file=sys.stderr)
            return EXIT_WORKER_INTERNAL_ERROR
        except (AttributeError, ValueError, KeyError) as exc:
            # Misconfigured scopes / entry → loud worker failure.
            print(f"[worker] startup-error: {exc}", file=sys.stderr)
            return EXIT_WORKER_ERROR

        start_ns = time.perf_counter_ns()
        worker._emit_telemetry(
            phase="start",
            duration_ms=0,
            files_scanned=0,
            violation_count=0,
            exit_code=0,
        )

        rc = EXIT_WORKER_ERROR
        files_scanned = getattr(worker, "files_scanned", 0)
        violation_count = getattr(worker, "violation_count", 0)
        try:
            rc = worker.run()
            files_scanned = getattr(worker, "files_scanned", files_scanned)
            violation_count = getattr(worker, "violation_count", violation_count)
        except WorkerInternalError as exc:
            print(f"[worker] internal-error: {exc}", file=sys.stderr)
            rc = EXIT_WORKER_INTERNAL_ERROR
        except Exception as exc:  # noqa: BLE001 — TR-2 trap: convert to exit 2
            # V17 Table 4 row 6: trap any uncaught exception → exit 2.
            # We re-raise the message to stderr so it isn't lost; the manager
            # captures stderr per warning #4.
            print(f"[worker] uncaught: {type(exc).__name__}: {exc}", file=sys.stderr)
            rc = EXIT_WORKER_ERROR
        finally:
            duration_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
            worker._emit_telemetry(
                phase="end",
                duration_ms=duration_ms,
                files_scanned=files_scanned,
                violation_count=violation_count,
                exit_code=rc,
            )

        return rc


__all__ = [
    "EnforcementWorker",
    "ViolationRecord",
    "WorkerInternalError",
    "EXIT_OK",
    "EXIT_VIOLATIONS_FOUND",
    "EXIT_WORKER_ERROR",
    "EXIT_WORKER_INTERNAL_ERROR",
    "SCOPE_LIST_TYPES",
    "ENGINEERING_RULES_PATH",
    "PROJECT_ROOT",
]
