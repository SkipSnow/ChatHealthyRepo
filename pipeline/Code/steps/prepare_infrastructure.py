# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""prepare_infrastructure — wake Atlas, reserve cluster, ensure indexes."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from cluster_lifecycle_manager import ClusterLifecycleManager
from ensure_provider_indexes_activity import ensure_provider_indexes_fn
from pipeline_config import ensure_pipeline_config
from pipeline_db import get_frontend_mongo

_log = ChatHealthyLoggingService()


def execute(ctx) -> dict:
    cfg = ensure_pipeline_config(get_frontend_mongo(), ctx.env_prefix)
    ctx.config.setdefault("dataset_versions", cfg.get("dataset_versions", {}))
    ctx.config.setdefault("source_freshness", cfg.get("source_freshness", []))
    cluster = ctx.config.get("pipeline_cluster", "ChatHealthyDataPipelines")
    duration = int(ctx.args.expected_duration_minutes)
    ops = ClusterLifecycleManager(get_db_fn=get_frontend_mongo)
    ops.wake(cluster, job_id=ctx.run_id)
    reservation = ops.reserve(
        cluster_name=cluster,
        job_id=ctx.run_id,
        requester="provider_pipeline_lld",
        expected_duration_minutes=duration,
    )
    idx = ensure_provider_indexes_fn({
        "provider_collection": ctx.provider_collection,
        "cluster_wait_minutes": int(ctx.config.get("cluster_wait_minutes", 20)),
    })
    ctx.manifest.metrics["reservation"] = reservation
    return {"cluster_ready": True, "indexes": idx, "reservation": reservation}
