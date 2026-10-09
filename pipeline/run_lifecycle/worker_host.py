"""worker_host.py - the resident worker host for the two-VM run shape.

One Worker VM per run (the big compute box), provisioned once by the common
hardware-allocation function (worker_vm_provisioning.allocate_worker_vm). Its
cloud-init boots this host. The host drains the run's work_items queue by
spawning a BOUNDED POOL of pipeline_worker processes - one process per claimed
partition - across every fan-out step of the run, until the run is terminal,
then returns so the cloud-init self-deletes the VM.

This is the Worker side of the two-VM topology (LLD Part I, I.1.1.5 / I.2.3.3).
Workers are PROCESSES on this one box, not one VM per partition: the hardware a
run needs is this single box, sized once. Controller and host never speak
directly; the only channel is the Mongo work_items collection on the always-on
front-end cluster. The Controller enqueues items per fan-out step; this host
claims and runs them; the Controller watches the same items for completion.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.exceptions import ChatHealthyException

# Separate process on the Worker VM: it names its own Mongo identity, because
# nothing the Controller set in memory survives onto this box.
set_mongo_log_identity("pipelineEditor")

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

_log = ChatHealthyLoggingService()

# A run is terminal when the Controller has stopped driving it. The host
# returns on any of these so the Worker VM self-deletes.
_TERMINAL = frozenset({"succeeded", "failed", "aborted", "cancelled", "completed", "done"})

# Poll cadences.
_FILL_SLEEP_S = 2       # between pool-fill passes while work is in flight
_IDLE_SLEEP_S = 5       # between passes when the queue is momentarily empty
_LOG_EVERY_S = 120      # progress heartbeat to the run log


def _pool_cap() -> int:
    """How many Worker processes run concurrently on this box. The box is
    sized (D32s) for len(state_scope) x entity_types; the cap defaults to the
    core count and is overridable by the deploy via WORKER_MAX_PARALLEL."""
    raw = os.environ.get("WORKER_MAX_PARALLEL", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return os.cpu_count() or 4


def _spawn(worker_py: str, step: str, run_id: str, replica: int, env: dict):
    """Spawn one pipeline_worker process. It claims one pending work_item for
    (run_id, step), runs it, and exits; this host reaps it by handle. The host
    outlives its children, so the bootstrap cert in the environment stays valid
    for the whole pool and is cleaned up only when the host itself exits."""
    import subprocess  # noqa: PLC0415
    child_env = dict(env)
    child_env["CH_SPACE_NAME"] = "worker"
    child_env["CH_COMPONENT"] = "worker"
    child_env["RUN_ID"] = run_id
    return subprocess.Popen(
        [sys.executable, worker_py, step, "--replica", str(replica),
         "--run-id", run_id],
        env=child_env,
    )


def _run() -> int:
    run_id = os.environ.get("RUN_ID", "").strip()
    if not run_id:
        raise ChatHealthyException(
            mode="config_error",
            message="worker_host: RUN_ID is absent from the environment; the "
                    "Worker VM cloud-init must supply it.",
            component="worker_host")

    cap = _pool_cap()
    worker_py = str(Path(__file__).parent / "pipeline_worker.py")
    env = os.environ.copy()

    coord = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    wi = coord["pipelineAdmin"]["pipeline.work_items"]
    runs = coord["pipelineAdmin"]["pipeline.runs"]

    _log.LogPipeline("INFO",
        "worker_host: up run_id=%s pool_cap=%d worker=%s", run_id, cap, worker_py)

    active: list = []          # live pipeline_worker Popen handles
    replica = 0
    last_log = 0.0
    while True:
        # Reap finished workers.
        active = [p for p in active if p.poll() is None]

        # Fill the pool from pending work_items. Steps run sequentially, so the
        # pending items are for the current fan-out step; a worker claims one
        # atomically via findOneAndUpdate (two never claim the same item).
        # Never spawn more workers than there are pending items to claim. A
        # serial (single-partition) step has exactly one pending item, so it
        # needs exactly one worker; spawning the whole pool_cap for it
        # over-spawns workers that find nothing to claim and can mask a step
        # with no claimable work. Bound concurrency to min(cap, pending_count).
        pending = wi.find_one({"run_id": run_id, "status": "pending"}, {"step": 1})
        pending_count = wi.count_documents({"run_id": run_id, "status": "pending"})
        want = min(cap, pending_count)
        if pending is not None and len(active) < want:
            try:
                active.append(_spawn(worker_py, pending["step"], run_id, replica, env))
                _log.LogPipeline("INFO",
                    "worker_host: spawned worker step=%s run_id=%s replica=%d "
                    "active=%d want=%d cap=%d pending=%d",
                    pending["step"], run_id, replica, len(active), want, cap, pending_count)
                replica += 1
            except Exception as exc:  # noqa: BLE001
                # The item stays pending and is retried next pass; a persistent
                # failure surfaces to the Controller's wait loop and the
                # Watchdog. Log and keep draining.
                _log.LogPipeline("ERROR",
                    "worker_host: failed to spawn worker step=%s run_id=%s: %s",
                    pending["step"], run_id, str(exc)[:300])
            continue  # tight loop to fill the pool before sleeping

        now = time.time()
        if now - last_log >= _LOG_EVERY_S:
            remaining = wi.count_documents(
                {"run_id": run_id, "status": {"$in": ["pending", "running"]}})
            _log.LogPipeline("INFO",
                "worker_host: draining run_id=%s active=%d open_items=%d",
                run_id, len(active), remaining)
            last_log = now

        if not active and pending is None:
            run_doc = runs.find_one({"run_id": run_id}, {"status": 1})
            status = (run_doc or {}).get("status")
            if status in _TERMINAL:
                _log.LogPipeline("INFO",
                    "worker_host: run_id=%s terminal status=%s; host exiting, "
                    "VM self-deletes", run_id, status)
                return 0
            time.sleep(_IDLE_SLEEP_S)  # between fan-out steps; await next items
        else:
            time.sleep(_FILL_SLEEP_S)


def main() -> int:
    return _run()


if __name__ == "__main__":
    sys.exit(main())
