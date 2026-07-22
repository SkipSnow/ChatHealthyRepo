# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v32 §3.1.4 / §5.2 — Controller entry point.

Runs inside the pipeline docker image on the Pipeline Run VM. The VM was
created by the Runbook (v32 §5.2.2) with cloud-init user_data that pulls
the image and invokes this module:

    python control_runner.py --run-id ... --env-prefix ...

The Runbook has already written a fresh run manifest to
chathealthyfrontend.pipeline.runs with status=pending_vm_provision.
This process:
  1. Reads the manifest via Mongo Atlas Private Endpoint (X.509 identity
     from the Controller's F-003 cert, fetched from KV via the VM's MI).
  2. Updates the manifest to status=running + controller_heartbeat_at.
  3. Walks the STEPS list; spawns Workers as subprocesses.
  4. On terminal state (success/failed/aborted), quiesces:
     - Writes final manifest state
     - Fires `az vm delete --no-wait` on the current VM (farewell script)
     - Exits — cloud-init container ends, VM is destroyed by ARM.

Local runs (developer workstation) skip the VM-teardown step (they
detect PIPELINE_LOCAL_MODE=1 in env).
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

import argparse
import json
import logging
import os
import subprocess
import sys

from blob_client import get_blob_service
from pipeline_db import get_mongo
from pipeline_env import load_pipeline_env
from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
from step_context import PipelineArgs

_log = ChatHealthyLoggingService()


def _fire_farewell_vm_delete() -> None:
    """v32 §5.2.20 quiesce step 6: fire `az vm delete --no-wait` on the
    current VM. ARM destroys the VM within 30-60s. Never blocks.

    In local mode (PIPELINE_LOCAL_MODE=1) this is a no-op — nothing to
    tear down.

    The VM name is read from the AZURE_VM_NAME env var (set by cloud-init
    at boot time from the metadata service) OR derived from the RUN_ID
    (Runbook naming convention: vm-chpipeline-<run_id_short>).
    """
    if os.environ.get("PIPELINE_LOCAL_MODE", "").strip() == "1":
        _log.info("control_runner: local mode — skipping VM delete")
        return
    rg = os.environ.get("AZURE_RESOURCE_GROUP", "").strip()
    if not rg:
        _log.warning("control_runner: AZURE_RESOURCE_GROUP not set — cannot fire VM delete")
        return
    # Prefer explicit env var; otherwise derive from RUN_ID
    vm_name = os.environ.get("AZURE_VM_NAME", "").strip()
    if not vm_name:
        run_id = os.environ.get("RUN_ID", "").strip()
        if not run_id:
            _log.warning("control_runner: neither AZURE_VM_NAME nor RUN_ID set — cannot fire VM delete")
            return
        short_id = run_id.split("-")[-1][:8] if "-" in run_id else run_id[:8]
        vm_name = f"vm-chpipeline-{short_id}"
    # Fire-and-forget: async delete via ARM. The `--no-wait` flag returns
    # immediately; the current process can then exit cleanly. Once the
    # container process ends, cloud-init has nothing more to run.
    try:
        subprocess.Popen(
            ["az", "vm", "delete", "--yes", "--no-wait",
             "--resource-group", rg, "--name", vm_name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _log.info("control_runner: fired az vm delete --no-wait for %s", vm_name)
    except FileNotFoundError:
        _log.warning("control_runner: az CLI not on PATH; VM %s will be reaped by Watchdog", vm_name)
    except Exception as exc:  # noqa: BLE001
        _log.warning("control_runner: farewell VM-delete failed for %s: %s (Watchdog will reap)", vm_name, exc)


def _states_default_from_env() -> str:
    """Runbook publishes STATE_SCOPE as JSON-encoded list. If present,
    that is the operator's chosen scope; use it. Otherwise fall back to
    STATES env, otherwise "ALL"."""
    raw = os.environ.get("STATE_SCOPE", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return ",".join(str(s) for s in parsed) or "ALL"
        if isinstance(parsed, str):
            return parsed or "ALL"
    return os.environ.get("STATES", "ALL")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provider Pipeline Control runner")
    parser.add_argument("--run-id", dest="run_id",
                        default=os.environ.get("RUN_ID") or None,
                        help="Existing manifest run_id; new one is minted if omitted")
    parser.add_argument("--env-prefix", dest="env_prefix",
                        default=os.environ.get("ENV_PREFIX", "dev"),
                        help="Environment prefix (local|dev|qa|prod)")
    parser.add_argument("--states", dest="states",
                        default=_states_default_from_env(),
                        help="Comma-separated state list or ALL. Defaults from "
                             "STATE_SCOPE env (runbook publishes as JSON list).")
    parser.add_argument("--load-mode", dest="load_mode",
                        default=os.environ.get("LOAD_MODE", "full"),
                        choices=["full", "incremental"],
                        help="Full load or incremental. Defaults from LOAD_MODE env.")
    parser.add_argument("--resume-from-step", dest="resume_from_step",
                        default=os.environ.get("RESUME_FROM_STEP") or None,
                        help="Skip completed steps up to this step name. "
                             "Defaults from RESUME_FROM_STEP env.")
    parser.add_argument("--expected-duration-minutes",
                        dest="expected_duration_minutes",
                        type=int,
                        default=int(os.environ.get("EXPECTED_DURATION_MINUTES", "120")))
    parser.add_argument("--log-level", dest="log_level",
                        default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser.parse_args(argv)


def _states_list(raw: str) -> list[str]:
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def main(argv: list[str] | None = None) -> int:
    ns = _parse_args(argv if argv is not None else sys.argv[1:])

    root = ChatHealthyLoggingService()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, ns.log_level.upper(), logging.INFO))

    load_pipeline_env()

    states_list = _states_list(ns.states)
    incremental = (ns.load_mode == "incremental")
    args = PipelineArgs(
        states=states_list,
        env_prefix=ns.env_prefix,
        expected_duration_minutes=ns.expected_duration_minutes,
        resume_from_step=ns.resume_from_step,
        run_id=ns.run_id,
        incremental=incremental,
    )

    orchestrator = ProviderPipelineOrchestrator(
        env=ns.env_prefix,
        config={},
        mongo_client=get_mongo(),
        blob_client=get_blob_service(),
    )
    if ns.run_id:
        os.environ["RUN_ID"] = ns.run_id
    _log.info(
        "control_runner: starting run env_prefix=%s states=%s load_mode=%s "
        "resume_from_step=%s expected_duration_minutes=%d run_id=%s "
        "STATE_SCOPE_env=%r LOAD_MODE_env=%r",
        ns.env_prefix, states_list, ns.load_mode, ns.resume_from_step,
        ns.expected_duration_minutes, ns.run_id or "(mint)",
        os.environ.get("STATE_SCOPE", ""), os.environ.get("LOAD_MODE", ""),
    )
    # v32 §5.2.20 finally-block: quiesce fires on both success and failure
    # paths. The farewell VM-delete is at the tail of quiesce so this
    # wrapper guarantees teardown even if the orchestrator raises.
    try:
        manifest = orchestrator.run(args)
        if manifest.run_id:
            os.environ["RUN_ID"] = manifest.run_id
        _log.info("control_runner: run %s finished status=%s",
                  manifest.run_id, manifest.status)
        exit_code = 0 if manifest.status == "succeeded" else 1
    finally:
        _fire_farewell_vm_delete()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
