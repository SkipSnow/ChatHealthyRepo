# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Per-step status of a pipeline run.

The step list comes from the orchestrator that owns it, so a step added,
renamed or merged shows up here without anything being edited. An earlier
copy of this tool carried its own list, and when type1/type2 merged into
entity_first_branch it reported four steps that no longer exist as "not
started" and never showed the two that ran.

Usage:
    python pipeline/Code/ops/pipeline_run_status.py [--pipeline provider]
                                                    [--run-id RUN_ID]
                                                    [--since ISO8601]
                                                    [--watch SECONDS]

Exit code: 0 the run succeeded, 1 it failed, 2 it is still going, 3 no run.
"""
from __future__ import annotations

import argparse
import datetime
import pathlib
import sys
import time

# The modules this reads live beside the pipeline, not beside this file. The
# usage line above says to run it from the repository root, which could not
# work without this: pipeline_db is not importable from there.
_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline" / "Code").is_dir():
        sys.path.insert(0, str(_parent / "pipeline" / "Code"))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        break

DONE = ("completed", "succeeded", "done")
FAILED = ("failed", "error")
ACTIVE = ("claimed", "running", "in_progress")
TERMINAL_RUN = ("succeeded", "failed", "aborted", "completed")


def _orchestrator(pipeline_name: str):
    """The orchestrator that owns this pipeline's steps."""
    from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
    known = {ProviderPipelineOrchestrator.PIPELINE_NAME: ProviderPipelineOrchestrator}
    if pipeline_name not in known:
        _raise_unknown_pipeline(pipeline_name, sorted(known))
    return known[pipeline_name]


def _raise_unknown_pipeline(name: str, known: list[str]) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    from chathealthy_lib.exceptions import ChatHealthyException
    raise ChatHealthyException(
        mode="value_error",
        message=f"no orchestrator for pipeline {name!r}; known: {', '.join(known)}",
        component="pipeline_run_status",
    )


def step_names(pipeline_name: str) -> list[str]:
    return [s.name for s in _orchestrator(pipeline_name).STEPS]


def hhmmss(seconds) -> str:
    if seconds is None:
        return "        "
    s = int(max(seconds, 0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def find_run(db, pipeline_name: str, run_id: str | None, since):
    if run_id:
        return db["pipeline.runs"].find_one({"run_id": run_id})
    query: dict = {"pipeline_name": pipeline_name} if pipeline_name else {}
    if since:
        query["started_at"] = {"$gte": since.replace(tzinfo=None)}
    return db["pipeline.runs"].find_one(query, sort=[("started_at", -1)])


def render(db, run, names: list[str], now) -> str:
    rid = run["run_id"]
    started = aware(run.get("started_at"))
    scope = ",".join(run.get("state_scope") or []) or "-"
    # Only fields the run actually records. data_version and debug arrive on
    # the webhook and are never written to pipeline.runs, so they are not
    # shown rather than shown wrong.
    lines = [
        f"run {rid}  |  {scope}  |  {run.get('load_mode') or '-'}  |  "
        f"{run.get('env') or '-'}  |  {run.get('invocation_mode') or '-'}  |  "
        f"{run.get('status')}  |  elapsed "
        f"{hhmmss((now - started).total_seconds() if started else None)}"
    ]
    by_step: dict = {}
    for item in db["pipeline.work_items"].find({"run_id": rid}):
        by_step.setdefault(item.get("step"), []).append(item)

    # A step the orchestrator does not declare but that has work items is a
    # real event: it says the running code and this checkout disagree.
    for extra in sorted(set(by_step) - set(names) - {None}):
        names = names + [extra]

    for name in names:
        rows = by_step.get(name) or []
        if not rows:
            lines.append(f"  - {name:38s} not started")
            continue
        states = [r.get("status") or "unknown" for r in rows]
        done = sum(1 for s in states if s in DONE)
        failed = sum(1 for s in states if s in FAILED)
        active = sum(1 for s in states if s in ACTIVE)
        if failed:
            state = f"FAILED {failed}/{len(rows)}"
        elif done == len(rows):
            state = f"complete {done}/{len(rows)}"
        elif active or done:
            state = f"running {done}/{len(rows)}"
        else:
            state = f"queued 0/{len(rows)}"
        starts = [aware(r.get("claimed_at") or r.get("created_at")) for r in rows]
        ends = [aware(r.get("completed_at") or r.get("finished_at")) for r in rows]
        starts = [s for s in starts if s]
        ends = [e for e in ends if e]
        if starts:
            end = max(ends) if len(ends) == len(rows) else now
            elapsed = hhmmss((end - min(starts)).total_seconds())
        else:
            elapsed = hhmmss(None)
        lines.append(f"  - {name:38s} {state:22s} {elapsed}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pipeline", default="provider")
    parser.add_argument("--run-id", default=None,
                        help="a specific run; otherwise the most recent one")
    parser.add_argument("--since", default=None,
                        help="ISO8601; ignore runs that began before this")
    parser.add_argument("--watch", type=int, default=0,
                        help="seconds between reads; stops at a terminal state")
    args = parser.parse_args()

    since = None
    if args.since:
        since = datetime.datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=datetime.timezone.utc)

    # Run from a workstation the identity comes from the local .env, the same
    # file every other pipeline tool reads; in a container it is already in
    # the environment and nothing is overridden.
    try:
        from dotenv import load_dotenv
        load_dotenv(".env", override=False)
    except ImportError:
        pass

    from pipeline_db import get_metadata_db
    db = get_metadata_db()
    names = step_names(args.pipeline)

    while True:
        now = datetime.datetime.now(datetime.timezone.utc)
        run = find_run(db, args.pipeline, args.run_id, since)
        if not run:
            sys.stdout.write(
                f"no {args.pipeline} run"
                + (f" since {since.isoformat()}" if since else "") + "\n")
            for name in names:
                sys.stdout.write(f"  - {name:38s} not started\n")
            sys.stdout.flush()
            if not args.watch:
                return 3
            time.sleep(args.watch)
            continue

        sys.stdout.write(render(db, run, names, now) + "\n")
        sys.stdout.flush()
        status = str(run.get("status", "")).lower()
        if not args.watch:
            return 0 if status == "succeeded" else 1 if status in TERMINAL_RUN else 2
        if status in TERMINAL_RUN:
            return 0 if status == "succeeded" else 1
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
