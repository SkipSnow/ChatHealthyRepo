# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 §3.1 — BasePipelineOrchestrator.

In-process ACA Control substrate. No Durable Functions. No fallbacks.

Behavior:
  * Constructor accepts (env, config, mongo_client, blob_client).
  * Subclass declares PIPELINE_NAME (str) and STEPS (list[StepSpec]).
  * run(args) topologically sequences STEPS, honoring StepSpec.prerequisites
    and StepSpec.invocation_phase in {main_loop, finally_block}.
  * Steps with parallelism in {serial, gather} execute in-process via the
    step's run_step(ctx) callable, resolved through steps.STEP_RUNNERS.
  * Steps with parallelism == process_pool are dispatched to per-partition
    ACA Worker Jobs via aca_job_manager.start_job; local runs are
    handled by aca_job_manager's own local-mode branch, which returns
    without contacting ARM. In local mode this orchestrator additionally
    invokes the step's run_step(ctx) in-process for each partition so the
    coordination substrate stays exercised.
  * PipelineArgs.resume_from_step skips completed steps up to that name.
  * finally_block steps run in a try/finally on both success and failure.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.subprocess_utilities import spawn_detached_worker


import datetime as _dt
import json
import os
import subprocess
import sys
import time
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from step_context import PipelineArgs, RunManifest, StepContext, StepTransition
from step_spec import StepSpec
from steps import get_runner
from steps._partitions import county_partitions, state_partitions
from pipeline_db import PIPELINE_ADMIN_DB

_log = ChatHealthyLoggingService()

# Worker heartbeat is written every 120s by pipeline_worker._HeartbeatThread.
# Controller marks a Worker "crashed" if its WI status is still running AND
# heartbeat_at is older than this threshold. 5 min = >= 2 missed heartbeats.
_HEARTBEAT_STALE_S = 300


