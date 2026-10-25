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

from pipeline.run_lifecycle.step_context import PipelineArgs, RunManifest, StepContext, StepTransition
from pipeline.run_lifecycle.step_spec import StepSpec
from pipeline.run_lifecycle.steps._partitions import (county_partitions, state_entity_partitions,
                               state_partitions)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_log = ChatHealthyLoggingService()

# Worker liveness is REPORTED, never enforced (operator ruling 2026-10-24).
# The heartbeat is written every 120s by pipeline_worker._HeartbeatThread. A
# running item whose beat is older than worker_liveness.silent_after_s is
# SILENT -- logged so a stalled worker is visible, and nothing more. No
# elapsed time kills a work item, fails a step, or ends a run: a process does
# not get to decide that another process's work has taken too long. The run's
# only window is the one the operator argues when the run is fired.


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
    PIPELINE_DISPLAY_NAME: str = ""
    STEPS: list[StepSpec] = []

    def __init__(
        self,
        env: str,
        config: dict[str, Any],
        mongo_client: Any,
        blob_client: Any,
    ) -> None:
        if not self.PIPELINE_NAME:
            raise ChatHealthyException(
                mode="not_implemented",
                component="base_pipeline_orchestrator",
                message="Subclass must declare PIPELINE_NAME")
        if not self.STEPS:
            raise ChatHealthyException(
                mode="not_implemented",
                component="base_pipeline_orchestrator",
                message="Subclass must declare STEPS")
        self.env = env
        self.config = dict(config or {})
        self.mongo_client = mongo_client
        self.blob_client = blob_client

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def run(self, args: PipelineArgs) -> RunManifest:
        manifest = RunManifest.new(self.PIPELINE_NAME, args)
        _log.LogPipeline("INFO", 
            "orchestrator run=%s pipeline=%s env=%s steps=%d",
            manifest.run_id, self.PIPELINE_NAME, args.env_prefix, len(self.STEPS),
        )

        main_steps = [s for s in self.STEPS if s.invocation_phase == "main_loop"]
        finally_steps = [s for s in self.STEPS if s.invocation_phase == "finally_block"]

        self._validate_prerequisites(self.STEPS)

        # START AT (operator design 2026-10-22): begin the walk at the named
        # step. This run carries no history and references no prior job --
        # whether the walk may proceed is decided by DATA alone, through the
        # StepDataDependenciesAreMet gate below. An unknown step name is fatal
        # (a skip-until loop would otherwise mark everything complete and
        # report success on an empty run).
        start_at = getattr(args, "start_at_step", None)
        if start_at:
            names = [s.name for s in main_steps]
            if start_at not in names:
                raise ChatHealthyException(
                    mode="value_error",
                    message=(f"start_at_step {start_at!r} names no step of "
                             f"pipeline {self.PIPELINE_NAME!r}; legal steps: "
                             f"{', '.join(names)}"))
            # Infrastructure steps (always_run) are kept regardless of the
            # start point: a canceled predecessor leaves the cluster paused
            # and partial state behind, and the step owns readiness
            # (operator ruling 2026-10-22). The slice removes data steps only.
            # skipped_step_names is the ONE skip truth, shared with every
            # status surface so the walk and the report cannot disagree.
            from pipeline.run_lifecycle.step_spec import skipped_step_names  # noqa: PLC0415
            skipped = skipped_step_names(self.STEPS, start_at)
            main_steps = [s for s in main_steps if s.name not in skipped]
            _log.LogPipeline("INFO",
                "orchestrator run=%s starts at step %s (%d of %d steps scheduled)",
                manifest.run_id, start_at, len(main_steps), len(names))
        scheduled = {s.name for s in main_steps}

        ctx = StepContext(
            args=args,
            manifest=manifest,
            config=self.config,
            mongo_client=self.mongo_client,
            blob_client=self.blob_client,
        )

        try:
            for spec in main_steps:
                # The DATA gate, before EVERY step (operator design
                # 2026-10-22): the collections this step consumes either exist
                # with data, or the run dies here. No history is consulted.
                if not self.StepDataDependenciesAreMet(spec.name, ctx):
                    raise ChatHealthyException(
                        mode="pipeline_data_dependencies_unmet",
                        message=(f"step {spec.name!r}: the data this step "
                                 f"consumes is absent or empty; the run cannot "
                                 f"proceed (StepDataDependenciesAreMet=False)"))
                # In-run ordering: only prerequisites scheduled in THIS run are
                # required to have completed; a prerequisite before the
                # start-at point is vouched for by the data gate, not history.
                self._require_prereqs_met(spec, manifest,
                                          required_among=scheduled)
                self._invoke_step(spec, ctx)
            manifest.status = "succeeded"
        except Exception as exc:
            manifest.status = "failed"
            manifest.metrics.setdefault("failure", {})["message"] = str(exc)
            _log.LogPipeline("EXCEPTION", "orchestrator run=%s failed at main-loop step", manifest.run_id)
            raise
        finally:
            for spec in finally_steps:
                try:
                    # The same DATA gate guards every step, finally-block
                    # included (operator design 2026-10-22).
                    if not self.StepDataDependenciesAreMet(spec.name, ctx):
                        raise ChatHealthyException(
                            mode="pipeline_data_dependencies_unmet",
                            message=(f"step {spec.name!r}: the data this step "
                                     f"consumes is absent or empty "
                                     f"(StepDataDependenciesAreMet=False)"))
                    self._invoke_step(spec, ctx)
                except Exception:
                    _log.LogPipeline("EXCEPTION",
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
        wi_coll = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.work_items"]

        # 1. Enqueue work_items.
        args_snapshot = {
            "env_prefix": ctx.args.env_prefix,
            "state_scope": ctx.args.state_scope or ctx.args.states,
            "load_mode": (
                "full" if not ctx.args.incremental else "incremental"
            ),
            "data_version": int(ctx.args.data_version),
            # Operator-driven flags carried on the args snapshot so worker
            # subprocesses see the same value the Controller received.
            "google_maps_enabled": bool(getattr(ctx.args, "google_maps_enabled", False)),
            "build_fda_drug": bool(getattr(ctx.args, "build_fda_drug", False)),
            "provider_collection": getattr(ctx.args, "provider_collection", None),
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
        _log.LogPipeline("INFO", 
            "orchestrator dispatch step=%s partitions=%d work_items_enqueued=%d "
            "claimable=%d",
            spec.name, len(partitions), len(item_ids), claimable,
        )

        # 2. Spawn N Worker subprocesses. Concurrency ceiling (LLD §I.2.3.4) is
        #    min(len(partitions), the step's declared parallelism_cap). The cap
        #    lives in the persistent fan-out config
        #    (fan_out.<pipeline>.<step>.parallelism_cap); WORKER_MAX_PARALLEL
        #    remains an operator override for dialing a VM down. Neither set =>
        #    one worker per partition. Workers race on findOneAndUpdate to claim.
        cap_cfg = self._fan_out_spec(spec, ctx).get("parallelism_cap")
        max_parallel_env = os.environ.get("WORKER_MAX_PARALLEL", "").strip()
        if max_parallel_env.isdigit():
            max_parallel_cap = int(max_parallel_env)
        elif isinstance(cap_cfg, int) and cap_cfg > 0:
            max_parallel_cap = cap_cfg
        else:
            max_parallel_cap = len(partitions)
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
        # Worker compute shape. Default 'subprocess' (single-VM run): workers
        # are detached subprocesses on this VM, unchanged. 'vm' (two-VM run):
        # one ephemeral Worker VM per partition, each booting pipeline_worker.py
        # to claim one of the work_items enqueued above. A pipeline opts in by
        # declaring worker_compute='vm' in its config; every current pipeline
        # leaves it unset and keeps the subprocess path below untouched. The
        # wait/aggregate loop that follows watches work_item heartbeats, not
        # process handles, so it is identical for both shapes.
        worker_compute = str(
            ctx.config.get("worker_compute")
            or os.environ.get("WORKER_COMPUTE", "subprocess")
        ).strip().lower()
        if worker_compute == "vm":
            # Two-VM shape: the run has ONE Worker VM (the big box), allocated
            # once through the common hardware-allocation function. Its resident
            # worker_host drains the work_items enqueued above - and every later
            # step's - as a bounded pool of processes. The Controller spawns
            # nothing locally; it only waits on the work_items below.
            from pipeline.run_lifecycle.worker_vm_provisioning import allocate_worker_vm
            if not getattr(self, "_worker_vm_name", None):
                res = allocate_worker_vm(run_id, ctx.manifest.pipeline_name)
                self._worker_vm_name = res.get("vm_name")
                _log.LogPipeline("INFO",
                    "orchestrator allocated worker VM run_id=%s vm=%s",
                    run_id, self._worker_vm_name)
        else:
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
            _log.LogPipeline("INFO",
                "orchestrator spawned step=%s workers=%d worker_py=%s pids=%s "
                "detached_children_total=%s cert=%s",
                spec.name, max_parallel, worker_py, worker_pids,
                os.environ["CHATHEALTHY_DETACHED_CHILDREN"],
                os.environ.get("CHATHEALTHY_CERT_PATH", "<unset>"),
            )

        # 3. Wait for terminal state on every work_item. Poll every 5s.
        #    NO INTERIM TIME LIMIT (operator ruling 2026-10-24). Nothing here
        #    decides that work has taken too long: a step is not bounded by a
        #    budget, and a quiet worker is not killed for being quiet. The run
        #    has ONE window, the one the operator argues per run, and it is
        #    enforced where it belongs -- on the run, not inside it. A step
        #    timeout killed a four-hour run two partitions from completion and
        #    destroyed the work of the other fifty-nine.
        #
        #    Silence is reported, never acted on: the orchestrator says how
        #    many items are open and how long the quietest has been quiet, so
        #    a genuinely dead worker is VISIBLE and the operator decides.
        liveness = self._worker_liveness(ctx)
        polls = 0
        while True:
            open_count = wi_coll.count_documents({
                "_id": {"$in": item_ids},
                "status": {"$in": ["pending", "running"]},
            })
            if open_count == 0:
                break
            polls += 1
            if polls % 24 == 0:  # every ~2 min
                now = _dt.datetime.utcnow()
                silent_cutoff = now - _dt.timedelta(seconds=liveness["silent_after_s"])
                silent = wi_coll.count_documents({
                    "_id": {"$in": item_ids},
                    "status": "running",
                    "heartbeat_at": {"$lt": silent_cutoff},
                })
                quietest = ""
                oldest = wi_coll.find_one(
                    {"_id": {"$in": item_ids}, "status": "running"},
                    {"partition": 1, "heartbeat_at": 1},
                    sort=[("heartbeat_at", 1)])
                if oldest and oldest.get("heartbeat_at"):
                    age = int((now - oldest["heartbeat_at"]).total_seconds())
                    quietest = (f" quietest={oldest.get('partition')}"
                                f" last_beat={age}s ago")
                _log.LogPipeline("INFO",
                    "orchestrator waiting step=%s remaining=%d silent=%d%s",
                    spec.name, open_count, silent, quietest,
                )
            time.sleep(5)

        # 4. Aggregate + raise on partition failure. Rule-005 requires the
        #    catcher log and not the thrower, so the raise-bearing helper
        #    is separate from _invoke_process_pool's own dispatch/spawn/
        #    wait logging.
        return self._aggregate_worker_results(
            spec, wi_coll, item_ids, partitions, max_parallel,
        )

    def _worker_liveness(self, ctx: StepContext) -> dict:
        """The worker_liveness block from the per-env durable configuration.
        silent_after_s is the REPORTING threshold and the only one left: it
        decides when a quiet worker is worth a log line, never when it dies."""
        liveness = (ctx.config or {}).get("worker_liveness")
        if (not isinstance(liveness, dict)
                or not isinstance(liveness.get("silent_after_s"), int)):
            raise ChatHealthyException(
                mode="pipeline_config_missing_value",
                message=("worker_liveness{silent_after_s} is required in the "
                         "per-env pipeline configuration and is absent or "
                         "malformed (H7: no defaults)"),
                component="BasePipelineOrchestrator",
            )
        return liveness


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
    # ---- Fan-out: fully config-driven, per LLD §I.3.4 --------------------- #
    # A fan-out step declares NOTHING dimension-specific in code. The persistent
    # config (pipeline_config.json → fan_out.<pipeline>.<step>) names the
    # partition_source that yields the step's partition values at run time and
    # the parallelism_cap that bounds its workers. The orchestrator consumes
    # that generically, with no built-in knowledge of any dimension (state,
    # record range, source). Named enumerators the pipeline supplies are the one
    # code hook; everything else is data. Replaces the hard-coded dimension
    # switch that was BUG-014.

    def _fan_out_spec(self, spec: StepSpec, ctx: StepContext) -> dict:
        """One step's persistent fan-out declaration: {partition_source,
        parallelism_cap}. Keyed in config by pipeline, then step."""
        fan_out = (ctx.config or {}).get("fan_out") or {}
        return (fan_out.get(self.PIPELINE_NAME) or {}).get(spec.name) or {}

    def _partition_enumerators(self):
        """name -> enumerator(ctx, **params) -> list[partition dict]. The base
        supplies the geographic enumerators; a pipeline extends this by
        overriding and merging in its own (e.g. record_ranges, nucc)."""
        return {
            "state": self._enum_state,
            "county": self._enum_county,
            "state_entity": self._enum_state_entity,
        }

    def _enum_state(self, ctx: StepContext, *, key: str = "business_address_state") -> list[dict]:
        """52-way state fan-out, emitted under the step's declared key."""
        return [{key: next(iter(p.values()))}
                for p in state_partitions(ctx.args.partition_states())]

    def _enum_county(self, ctx: StepContext) -> list[dict]:
        return list(county_partitions(ctx.args.partition_states()))

    def _enum_state_entity(self, ctx: StepContext, *, key: str = "business_address_state") -> list[dict]:
        """State x entity-type fan-out: 2 partitions per state (full scope ->
        104), scaling with the run's state scope so two states yield 4, never
        more processes than partitions. Emitted under the step's declared key so
        a pipeline that fans by prescriber state gets that key, not the default."""
        return [{key: next(iter(p.values())), "entity_type": t}
                for p in state_partitions(ctx.args.partition_states())
                for t in (1, 2)]

    def _resolve_partition_source(self, spec: StepSpec, ctx: StepContext, src: dict) -> list[dict]:
        t = src.get("type")
        if t == "config_list":
            return [dict(v) for v in (src.get("values") or [])]
        if t == "mongo_distinct":
            coll = ctx.mongo_client[src["database"]][src["collection"]]
            key = src.get("partition_key") or spec.partition_key
            vals = [v for v in coll.distinct(src["field"], src.get("filter") or {})
                    if v is not None]
            return [{key: v} for v in sorted(vals)]
        if t == "named_enumerator":
            fn = self._partition_enumerators().get(src.get("name"))
            if fn is None:
                raise ChatHealthyException(
                    mode="value_error",
                    message=(f"fan-out step {spec.name!r}: unknown partition "
                             f"enumerator {src.get('name')!r}"))
            return list(fn(ctx, **(src.get("params") or {})))
        raise ChatHealthyException(
            mode="value_error",
            message=(f"fan-out step {spec.name!r}: unknown partition_source type "
                     f"{t!r} (expected config_list | mongo_distinct | named_enumerator)"))

    def _partitions_for(self, spec: StepSpec, ctx: StepContext) -> Iterable[dict]:
        # Non-fanout steps still route one implicit partition through Worker
        # dispatch (Controller orchestrates, Workers execute — every step).
        if spec.parallelism in (None, "serial", "gather"):
            return [{"single": True}]
        src = self._fan_out_spec(spec, ctx).get("partition_source")
        if not src:
            raise ChatHealthyException(
                mode="value_error",
                message=(f"fan-out step {spec.name!r} declares no partition_source in "
                         f"config fan_out[{self.PIPELINE_NAME!r}]; §I.3.4 requires every "
                         f"fan-out step to name its partition source"))
        parts = self._resolve_partition_source(spec, ctx, src)
        _log.LogPipeline(
            "INFO", "orchestrator fan-out step=%s source=%s partitions=%d",
            spec.name, src.get("type"), len(parts))
        return parts

    def _validate_prerequisites(self, steps: list[StepSpec]) -> None:
        names = {s.name for s in steps}
        for spec in steps:
            for pre in spec.prerequisites:
                if pre not in names:
                    raise ChatHealthyException(mode="value_error", message=f"Step {spec.name} names unknown prerequisite {pre!r}")

    def _require_prereqs_met(self, spec: StepSpec, manifest: RunManifest,
                             required_among: set[str] | None = None) -> None:
        """In-run ordering only: a prerequisite is required to have completed
        iff it is scheduled in THIS run (required_among). A prerequisite
        before a start-at point is vouched for by the StepDataDependenciesAreMet
        gate -- data, not history (operator design 2026-10-22)."""
        missing = [p for p in spec.prerequisites
                   if (required_among is None or p in required_among)
                   and p not in manifest.completed_steps]
        if missing:
            raise ChatHealthyException(mode="runtime_error", message=f"Step {spec.name} prerequisites not met: {missing}")

    def StepDataDependenciesAreMet(self, step_name: str, ctx: StepContext) -> bool:
        """Does the data this step consumes exist, with rows in it? Called by
        the infrastructure before EVERY step (operator design 2026-10-22).
        Virtual: every pipeline child class MUST override this and answer for
        every one of its steps -- a step that consumes nothing answers True
        explicitly. A pipeline that fails to override has no business running."""
        raise ChatHealthyException(
            mode="not_implemented",
            message=(f"{type(self).__name__} does not override "
                     f"StepDataDependenciesAreMet; every pipeline must declare "
                     f"the data dependencies of every step (asked for "
                     f"{step_name!r})"))
