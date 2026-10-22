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
A running partition whose heartbeat is older than silent_after_s is shown as
SILENT, not failed (LLD v83 III.15). work_items are purged at quiesce, so a
finished run shows its run record only.

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

    # Surface the run's recent ERROR lines in EVERY state (operator ruling
    # 2026-10-22: errors were invisible while a run was live and only appeared
    # post-mortem), and only THIS incarnation's: a resumed run reuses its
    # run_id, so without the resumed_at epoch filter the tool reprinted the
    # dead incarnation's errors under the live one.
    if status in ("failed", "aborted", "error"):
        fx = run.get("fatal_exception") or {}
        if fx:
            out(f"  FATAL [{fx.get('mode', '')}] {str(fx.get('message') or fx.get('type') or '')[:400]}\n")
    logcoll = "Log_" + str(run.get("env") or "dev")
    try:
        err_q = {"run_id": run_id, "level": "ERROR"}
        epoch = run.get("resumed_at")
        if epoch:
            from bson import ObjectId  # noqa: PLC0415
            err_q["_id"] = {"$gte": ObjectId.from_datetime(
                epoch.replace(tzinfo=datetime.timezone.utc))}
        errs = list(db[logcoll].find(err_q).sort([("_id", -1)]).limit(5))
        for e in reversed(errs):
            stamp = e["_id"].generation_time.strftime("%H:%M:%SZ")
            out(f"  ERROR [{stamp}] {str(e.get('message') or '')[:400]}\n")
    except Exception:  # noqa: BLE001
        pass

    # silent_after_s (LLD v83 III.15): a running item with no beat for this
    # long is SILENT -- a diagnostic, never a failure. Read from the per-env
    # durable configuration; required, no defaults (H7).
    from pipeline.run_lifecycle.pipeline_config import load_pipeline_config  # noqa: PLC0415
    cfg = load_pipeline_config(env_prefix=str(run.get("env") or "dev"))
    silent_after_s = int(cfg["worker_liveness"]["silent_after_s"])
    silent_cutoff = now - datetime.timedelta(seconds=silent_after_s)

    by_step: dict[str, Counter] = {}
    silent_by_step: dict[str, int] = {}
    for wi in db["pipeline.work_items"].find(
            {"run_id": run_id}, {"step": 1, "status": 1, "heartbeat_at": 1}):
        step_name = wi.get("step")
        by_step.setdefault(step_name, Counter())[str(wi.get("status") or "unknown")] += 1
        hb = wi.get("heartbeat_at")
        if (str(wi.get("status")) in _ACTIVE and hb is not None
                and hb < silent_cutoff):
            silent_by_step[step_name] = silent_by_step.get(step_name, 0) + 1

    names = _step_names(run.get("pipeline_name"))
    for extra in sorted(set(by_step) - set(names) - {None}):
        names = names + [extra]

    out(f"| {'step':38s} | {'state':10s} | {'progress':24s} |\n")
    out(f"|{'-' * 40}|{'-' * 12}|{'-' * 26}|\n")
    for name in names:
        counts = by_step.get(name)
        if not counts:
            out(f"| {name:38s} | {'not started':10s} | {'-':24s} |\n")
            continue
        total = sum(counts.values())
        done = sum(counts[s] for s in _DONE if s in counts)
        # failed is a REAL verdict (LLD v83 III.7): written only by the item's
        # own worker, the controller's worker_max_silence_s judgment (H10), or
        # the operator. A running item whose heartbeat is older than
        # silent_after_s is reported SILENT -- a diagnostic, never a failure.
        failed = sum(counts[s] for s in _FAILED if s in counts)
        active = sum(counts[s] for s in _ACTIVE if s in counts)
        pending = counts.get("pending", 0)
        silent = silent_by_step.get(name, 0)
        if done == total:
            state, prog = "complete", f"{done}/{total}"
        else:
            state = "FAILED" if failed else (
                "running" if (active or done) else "queued")
            bits = []
            if active:
                bits.append(f"{active} running")
            if silent:
                bits.append(f"{silent} silent")
            if done:
                bits.append(f"{done} done")
            if failed:
                bits.append(f"{failed} failed")
            if pending:
                bits.append(f"{pending} pending")
            prog = ", ".join(bits) + f" / {total}"
        out(f"| {name:38s} | {state:10s} | {prog:24s} |\n")

    return 0 if status == "succeeded" else 1 if status in _TERMINAL else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report a pipeline run's status by run_id (the run's process id). The run_id is required.")
    parser.add_argument("run_id", help="the run_id to report on (the caller supplies it)")
    ns = parser.parse_args(argv)
    return check(ns.run_id)


if __name__ == "__main__":
    sys.exit(main())