def _require_claimable(wi_coll, run_id: str, step: str, enqueued: int) -> int:
    """Return the number of readable pending items, or raise.

    No worker is spawned until every item it might claim is readable. A worker
    looks once, by design; if it can start before its item is visible then that
    single look can find nothing, the item stays pending forever, and the
    Controller waits on a claim that will never come. Ordering removes the
    possibility -- retrying in the worker would only have hidden it.
    """
    claimable = wi_coll.count_documents(
        {"run_id": run_id, "step": step, "status": "pending"})
    if claimable != enqueued:
        raise ChatHealthyException(
            mode="work_items_not_visible",
            message=(
                f"orchestrator: enqueued {enqueued} work item(s) for step "
                f"{step!r} but only {claimable} are readable. Spawning workers "
                f"now would strand the difference."
            ),
            step=step, run_id=run_id, enqueued=enqueued, readable=claimable,
        )
    return claimable


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BasePipelineOrchestrator:
    PIPELINE_NAME: str = ""
    STEPS: list[StepSpec] = []

    def __init__(
        self,
        env: str,
        config: dict[str, Any],
        mongo_client: Any,
        blob_client: Any,
    ) -> None:
        if not self.PIPELINE_NAME:
            raise NotImplementedError("Subclass must declare PIPELINE_NAME")
        if not self.STEPS:
            raise NotImplementedError("Subclass must declare STEPS")
        self.env = env
        self.config = dict(config or {})
        self.mongo_client = mongo_client
        self.blob_client = blob_client

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self, args: PipelineArgs) -> RunManifest:
        manifest = RunManifest.new(self.PIPELINE_NAME, args)
        _log.info(
            "orchestrator run=%s pipeline=%s env=%s steps=%d",
            manifest.run_id, self.PIPELINE_NAME, args.env_prefix, len(self.STEPS),
        )

        main_steps = [s for s in self.STEPS if s.invocation_phase == "main_loop"]
        finally_steps = [s for s in self.STEPS if s.invocation_phase == "finally_block"]

        self._validate_prerequisites(self.STEPS)

        ctx = StepContext(
            args=args,
            manifest=manifest,
            config=self.config,
            mongo_client=self.mongo_client,
            blob_client=self.blob_client,
        )

        skipping_until = args.resume_from_step
        try:
            for spec in main_steps:
                if skipping_until and spec.name != skipping_until:
                    _log.info("skip step=%s (resume_from_step=%s)", spec.name, skipping_until)
                    manifest.completed_steps.add(spec.name)
                    continue
                skipping_until = None
                self._require_prereqs_met(spec, manifest)
                self._invoke_step(spec, ctx)
            manifest.status = "succeeded"
        except Exception as exc:
            manifest.status = "failed"
            manifest.metrics.setdefault("failure", {})["message"] = str(exc)
            _log.exception("orchestrator run=%s failed at main-loop step", manifest.run_id)
            raise
        finally:
            for spec in finally_steps:
                try:
                    self._invoke_step(spec, ctx)
                except Exception:
                    _log.exception(
                        "finally-block step %s raised; continuing", spec.name
                    )
            manifest.updated_at = _utc_now_iso()

        return manifest

    # ------------------------------------------------------------------ #
    # Step invocation
    # ------------------------------------------------------------------ #
    def _invoke_step(self, spec: StepSpec, ctx: StepContext) -> None:
        started = _utc_now_iso()
        transition = StepTransition(step=spec.name, status="running", started_at=started)
        ctx.manifest.step_transitions.append(transition)
        ctx.manifest.updated_at = started

        try:
            # LLD v36 §3.1.4/§3.1.5/§4.3.2: Controller orchestrates,
            # Workers execute. Every step -> Worker subprocess dispatch;
            # single-partition steps get one Worker, N-partition steps
            # get N. Controller never executes step business logic itself.
            if spec.parallelism not in (None, "serial", "gather", "process_pool"):
                raise ChatHealthyException(
                    mode="value_error",
                    message=f"Unknown parallelism {spec.parallelism!r} on step {spec.name}",
                )
            summary = self._invoke_process_pool(spec, ctx)
            transition.summary = summary if isinstance(summary, dict) else {"result": summary}
            transition.status = "success"
            ctx.step_summaries[spec.name] = transition.summary
            ctx.manifest.completed_steps.add(spec.name)
        except Exception as exc:
            transition.status = "failed"
            transition.error = str(exc)
            raise
        finally:
            transition.finished_at = _utc_now_iso()
            ctx.manifest.updated_at = transition.finished_at

    def _invoke_in_process(self, spec: StepSpec, ctx: StepContext) -> dict:
        runner = get_runner(spec.name)
        result = runner(ctx)
        return result if isinstance(result, dict) else {"result": result}

    def _invoke_process_pool(self, spec: StepSpec, ctx: StepContext) -> dict:
        """LLD v22-v25 + v32 §4.3.4 fan-out: Worker-subprocess dispatch.

        Controller is orchestration only. Per-partition work is executed by
        `pipeline_worker.py` subprocesses. Flow:
          1. Insert one work_item per partition into pipeline.work_items
             (status=pending, payload carries partition + run_id + config).
          2. Spawn N Worker subprocesses (bounded concurrency). Each Worker
             atomically claims a work_item, dispatches to steps.get_runner,
             writes status=done + output on success, status=failed on error.
          3. Wait until every enqueued work_item is terminal (done or
             failed). Aggregate results into a step summary.
        """
        partitions = list(self._partitions_for(spec, ctx))
        if not partitions:
            return {"step": spec.name, "partitions": 0, "mode": "worker_subprocess"}

        run_id = ctx.manifest.run_id
        # LLD v36 §4.3.4: the coordination substrate (pipeline.work_items)
        # lives on the FRONT-END cluster (always-on chathealthyfrontend
        # Atlas). ctx.mongo_client is the PIPELINE cluster (paused between
        # runs) and is not the right target for coordination writes.
        from pipeline_db import get_frontend_mongo  # noqa: PLC0415
        wi_coll = get_frontend_mongo()[PIPELINE_ADMIN_DB]["pipeline.work_items"]

        # 1. Enqueue work_items.
        args_snapshot = {
            "env_prefix": ctx.args.env_prefix,
            "state_scope": ctx.args.state_scope or ctx.args.states,
            "resume_from_step": ctx.args.resume_from_step,
            "load_mode": (
                "full" if not ctx.args.incremental else "incremental"
            ),
            "data_version": int(ctx.args.data_version),
            # Operator-driven flags carried on the args snapshot so worker
            # subprocesses see the same value the Controller received.
            "google_maps_enabled": bool(getattr(ctx.args, "google_maps_enabled", False)),
        }
        # Config we pass to Worker excludes non-picklable clients; each
        # Worker reconstructs its own mongo_client + blob_client.
        config_snapshot = {
            k: v for k, v in ctx.config.items()
            if k not in ("mongo_client", "blob_client")
        }
        now = _dt.datetime.utcnow()
        item_ids: list = []
        for part in partitions:
            payload = {
                "run_id": run_id,
                "step": spec.name,
                "partition": part,
                "config": config_snapshot,
                "args": args_snapshot,
                "pipeline_name": ctx.manifest.pipeline_name,
            }
            doc = {
                "run_id": run_id,
                "step": spec.name,
                "status": "pending",
                "payload": payload,
                "created_at": now,
                "partition": part,
            }
            r = wi_coll.insert_one(doc)
            item_ids.append(r.inserted_id)

        claimable = _require_claimable(wi_coll, run_id, spec.name, len(item_ids))
        _log.info(
            "orchestrator dispatch step=%s partitions=%d work_items_enqueued=%d "
            "claimable=%d",
            spec.name, len(partitions), len(item_ids), claimable,
        )

        # 2. Spawn N Worker subprocesses. Cap concurrency at min(partitions,
        #    WORKER_MAX_PARALLEL). Default cap = one worker per partition
        #    (no artificial ceiling); the env override lets the operator
        #    dial down when the VM cannot sustain that many concurrent
        #    Python processes. Workers race on findOneAndUpdate to claim.
        max_parallel_env = os.environ.get("WORKER_MAX_PARALLEL", "").strip()
        max_parallel_cap = (
            int(max_parallel_env) if max_parallel_env.isdigit()
            else len(partitions)
        )
        max_parallel = min(len(partitions), max_parallel_cap)
        worker_py = str(Path(__file__).parent / "pipeline_worker.py")
        env = os.environ.copy()
        env["RUN_ID"] = run_id
        env["ENV_PREFIX"] = ctx.args.env_prefix
        env["PIPELINE_NAME"] = ctx.manifest.pipeline_name
        # Workers write to the same Log_{env} Mongo collection as Controller.
        # Override (not setdefault) CH_SPACE_NAME + CH_COMPONENT so Worker
        # log docs are distinguishable from Controller docs in the same run.
        # Controller cloud-init pins CH_COMPONENT='provider_pipeline_control';
        # Workers must not inherit that string.
        env["CH_LOG_DESTINATION"] = env.get("CH_LOG_DESTINATION", "stderr,mongo")
        env["CH_SPACE_NAME"] = "worker"
        env["CH_COMPONENT"] = "worker"
        # Workers are FULLY DETACHED daemons: double-fork + setsid so the
        # grandchild reparents to init (PID 1). Controller keeps NO handle.
        # This is a firm policy — Controller/Worker communication is Mongo-
        # only in both directions. The prior PIPE+subprocess.Popen design
        # deadlocked NPPES on 64KB kernel-buffer fill; that failure mode is
        # structurally impossible now because no pipe exists between them.
        worker_pids: list[int] = []
        for replica in range(max_parallel):
            pid = spawn_detached_worker(
                [sys.executable, worker_py, spec.name,
                 "--replica", str(replica),
                 "--run-id", run_id],
                env=env,
            )
            worker_pids.append(pid)
        # Declared in THIS process's environment, not the child's: it is
        # bootstrap's atexit here that would otherwise unlink the identity
        # certificate these detached children authenticate with. They are
        # reparented to init and outlive us; their credential must not be
        # deleted on our way out.
        os.environ["CHATHEALTHY_DETACHED_CHILDREN"] = str(
            len(worker_pids)
            + int(os.environ.get("CHATHEALTHY_DETACHED_CHILDREN", "0") or 0)
        )
        _log.info(
            "orchestrator spawned step=%s workers=%d worker_py=%s pids=%s "
            "detached_children_total=%s cert=%s",
            spec.name, max_parallel, worker_py, worker_pids,
            os.environ["CHATHEALTHY_DETACHED_CHILDREN"],
            os.environ.get("CHATHEALTHY_CERT_PATH", "<unset>"),
        )

        # 3. Wait for terminal state on every work_item. Poll every 5s.
        #    Crash detection is heartbeat-based (not p.poll()) so the
        #    Controller has zero stdio/pid dependency on the Worker: Worker
        #    heartbeats every 120s to WI.heartbeat_at (pipeline_worker.py
        #    _HeartbeatThread). A running WI whose heartbeat_at is stale
        #    beyond _HEARTBEAT_STALE_S is a crash. This lets Workers run
        #    fully detached (start_new_session, DEVNULL stdio) without
        #    losing crash detection.
        deadline_polls = 0
        while True:
            open_count = wi_coll.count_documents({
                "_id": {"$in": item_ids},
                "status": {"$in": ["pending", "running"]},
            })
            if open_count == 0:
                break
            # Heartbeat-based crash check: any 'running' WI whose heartbeat_at
            # is older than _HEARTBEAT_STALE_S is a dead Worker. Mark failed.
            stale_cutoff = _dt.datetime.utcnow() - _dt.timedelta(seconds=_HEARTBEAT_STALE_S)
            crashed = list(wi_coll.find({
                "_id": {"$in": item_ids},
                "status": "running",
                "heartbeat_at": {"$lt": stale_cutoff},
            }, {"_id": 1, "partition": 1, "heartbeat_at": 1}))
            if crashed:
                self._surface_crashed_workers(wi_coll, spec, crashed)
            deadline_polls += 1
            if deadline_polls % 24 == 0:  # every ~2 min
                _log.info(
                    "orchestrator waiting step=%s remaining=%d",
                    spec.name, open_count,
                )
            time.sleep(5)

        # 4. Aggregate + raise on partition failure. Rule-005 requires the
        #    catcher log and not the thrower, so the raise-bearing helper
        #    is separate from _invoke_process_pool's own dispatch/spawn/
        #    wait logging.
        return self._aggregate_worker_results(
            spec, wi_coll, item_ids, partitions, max_parallel,
        )

    def _surface_crashed_workers(self, wi_coll, spec, crashed) -> None:
        """Mark WIs whose Worker heartbeat has gone stale as failed. Called
        from the poll loop; sole side effect is a Mongo update + info log."""
        ids = [c["_id"] for c in crashed]
        now = _dt.datetime.utcnow()
        wi_coll.update_many(
            {"_id": {"$in": ids}, "status": "running"},
            {"$set": {
                "status": "failed",
                "output": {"error": "worker_heartbeat_stale",
                           "stale_threshold_s": _HEARTBEAT_STALE_S},
                "finished_at": now,
            }},
        )
        _log.info(
            "orchestrator crash-detected step=%s count=%d partitions=%s",
            spec.name, len(ids),
            [str(c.get("partition")) for c in crashed[:5]],
        )

    def _aggregate_worker_results(
        self, spec, wi_coll, item_ids, partitions, workers_spawned,
    ):
        done = wi_coll.count_documents({"_id": {"$in": item_ids}, "status": "done"})
        failed = wi_coll.count_documents({"_id": {"$in": item_ids}, "status": "failed"})
        failures = list(wi_coll.find(
            {"_id": {"$in": item_ids}, "status": "failed"},
            {"partition": 1, "output": 1},
        ).limit(10))
        summary = {
            "step": spec.name,
            "partitions": len(partitions),
            "workers_spawned": workers_spawned,
            "done": done,
            "failed": failed,
            "mode": "worker_subprocess",
        }
        if failed:
            summary["failures"] = [
                {"partition": f.get("partition"), "output": f.get("output")}
                for f in failures
            ]
            raise ChatHealthyException(
                mode="pipeline_step_failed",
                message=(
                    f"step {spec.name!r} had {failed}/{len(partitions)} "
                    f"partitions fail. First failures: {summary['failures']}"
                ),
                component="BasePipelineOrchestrator",
                step=spec.name,
                done=done,
                failed=failed,
            )
        return summary

    # ------------------------------------------------------------------ #
    # Partition & prerequisite helpers
    # ------------------------------------------------------------------ #
    def _partitions_for(self, spec: StepSpec, ctx: StepContext) -> Iterable[dict]:
        # Non-fanout steps (parallelism in {None,"serial","gather"}) get a
        # single implicit partition so they still route through Worker
        # subprocess dispatch per LLD v36 §3.1.4/§3.1.5/§4.3.2 (Controller
        # orchestrates, Workers execute — every step).
        if spec.parallelism in (None, "serial", "gather"):
            return [{"single": True}]
        key = spec.partition_key or "business_address_state"
        if key == "source_name":
            # One partition per pipeline source (INCLUDING pl_pfile) for
            # downstream steps like load_staging_parallel that need one
            # Worker per source.
            return [
                {"source": "nppes_npi"},
                {"source": "nucc"},
                {"source": "census_zcta_county"},
                {"source": "usda_rucc"},
                {"source": "pl_pfile"},
                {"source": "specialty_catalog"},
            ]
        if key == "source_name_base":
            # Fetch-phase partitioning. pl_pfile is EXCLUDED because it is
            # derived from nppes_npi's ZIP inside the nppes Worker's own
            # fetch (see steps.fetch_all_sources._DERIVED_SOURCES). Giving
            # pl_pfile its own fetch Worker would race the parent Worker
            # and fail with "derived_from parent unavailable".
            return [
                {"source": "nppes_npi"},
                {"source": "nucc"},
                {"source": "census_zcta_county"},
                {"source": "usda_rucc"},
                {"source": "specialty_catalog"},
            ]
        states = ctx.args.resolved_states()
        if key == "county_partition":
            return county_partitions(states)
        return state_partitions(states)

    def _validate_prerequisites(self, steps: list[StepSpec]) -> None:
        names = {s.name for s in steps}
        for spec in steps:
            for pre in spec.prerequisites:
                if pre not in names:
                    raise ChatHealthyException(mode="value_error", message=f"Step {spec.name} names unknown prerequisite {pre!r}")

    def _require_prereqs_met(self, spec: StepSpec, manifest: RunManifest) -> None:
        missing = [p for p in spec.prerequisites if p not in manifest.completed_steps]
        if missing:
            raise ChatHealthyException(mode="runtime_error", message=f"Step {spec.name} prerequisites not met: {missing}")
