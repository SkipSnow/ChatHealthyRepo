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

import logging
from datetime import datetime, timezone
from typing import Any, Iterable

# v32: aca_job_manager is retired. Fan-out steps run sequentially in the
# Controller process for the initial testable build (see _invoke_process_pool).
from step_context import PipelineArgs, RunManifest, StepContext, StepTransition
from step_spec import StepSpec
from steps import get_runner
from steps._partitions import county_partitions, state_partitions

_log = logging.getLogger(__name__)


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
        _log.info("step=%s parallelism=%s phase=%s begin",
                  spec.name, spec.parallelism, spec.invocation_phase)

        try:
            if spec.parallelism in (None, "serial", "gather"):
                summary = self._invoke_in_process(spec, ctx)
            elif spec.parallelism == "process_pool":
                summary = self._invoke_process_pool(spec, ctx)
            else:
                raise ValueError(
                    f"Unknown parallelism {spec.parallelism!r} on step {spec.name}"
                )
            transition.summary = summary if isinstance(summary, dict) else {"result": summary}
            transition.status = "success"
            ctx.step_summaries[spec.name] = transition.summary
            ctx.manifest.completed_steps.add(spec.name)
            _log.info("step=%s success summary_keys=%s",
                      spec.name, list(transition.summary.keys()))
        except Exception as exc:
            transition.status = "failed"
            transition.error = str(exc)
            _log.exception("step=%s failed", spec.name)
            raise
        finally:
            transition.finished_at = _utc_now_iso()
            ctx.manifest.updated_at = transition.finished_at

    def _invoke_in_process(self, spec: StepSpec, ctx: StepContext) -> dict:
        runner = get_runner(spec.name)
        result = runner(ctx)
        return result if isinstance(result, dict) else {"result": result}

    def _invoke_process_pool(self, spec: StepSpec, ctx: StepContext) -> dict:
        """v32 §5 fan-out execution — Controller-in-process partition loop.

        Every partition runs sequentially inside the Controller process. On
        the Pipeline Run VM this is one Python process; the parallelism
        knob that used to fan work out to ACA Job replicas (v22-v25) is
        deferred until the Worker-subprocess model in `pipeline_worker.py`
        is fully wired. For the initial v32 testable build, sequential
        Controller-in-process partition execution is correct — it exercises
        every business-logic path end-to-end.
        """
        partitions = list(self._partitions_for(spec, ctx))
        runner = get_runner(spec.name)
        for part in partitions:
            per_ctx_config = dict(ctx.config)
            per_ctx_config["partition"] = part
            per_ctx = StepContext(
                args=ctx.args,
                manifest=ctx.manifest,
                config=per_ctx_config,
                mongo_client=ctx.mongo_client,
                blob_client=ctx.blob_client,
                notification_client=ctx.notification_client,
                catalog_cache=ctx.catalog_cache,
                catalog=ctx.catalog,
                step_summaries=ctx.step_summaries,
            )
            runner(per_ctx)
        return {
            "step": spec.name,
            "partitions": len(partitions),
            "mode": "controller_in_process_sequential",
        }

    # ------------------------------------------------------------------ #
    # Partition & prerequisite helpers
    # ------------------------------------------------------------------ #
    def _partitions_for(self, spec: StepSpec, ctx: StepContext) -> Iterable[dict]:
        key = spec.partition_key or "business_address_state"
        states = ctx.args.resolved_states()
        if key == "county_partition":
            return county_partitions(states)
        return state_partitions(states)

    def _validate_prerequisites(self, steps: list[StepSpec]) -> None:
        names = {s.name for s in steps}
        for spec in steps:
            for pre in spec.prerequisites:
                if pre not in names:
                    raise ValueError(
                        f"Step {spec.name} names unknown prerequisite {pre!r}"
                    )

    def _require_prereqs_met(self, spec: StepSpec, manifest: RunManifest) -> None:
        missing = [p for p in spec.prerequisites if p not in manifest.completed_steps]
        if missing:
            raise RuntimeError(
                f"Step {spec.name} prerequisites not met: {missing}"
            )
