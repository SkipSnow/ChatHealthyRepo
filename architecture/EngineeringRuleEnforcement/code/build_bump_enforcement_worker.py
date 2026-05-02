# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""BuildBumpEnforcementWorker — Rule-063 enforcement.

Implements Rule-063's two statements as a side-effect worker:
    1. Insert a new admin.Versions record with build += 1; copy version and
       framework forward unchanged. (Statement 1)
    2. POST {build, version, framework} to the conversation-log producer's
       /version_bump endpoint so it updates its in-memory static. (Statement 2)

Invocation contract per EnforcementWorker:
    python build_bump_enforcement_worker.py <enforcement_id>

Defensive: skips if GITHUB_ACTIONS env var is set (post-commit hooks do not
normally fire on CI, but this guards against future wiring). Push failure
is logged but does not fail the worker — the producer self-corrects on next
restart by re-reading from MongoDB.

Trace:
    Rule-063 (engineering_rules.json) — statements 1 & 2.
    EPIC-008-F-004-S-001-REQ-T-003 — the requirement Rule-063 implements.
    BUG-001 — the cleanup that brings this into being.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow importing from Code/Shared/ops by adding repo root to sys.path.
_THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = _THIS_FILE.parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import after path setup.
from architecture.EngineeringRuleEnforcement.code.enforcement_worker import (  # noqa: E402
    EnforcementWorker,
    ViolationRecord,
    EXIT_OK,
    EXIT_VIOLATIONS_FOUND,
)

PRODUCER_URL = os.environ.get(
    "CONVERSATION_LOG_PRODUCER_URL", "http://127.0.0.1:8100"
)
PUSH_TIMEOUT_SECONDS = 5


class BuildBumpEnforcementWorker(EnforcementWorker):
    """Rule-063: bump the build field; push the new values to the producer."""

    SCOPE_DEFAULT = True  # always in scope; this worker is unconditional

    def run(self) -> int:
        # Defensive: post-commit hooks do not normally fire under GitHub
        # Actions, but skip if they ever do — Rule-063 statement 1 says "NOT
        # part of a deploy pipeline".
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(
                "[Rule-063] GITHUB_ACTIONS=true; skipping build-bump on a "
                "deploy-pipeline commit.",
                file=sys.stderr,
            )
            return EXIT_OK

        # Statement 1: bump the build field in admin.Versions.
        from Code.Shared.ops.bump_build import bump  # noqa: E402

        record = bump()  # returns {build, version, framework, from}

        # Statement 2: push the new {build, version, framework} to the
        # producer's in-memory static. Push failure is loud — emit a
        # violation and return EXIT_VIOLATIONS_FOUND. Silent swallow is
        # forbidden: any failure is a failure.
        import requests  # local import — only this branch needs it

        payload = {
            "build": record["build"],
            "version": record["version"],
            "framework": record["framework"],
        }
        try:
            resp = requests.post(
                f"{PRODUCER_URL}/version_bump",
                json=payload,
                timeout=PUSH_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=self.rule_id,
                resource=PRODUCER_URL,
                message=(
                    f"Push to /version_bump failed with exception "
                    f"{type(exc).__name__}: {exc}. The producer's in-memory "
                    f"static is stale; subsequent Kafka messages will carry "
                    f"the prior build value until the producer restarts and "
                    f"re-reads admin.Versions."
                ),
                severity="error",
            ))
            return EXIT_VIOLATIONS_FOUND

        if resp.status_code != 200:
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=self.rule_id,
                resource=PRODUCER_URL,
                message=(
                    f"Push to /version_bump returned HTTP {resp.status_code}: "
                    f"{resp.text[:200]}. The producer's in-memory static is "
                    f"stale until the producer restarts."
                ),
                severity="error",
            ))
            return EXIT_VIOLATIONS_FOUND

        return EXIT_OK


if __name__ == "__main__":
    sys.exit(BuildBumpEnforcementWorker.main())
