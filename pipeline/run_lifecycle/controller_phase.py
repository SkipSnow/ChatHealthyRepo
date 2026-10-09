# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""controller_phase.py -- the Controller's own authoritative state variable.

The Controller knows what it has done; it does not have to be inferred from
work_items. As it completes each milestone it records where it is to
pipelineAdmin.pipeline.runs on the always-on front-end cluster, so the status
API (and the watchdog, and an operator) read the controller's own account of
itself rather than guessing from side effects.

Two fields, two authorities:
  controller_phase -- what the Controller itself is doing (its lifecycle:
      starting, status_api_up, cluster_waking, cluster_ready, orchestrating,
      quiescing, released). The Controller sets these directly; it knows them.
  worker_phase     -- what the Controller has SEEN on the Worker box by looking
      at it (no_box, box_provisioning, host_up_no_fanout, fan_out_running). This
      is set only from a positive probe of the box (worker_pid_verifier), never
      assumed: a run is not fan_out_running until the pool PIDs were seen there.

Every write is best-effort: recording a phase must never crash the run. A failed
write is logged and the run continues; the phase simply does not advance in the
record that write.
"""
from __future__ import annotations

import datetime

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()

# Controller lifecycle phases, in order. The Controller sets these as it does
# each thing; they are its own account, not an inference.
CONTROLLER_STARTING = "controller_starting"
STATUS_API_UP = "status_api_up"
CLUSTER_WAKING = "cluster_waking"
CLUSTER_READY = "cluster_ready"
ORCHESTRATING = "orchestrating"
QUIESCING = "quiescing"
RELEASED = "released"

# Worker-box observation phases, set ONLY from a probe of the box.
WORKER_NO_BOX = "no_box"
WORKER_BOX_PROVISIONING = "box_provisioning"
WORKER_HOST_UP_NO_FANOUT = "host_up_no_fanout"
WORKER_FAN_OUT_RUNNING = "fan_out_running"


def _runs():
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.runs"]


def set_controller_phase(run_id: str, phase: str, detail: str = "") -> None:
    """Record what the Controller itself is doing. Best-effort."""
    if not run_id:
        return
    try:
        _runs().update_one(
            {"run_id": run_id},
            {"$set": {"controller_phase": phase,
                      "controller_phase_detail": detail,
                      "controller_phase_at": datetime.datetime.utcnow()}},
        )
        _log.LogPipeline("INFO", "controller_phase: run_id=%s -> %s (%s)",
                         run_id, phase, detail or "")
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING", "controller_phase: could not record %s run_id=%s err=%s",
                         phase, run_id, str(exc)[:200])


def set_worker_phase(run_id: str, phase: str, *, pool: int = 0, host: int = 0,
                     detail: str = "") -> None:
    """Record what the Controller SAW on the Worker box, from a probe. Best-effort.
    worker_pool is the fan-out width the probe counted on the box."""
    if not run_id:
        return
    try:
        _runs().update_one(
            {"run_id": run_id},
            {"$set": {"worker_phase": phase,
                      "worker_pool": pool,
                      "worker_host_pids": host,
                      "worker_phase_detail": detail,
                      "worker_probe_at": datetime.datetime.utcnow()}},
        )
        _log.LogPipeline("INFO", "controller_phase: run_id=%s worker -> %s pool=%d (%s)",
                         run_id, phase, pool, detail or "")
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING", "controller_phase: could not record worker phase "
                         "run_id=%s err=%s", run_id, str(exc)[:200])
