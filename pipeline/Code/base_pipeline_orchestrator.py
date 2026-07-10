# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""BasePipelineOrchestrator — LLD v22 ACA Control orchestration tier."""

from __future__ import annotations

import importlib
import logging
import os
import threading
import traceback
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from pipeline_config import load_pipeline_config
from step_context import PipelineArgs, RunManifest, StepContext, StepTransition
from step_spec import StepSpec
from work_item_claim import (
    claim_work_item,
    complete_work_item,
    fail_work_item,
    seed_work_items,
    wait_for_step_completion,
)

try:
    from aca_job_manager import delete_job, provision_job, start_job
except ImportError:
    provision_job = delete_job = start_job = None  # type: ignore

_log = logging.getLogger("base_pipeline_orchestrator")

RUNS_DB = "chathealthyfrontend"
RUNS_COLL = "pipeline.runs"


def _runs_collection_name() -> str:
    if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
        from pipeline_test_config import TEST_RUNS_COLL
        return TEST_RUNS_COLL.split(".", 1)[-1]
    return RUNS_COLL


def _runs_db_name() -> str:
    if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
        from pipeline_test_config import TEST_RUNS_DB
        return TEST_RUNS_DB
    return RUNS_DB


class BasePipelineOrchestrator(ABC):
    PIPELINE_NAME: str = ""
    STEPS: list[StepSpec] = []

    def __init__(
        self,
        env: str,
        config: dict,
        notification_client=None,
        mongo_client=None,
        blob_client=None,
    ) -> None:
        if not self.PIPELINE_NAME:
            raise ValueError(f"{type(self).__name__}.PIPELINE_NAME must be set")
        if not self.STEPS:
            raise ValueError(f"{type(self).__name__}.STEPS must be declared")
        self.env = env
        self.config = config
        self.notification_client = notification_client
        self._mongo = mongo_client
        self._blob = blob_client
        self._manifest_lock = threading.Lock()

    @abstractmethod
    def build_step_context(self, args: PipelineArgs) -> StepContext:
        raise NotImplementedError

    def _frontend_mongo(self):
        from pipeline_db import get_frontend_mongo
        return get_frontend_mongo()

    def _discrepancy_count(self, run_id: str) -> int:
        db_name, coll_name = _runs_db_name(), "pipeline.discrepancies"
        if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
            from pipeline_test_config import TEST_DISCREPANCIES_COLL
            db_name, coll_name = TEST_DISCREPANCIES_COLL.split(".", 1)
        return self._frontend_mongo()[db_name][coll_name].count_documents({"run_id": run_id})

    def _failure_threshold(self, ctx: StepContext) -> int:
        cfg = ctx.config.get("failure_thresholds") or {}
        return int(cfg.get("discrepancy_abort", ctx.args.discrepancy_threshold))

    def _check_failure_threshold(self, ctx: StepContext) -> None:
        threshold = self._failure_threshold(ctx)
        count = self._discrepancy_count(ctx.run_id)
        ctx.manifest.metrics["discrepancy_count"] = count
        if count > threshold:
            reason = f"discrepancy_threshold_exceeded count={count} threshold={threshold}"
            ctx.manifest.metrics["abort_reason"] = reason
            with self._manifest_lock:
                self._runs_coll().update_one(
                    {"run_id": ctx.run_id},
                    {"$set": {"abort_reason": reason, "status": "failed"}},
                )
            raise RuntimeError(reason)

    def _maybe_run_aca_stage(self, ctx: StepContext, spec: StepSpec, partitions: list[dict]) -> bool:
        if not spec.aca_job_name or provision_job is None:
            return False
        if os.environ.get("ACA_JOB_DEPLOY", "").lower() not in ("1", "true", "yes"):
            return False
        if os.environ.get("PIPELINE_WORKER_MODE", "control") != "control":
            return False
        job_name = spec.aca_job_name
        parallelism = min(len(partitions), int(ctx.config.get("pool_size", 8)))
        provision_job(job_name, parallelism=parallelism)
        start_job(job_name, run_id=ctx.run_id, step=spec.name, env_prefix=ctx.env_prefix)
        wait_for_step_completion(self._mongo, run_id=ctx.run_id, step=spec.name)
        delete_job(job_name)
        return True

    def _runs_coll(self):
        return self._frontend_mongo()[_runs_db_name()][_runs_collection_name()]

    def _persist_manifest(self, manifest: RunManifest) -> None:
        with self._manifest_lock:
            manifest.updated_at = datetime.now(timezone.utc).isoformat()
            self._runs_coll().update_one(
                {"run_id": manifest.run_id},
                {"$set": {
                    "run_id": manifest.run_id,
                    "pipeline_name": manifest.pipeline_name,
                    "env_prefix": manifest.env_prefix,
                    "status": manifest.status,
                    "created_at": manifest.created_at,
                    "updated_at": manifest.updated_at,
                    "step_transitions": [t.__dict__ for t in manifest.step_transitions],
                    "source_versions": manifest.source_versions,
                    "discrepancy_summary": manifest.discrepancy_summary,
                    "metrics": manifest.metrics,
                    "completed_steps": sorted(manifest.completed_steps),
                    "aca_job_resource_id": manifest.aca_job_resource_id,
                }},
                upsert=True,
            )

    def _schedule_main_steps(self, ctx: StepContext, main_steps: list[StepSpec], start_from: str | None) -> None:
        order = {s.name: i for i, s in enumerate(main_steps)}
        if start_from:
            start_idx = order[start_from]
            allowed = {s.name for s in main_steps[start_idx:]}
        else:
            allowed = {s.name for s in main_steps}

        pending = {s.name for s in main_steps if s.name in allowed}

        while pending:
            ready = [
                s for s in main_steps
                if s.name in pending and self._prerequisites_met(s, ctx.manifest)
            ]
            if not ready:
                raise RuntimeError(f"Pipeline deadlock — pending={sorted(pending)}")

            gather = [s for s in ready if s.parallelism == "gather"]
            if gather:
                with ThreadPoolExecutor(max_workers=len(gather)) as pool:
                    futs = {pool.submit(self._run_step, ctx, spec): spec for spec in gather}
                    for fut in as_completed(futs):
                        fut.result()
                for spec in gather:
                    pending.discard(spec.name)
                continue

            serial = sorted(
                [s for s in ready if s.parallelism != "gather"],
                key=lambda s: order[s.name],
            )
            spec = serial[0]
            self._run_step(ctx, spec)
            pending.discard(spec.name)

    def _prerequisites_met(self, spec: StepSpec, manifest: RunManifest) -> bool:
        return all(p in manifest.completed_steps for p in spec.prerequisites)

    def _load_handler(self, step_name: str):
        mod = importlib.import_module(f"steps.{step_name}")
        if not hasattr(mod, "execute"):
            raise RuntimeError(f"steps.{step_name} missing execute()")
        return mod.execute

    def _execute_serial(self, ctx: StepContext, step_name: str) -> dict:
        return self._load_handler(step_name)(ctx) or {}

    def _execute_process_pool(self, ctx: StepContext, spec: StepSpec) -> dict:
        from steps._partitions import county_partitions, state_partitions

        states = ctx.args.resolved_states()
        if spec.partition_key and "kind" in (spec.partition_key or ""):
            partitions = county_partitions(states)
        else:
            partitions = state_partitions(states)

        worker_mode = os.environ.get("PIPELINE_WORKER_MODE", "control")
        if worker_mode == "worker":
            worker_id = os.environ.get("PIPELINE_WORKER_ID", "worker-1")
            item = claim_work_item(self._mongo, run_id=ctx.run_id, step=spec.name, worker_id=worker_id)
            if not item:
                return {"partitions": 0}
            handler = self._load_handler(spec.name)
            try:
                part_ctx = StepContext(
                    args=ctx.args,
                    manifest=ctx.manifest,
                    config=ctx.config,
                    mongo_client=ctx.mongo_client,
                    blob_client=ctx.blob_client,
                    notification_client=ctx.notification_client,
                    catalog_cache=ctx.catalog_cache,
                    catalog=ctx.catalog,
                    step_summaries=ctx.step_summaries,
                )
                part_ctx.config["partition"] = item["partition"]
                result = handler(part_ctx) or {}
                complete_work_item(self._mongo, item_id=item["_id"], result=result)
                return result
            except Exception as exc:
                fail_work_item(self._mongo, item_id=item["_id"], error=str(exc))
                raise

        seed_work_items(self._mongo, run_id=ctx.run_id, step=spec.name, partitions=partitions)
        if self._maybe_run_aca_stage(ctx, spec, partitions):
            return {"partitions": len(partitions), "mode": "aca"}

        handler = self._load_handler(spec.name)
        max_workers = min(len(partitions), int(ctx.config.get("pool_size", 8)))

        def _run_partition(part: dict) -> dict:
            part_ctx = StepContext(
                args=ctx.args,
                manifest=ctx.manifest,
                config=dict(ctx.config),
                mongo_client=ctx.mongo_client,
                blob_client=ctx.blob_client,
                notification_client=ctx.notification_client,
                catalog_cache=ctx.catalog_cache,
                catalog=ctx.catalog,
                step_summaries=ctx.step_summaries,
            )
            part_ctx.config["partition"] = part
            return handler(part_ctx) or {}

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_run_partition, p): p for p in partitions}
            for fut in as_completed(futures):
                results.append(fut.result())
        return {"partitions": len(partitions), "results": results}

    def _run_step(self, ctx: StepContext, spec: StepSpec) -> None:
        if not self._prerequisites_met(spec, ctx.manifest):
            raise RuntimeError(f"Prerequisites not met for step {spec.name}")

        started = datetime.now(timezone.utc).isoformat()
        transition = StepTransition(step=spec.name, status="running", started_at=started)
        ctx.manifest.step_transitions.append(transition)
        self._persist_manifest(ctx.manifest)

        try:
            if spec.parallelism == "process_pool":
                summary = self._execute_process_pool(ctx, spec)
            else:
                summary = self._execute_serial(ctx, spec.name)
            with self._manifest_lock:
                transition.status = "completed"
                transition.summary = summary
                ctx.step_summaries[spec.name] = summary
                ctx.manifest.completed_steps.add(spec.name)
        except Exception as exc:
            with self._manifest_lock:
                transition.status = "failed"
                transition.error = str(exc)[:500]
            _log.error("step %s failed: %s", spec.name, exc)
            raise
        finally:
            with self._manifest_lock:
                transition.finished_at = datetime.now(timezone.utc).isoformat()
            self._persist_manifest(ctx.manifest)
            if spec.name not in ("discrepancy_reports_and_notifications", "quiesce_infrastructure"):
                try:
                    self._check_failure_threshold(ctx)
                except RuntimeError:
                    raise

    def run(self, args: PipelineArgs) -> RunManifest:
        manifest = RunManifest.new(self.PIPELINE_NAME, args)
        manifest.metrics["states"] = args.resolved_states()
        manifest.metrics["load_mode"] = "incremental" if args.incremental else "full"
        ctx = self.build_step_context(args)
        ctx.manifest = manifest
        self._persist_manifest(manifest)

        main_steps = [s for s in self.STEPS if s.invocation_phase == "main_loop"]
        finally_steps = [s for s in self.STEPS if s.invocation_phase == "finally_block"]
        abort_error: Exception | None = None

        try:
            if args.resume_from_step:
                self._schedule_main_steps(ctx, main_steps, args.resume_from_step)
            else:
                self._schedule_main_steps(ctx, main_steps, None)
            manifest.status = "completed"
        except Exception as exc:
            abort_error = exc
            manifest.status = "failed"
            manifest.metrics["abort_error"] = str(exc)[:500]
        finally:
            for spec in finally_steps:
                try:
                    self._run_step(ctx, spec)
                except Exception as exc:
                    _log.error("finally step %s failed: %s", spec.name, exc)
                    manifest.metrics.setdefault("finally_errors", []).append({
                        "step": spec.name,
                        "error": str(exc)[:500],
                    })
            self._persist_manifest(manifest)

        if abort_error:
            raise abort_error
        return manifest

    def resume(self, run_id: str, from_step: str) -> RunManifest:
        doc = self._runs_coll().find_one({"run_id": run_id})
        if not doc:
            raise ValueError(f"run_id not found: {run_id}")
        args = PipelineArgs(
            states=doc.get("metrics", {}).get("states", ["WY"]),
            env_prefix=doc.get("env_prefix", self.env),
            run_id=run_id,
            resume_from_step=from_step,
        )
        completed = set(doc.get("completed_steps") or [])
        manifest = RunManifest(
            run_id=run_id,
            pipeline_name=doc["pipeline_name"],
            env_prefix=doc["env_prefix"],
            status=doc.get("status", "running"),
            created_at=doc.get("created_at", datetime.now(timezone.utc).isoformat()),
            completed_steps=completed,
        )
        ctx = self.build_step_context(args)
        ctx.manifest = manifest
        return self.run(args)
