# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""fan_out_workers — shared Step-3 substrate for every worker-based pipeline step.

Every step that fans work across parallel workers (raw load, normalize,
multi-practice load, each county-enrichment pass, urban, provider flags,
embeddings) invokes this sub-orchestration. It owns the pre-warm / task_all /
cool-down / step-report shape so the caller is left with only the
business-specific worker activity and its per-worker config list.

Input shape (orchestrator input):
    {
        "worker_activity": "<activity_name>",     # required
        "worker_configs":  [ {...}, {...}, ... ], # required; one dict per task
        "num_workers":     <int>,                 # required; pre-warm instanceCount
        "step_label":      "<human-readable label for the step>",
        "warm_config":     { ... },               # passed to warm_instances_activity
        "cool_config":     { ... },               # passed to cool_instances_activity
        "skip_warm":       false,                 # optional
    }

Output:
    {
        "step_label":    "...",
        "worker_count":  <int>,
        "results":       [ <activity return value>, ... ],
        "warm_metrics":  { ... } | null,
        "cool_metrics":  { ... } | null,
    }
"""

from __future__ import annotations

import logging


def fan_out_workers_orchestrator_fn(context):
    cfg = context.get_input() or {}

    worker_activity = cfg["worker_activity"]
    worker_configs = cfg["worker_configs"]
    num_workers = cfg["num_workers"]
    step_label = cfg.get("step_label", worker_activity)
    warm_cfg = cfg.get("warm_config") or {"num_workers": num_workers}
    cool_cfg = cfg.get("cool_config") or {}
    skip_warm = bool(cfg.get("skip_warm", False))

    context.set_custom_status(f"{step_label}: pre-warming {num_workers} instance(s)")

    warm_metrics = None
    if not skip_warm and num_workers > 0:
        warm_metrics = yield context.call_activity(
            "warm_instances_activity",
            {**warm_cfg, "num_workers": num_workers},
        )

    context.set_custom_status(f"{step_label}: fan-out ({len(worker_configs)} task(s))")

    tasks = [
        context.call_activity(worker_activity, wc)
        for wc in worker_configs
    ]
    results = (yield context.task_all(tasks)) if tasks else []

    context.set_custom_status(f"{step_label}: cooling down")

    cool_metrics = None
    if not skip_warm:
        cool_metrics = yield context.call_activity(
            "cool_instances_activity",
            cool_cfg,
        )

    summary = {
        "step_label":   step_label,
        "worker_count": len(worker_configs),
        "results":      results,
        "warm_metrics": warm_metrics,
        "cool_metrics": cool_metrics,
    }
    logging.info(
        "fan_out_workers: step=%s worker_count=%d skip_warm=%s",
        step_label, len(worker_configs), skip_warm,
    )
    return summary
