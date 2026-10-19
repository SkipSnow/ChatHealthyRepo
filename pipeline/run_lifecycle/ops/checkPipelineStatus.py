# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""checkPipelineStatus -- report a pipeline run's live status by run_id.

One argument: the run_id (the run's process id). Reads the run's coordination
state directly from the always-on front-end cluster
(pipelineAdmin.pipeline.runs + pipeline.work_items) -- the store the runbook
writes before the controller exists and the controller writes once it is up, so
a status is available for the whole life of the run (EPIC-010-F-001-S-015). It
does not depend on the controller's :6969 listener being reachable.

Prints the run state, the controller phase, the controller heartbeat age, and
the per-step per-partition progress for every step the run's pipeline declares.

Usage:
    python pipeline/run_lifecycle/ops/checkPipelineStatus.py <run_id>

Exit code: 0 succeeded, 1 failed/aborted, 2 still running, 3 no such run.
"""
from __future__ import annotations

import argparse
import datetime
import os
import pathlib
import sys
from collections import Counter

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline").is_dir():
        sys.path.insert(0, str(_parent))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        break

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

_DONE = ("completed", "succeeded", "done")
_FAILED = ("failed", "error")
_ACTIVE = ("claimed", "running", "in_progress")
_TERMINAL = ("succeeded", "failed", "aborted", "completed")


def _step_names(pipeline_name: str) -> list[str]:
    """The ordered step list the run's pipeline declares, so steps that have not
    started yet still appear. Unknown pipeline -> empty (the work-item steps
    still print)."""
    from pipeline.provider_pipeline.provider_pipeline_orchestrator import ProviderPipelineOrchestrator
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (
        MedicareProviderEvaluationOrchestrator)
    known = {
        ProviderPipelineOrchestrator.PIPELINE_NAME: ProviderPipelineOrchestrator,
        MedicareProviderEvaluationOrchestrator.PIPELINE_NAME: MedicareProviderEvaluationOrchestrator,
    }
    owner = known.get(pipeline_name)
    return [s.name for s in owner.STEPS] if owner else []


def check(run_id: str, out=sys.stdout.write) -> int:
    for _env in ("Code/.env", ".env"):
        if os.path.exists(_env):
            try:
                from dotenv import load_dotenv
                load_dotenv(_env, override=False)
            except ImportError:
                pass
    db = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]
    run = db["pipeline.runs"].find_one({"run_id": run_id})
    if not run:
        out(f"no run with id {run_id}\n")
        return 3
    now = datetime.datetime.utcnow()
    hb = run.get("controller_heartbeat_at")
    hb_age = f"{(now - hb).total_seconds():.0f}s ago" if hb else "n/a"
    status = str(run.get("status", "")).lower()
    out(f"run {run_id}\n")
    out(f"  pipeline={run.get('pipeline_name')} env={run.get('env')} "
        f"scope={run.get('state_scope')}\n")
    out(f"  status={run.get('status')} controller_phase={run.get('controller_phase')} "
        f"heartbeat={hb_age}\n")

    by_step: dict[str, Counter] = {}
    for wi in db["pipeline.work_items"].find({"run_id": run_id}, {"step": 1, "status": 1}):
        by_step.setdefault(wi.get("step"), Counter())[str(wi.get("status") or "unknown")] += 1

    names = _step_names(run.get("pipeline_name"))
    for extra in sorted(set(by_step) - set(names) - {None}):
        names = names + [extra]

    for name in names:
        counts = by_step.get(name)
        if not counts:
            out(f"    - {name:40s} not started\n")
            continue
        total = sum(counts.values())
        done = sum(counts[s] for s in _DONE if s in counts)
        failed = sum(counts[s] for s in _FAILED if s in counts)
        active = sum(counts[s] for s in _ACTIVE if s in counts)
        pending = counts.get("pending", 0)
        if failed:
            state = f"FAILED failed={failed} done={done} total={total}"
        elif done == total:
            state = f"complete {done}/{total}"
        elif active or done:
            state = f"running={active} done={done} pending={pending} total={total}"
        else:
            state = f"queued pending={pending} total={total}"
        out(f"    - {name:40s} {state}\n")

    return 0 if status == "succeeded" else 1 if status in _TERMINAL else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report a pipeline run's status by run_id (the run's process id).")
    parser.add_argument("run_id", help="the run_id to report on")
    ns = parser.parse_args(argv)
    return check(ns.run_id)


if __name__ == "__main__":
    sys.exit(main())
